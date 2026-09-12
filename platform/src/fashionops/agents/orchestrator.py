"""总控编排 Agent —— 用 LangGraph StateGraph 把计划跑起来。

## 图长什么样

```
        START
          │
        [plan]          ← 模型出计划、代码校验（agents/planner.py）
          │
        [hub] ──条件边──┬─→ [selection] ──┐
          ▲             ├─→ [analytics] ──┤
          │             ├─→ [notification]┤
          └─────────────┴─→ [approval_gate]┘
                        └─→ [finalize] ──→ END
```

## 为什么是"中心 hub + 条件边"而不是写死顺序

写死 `plan → selection → analytics → notification → finalize` 简单得多，
但那样"计划"就只是个装饰品 —— 模型拆出来的 DAG 对执行没有任何影响。

用 hub 做路由，执行顺序**完全由计划决定**：
用户只说"出份报表"，图上就只跑 analytics 一步，不会白跑选品。

## 为什么每个 Agent 只能出现一次

图是**静态构建**的：一个 Agent 对应一个节点。如果计划里同一个 Agent
出现两次，节点名会冲突，路由函数也分不清"现在该跑第几次"。
所以规划校验里直接禁止重复（``duplicate_agent``）。

这确实牺牲了"同一个 Agent 干两件不同的事"的能力。等真需要时，
正确做法是按 step_id 动态构图（``add_node`` 之后再 ``compile``），
或者把重复的逻辑做成子图 —— 而不是让一个节点在两次执行间猜自己在哪一次。

## HITL 与 checkpoint 的关系

``interrupt()`` 是**抛异常**中断的，节点末尾那句 ``return`` 不会执行。
状态由 checkpointer 落盘，人确认后用**同一个 thread_id** 重新 invoke
（带 ``Command(resume=...)``）就能从断点继续。

这也是为什么 ``make_xxx_node(ctx)`` 要用工厂函数：节点闭包里持有
网关和模型客户端，它们**不能进 checkpoint**（不可序列化）。
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from fashionops.agents import analytics as analytics_mod
from fashionops.agents import notification as notification_mod
from fashionops.agents import selection as selection_mod
from fashionops.agents.planner import (
    EXECUTABLE_AGENTS,
    blocked_steps,
    make_plan_node,
    plan_as_text,
    plan_route_target,
    step_of,
)
from fashionops.agents.state import (
    ApprovalRequest,
    RunContext,
    TaskState,
)
from fashionops.db.base import utcnow

logger = logging.getLogger("fashionops.orchestrator")

#: 中心的 pass-through 节点名。条件边的**起点必须是节点**，不能直接挂 START，
#: 而且条件边函数不能写状态 —— 所以由一个真的节点来算"下一步是谁"。
HUB = "hub"
PLAN = "plan"
APPROVAL_GATE = "approval_gate"
FINALIZE = "finalize"

#: 计划里的 route 用这两个哨兵值表示"不是具体步骤"
_ROUTE_FINALIZE = "__finalize__"
_ROUTE_APPROVAL = "__approval__"

#: 终止状态：到了这些状态就不再往下走了
_TERMINAL = frozenset({"cancelled", "failed"})


def agent_node_builders() -> dict[str, Any]:
    """Agent 名 → 节点工厂。

    **必须和 ``planner.EXECUTABLE_AGENTS`` 保持一致**。
    不一致的后果是规划校验放行、构图时找不到节点，报错点离原因很远 ——
    所以下面有个显式断言，让它在构图时就炸掉。
    """
    builders: dict[str, Any] = {
        "selection": lambda ctx: selection_mod.make_selection_node(ctx),
        "analytics": lambda ctx: analytics_mod.make_analytics_node(ctx, days=30),
        "notification": lambda ctx: notification_mod.make_notification_node(ctx),
    }
    missing = [a for a in EXECUTABLE_AGENTS if a not in builders]
    if missing:
        raise RuntimeError(
            f"planner.EXECUTABLE_AGENTS 里的 {missing} 没有对应的节点实现。"
            f"加 Agent 时这两处要一起改。"
        )
    return builders


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------
def _wrap_agent(agent: str, node: Any) -> Any:
    """给 Agent 节点包一层，让它顺手记下"计划里的哪一步做完了 / 失败了"。

    Agent 节点本身不知道自己的 step_id（它只知道自己是谁），
    所以由 hub 写进 ``route``，这里读出来归档。

    成功和失败都要记：只记成功的话，失败的步骤在 hub 看来就是
    "还没做"，会被无限重试；而下游也会因为依赖没满足而卡住。
    """

    def wrapped(state: TaskState) -> dict[str, Any]:
        update = node(state)
        step_id = state.get("pending_step")
        # 只有指向真实步骤时才记录 —— 它也可能是空串
        if step_id:
            update = {**update, "completed_steps": [step_id]}
            if _step_failed(update):
                # 失败的步骤也进 completed_steps（否则会被无限重试），
                # 同时单独记一份，供 hub 算下游阻断
                update = {**update, "failed_steps": [step_id]}
        if not update.get("current_node"):
            update["current_node"] = agent
        return update

    wrapped.__name__ = f"{agent}_node"
    return wrapped


def _step_failed(update: dict[str, Any]) -> bool:
    """节点自己报告的执行状态是不是失败。

    有的节点会返回多条 steps（内部子步骤），只要有一条 failed 就算这一步没做成。
    """
    records = update.get("steps") or []
    return any(getattr(r, "status", None) == "failed" for r in records)


def make_hub_node() -> Any:
    """中心路由：算出下一步该跑谁，写进 ``route``。

    把决策放在**节点**里而不是条件边函数里，是因为条件边函数**不能写状态**。
    如果让条件边自己算，同样的逻辑就要在两处各写一遍（一处决定去哪、
    一处决定记录什么），迟早会不一致。
    """

    def hub_node(state: TaskState) -> dict[str, Any]:
        # 已经终止的任务不再往下走
        if state.get("status") in _TERMINAL:
            return {"route": _ROUTE_FINALIZE}

        completed = set(state.get("completed_steps", []))
        failed = set(state.get("failed_steps", []))
        plan_steps = (state.get("plan") or {}).get("steps", [])
        # 上游失败的**传递下游**全部跳过 —— 不能拿残缺的输入接着算
        blocked = blocked_steps(plan_steps, failed)

        target = plan_route_target(state, completed, blocked=blocked)
        if target is None:
            return {"route": _ROUTE_FINALIZE}

        step = step_of(state, target)
        if step is None:
            # 计划里查不到这个 step_id，说明状态被写坏了。
            # 不静默跳过 —— 直接结束并留下错误，比"少跑一步但看起来成功"好。
            return {
                "route": _ROUTE_FINALIZE,
                "errors": [f"计划与状态不一致：找不到 step_id={target}"],
            }

        if step.get("approval_required") and target not in set(state.get("approved_steps", [])):
            # 审批请求由 hub 准备好，approval_gate 只负责中断。
            #
            # **必须同时写 pending_step**：route 接下来会被置成哨兵值
            # ``__approval__``（条件边只能读那个），审批闸口拿 route 当 step_id 的话
            # 会把批准结果记到哨兵头上，于是 hub 下次还会判"这一步没批" ——
            # 表现为无限弹同一个审批。
            return {
                "route": _ROUTE_APPROVAL,
                "pending_step": target,
                "pending_approval": True,
                "approval": ApprovalRequest(
                    node=APPROVAL_GATE,
                    action=f"执行「{step.get('goal') or target}」（{step.get('agent')}）",
                    reason=(
                        f"步骤 {target} 被标记为需要人工确认。"
                        "它会产生对外不可撤销的副作用（发邮件 / 上架 / 改预算）。"
                    ),
                    risk="medium",
                    payload={"step_id": target, "agent": step.get("agent"), "goal": step.get("goal")},
                    requested_by="orchestrator",
                ),
            }

        return {"route": target, "pending_step": target}

    return hub_node


def make_approval_gate_node() -> Any:
    """人工确认闸口。用 ``interrupt()`` 暂停整个图。

    ``interrupt()`` 的返回值就是 ``Command(resume=...)`` 里传进来的东西。
    人确认后重新 invoke，执行会**从这里继续** —— 前面的节点不会重跑
    （状态在 checkpoint 里），这一点和"重跑整个任务"有本质区别。
    """

    def approval_gate_node(state: TaskState) -> dict[str, Any]:
        # 从 pending_step 取，**不能从 route 取** —— route 这时是哨兵值
        step_id = str(state.get("pending_step") or "")
        request = state.get("approval")

        decision = interrupt(
            {
                "kind": "approval_required",
                "step_id": step_id,
                "question": request.action if request else f"是否执行步骤 {step_id}？",
                "reason": request.reason if request else "",
                "payload": request.payload if request else {},
                "how_to_answer": "resume 时传 {'approved': true/false, 'by': '人名', 'reason': '...'}",
            }
        )

        approved = _read_approved(decision)
        by = ""
        reason = ""
        if isinstance(decision, dict):
            by = str(decision.get("by") or "")
            reason = str(decision.get("reason") or "")

        if approved:
            return {
                "approved_steps": [step_id],
                "pending_approval": False,
                "approval": None,
                "result": {
                    "approval": {"step_id": step_id, "approved": True, "by": by},
                },
            }

        # 被驳回：任务到此为止。
        #
        # **不要把这一步记进 completed_steps** —— 它没跑过，记进去会让轨迹
        # 看起来"做完了"。之所以不需要这个标记也不会重复弹审批，是因为下面
        # 设了 status=cancelled，hub 开头看到终态就直接去 finalize。
        #
        # 关键是不能让下游步骤接着跑：上一步没做，下游的输入是空的，
        # 硬跑出来的是"看起来成功但其实基于缺失数据"的结论。
        return {
            "pending_approval": False,
            "approval": None,
            "status": "cancelled",
            "errors": [f"人工驳回了步骤 {step_id}：{reason or '未说明理由'}"],
            "result": {
                "approval": {"step_id": step_id, "approved": False, "by": by, "reason": reason}
            },
        }

    return approval_gate_node


def _read_approved(decision: Any) -> bool:
    """把 resume 传进来的东西解释成"批没批"。

    容忍多种写法（``True`` / ``{"approved": true}`` / ``"yes"``）：
    调用方可能是前端、可能是脚本、可能是人手动 resume，
    因为格式不同就拒绝执行，是把接口设计的洁癖转嫁给了用户。
    """
    if isinstance(decision, bool):
        return decision
    if isinstance(decision, dict):
        value = decision.get("approved", decision.get("approve"))
        if isinstance(value, bool):
            return value
        return str(value).lower() in {"1", "true", "yes", "y", "ok", "approve", "approved"}
    if isinstance(decision, str):
        return decision.strip().lower() in {"1", "true", "yes", "y", "ok", "approve", "approved"}
    return False


def make_finalize_node() -> Any:
    """收尾：汇总结果、落终态。"""

    def finalize_node(state: TaskState) -> dict[str, Any]:
        completed = list(state.get("completed_steps", []))
        failed = list(state.get("failed_steps", []))
        plan = state.get("plan") or {}
        planned = [s.get("step_id") for s in plan.get("steps", [])]
        skipped = [s for s in planned if s not in completed]

        status = state.get("status") or "running"
        if status in _TERMINAL:
            final_status = status
        elif failed:
            # 有步骤失败 → 任务算失败。
            # 不标 succeeded 的理由：任务是"达成了用户的意图"还是"没达成"，
            # 与"过程里有没有出错"是同一件事。把失败的任务标成功，
            # 监控面板上的成功率就没意义了。
            final_status = "failed"
        else:
            final_status = "succeeded"

        summary = _build_summary(state, completed, skipped, final_status)

        return {
            "current_node": FINALIZE,
            "status": final_status,
            "progress": 100,
            "finished_at": utcnow(),
            "result": {
                **(state.get("result") or {}),
                "summary": summary,
                "plan": plan_as_text(plan),
                "completed_steps": completed,
                "failed_steps": failed,
                "skipped_steps": skipped,
                "artifact_refs": [a.ref for a in state.get("artifacts", [])],
                # 带上下载地址与文件名：前端拿这个直接渲染下载链接。
                # 只给 asset_no 的话前端要么再查一次接口、要么自己拼 URL ——
                # 而 URL 的拼法属于存储层细节，不该溢出到前端
                "artifacts": [
                    {
                        "kind": a.kind,
                        "name": a.name,
                        "ref": a.ref,
                        "url": (a.meta or {}).get("url", ""),
                        "size_bytes": (a.meta or {}).get("size_bytes"),
                    }
                    for a in state.get("artifacts", [])
                ],
                "step_count": len(state.get("steps", [])),
                "llm_calls": state.get("llm_calls", 0),
                "total_tokens": state.get("total_tokens", 0),
                "total_cost": float(state.get("total_cost") or 0),
            },
        }

    return finalize_node


def _build_summary(
    state: TaskState, completed: list[str], skipped: list[str], status: str
) -> str:
    parts: list[str] = []
    if status == "cancelled":
        parts.append("任务已被人工取消")
    elif status == "failed":
        failed = state.get("failed_steps") or []
        parts.append(f"任务失败（{', '.join(failed)} 未完成）")
    else:
        parts.append(f"已完成 {len(completed)} 个步骤")

    if skipped:
        parts.append(f"跳过：{', '.join(skipped)}")

    note = (state.get("findings") or {}).get("notification") or {}
    if note.get("status") == "blocked":
        parts.append(f"邮件被策略拦截（{note.get('decision')}），需人工处理")

    artifacts = state.get("artifacts") or []
    if artifacts:
        parts.append(f"产出 {len(artifacts)} 个文件")
    return "；".join(parts)


def _now() -> Any:
    return utcnow()


# ---------------------------------------------------------------------------
# 构图
# ---------------------------------------------------------------------------
def build_orchestrator(
    ctx: RunContext,
    *,
    checkpointer: Any = None,
    builders: dict[str, Any] | None = None,
) -> Any:
    """构建并编译编排图。

    ``ctx`` 是**总控自己**的上下文（用于 plan 节点）；
    每个子 Agent 会用**自己的权限集**新建 ``RunContext`` ——
    最小权限原则，不让通知 Agent 顺手拿到 ERP 写权限。
    """
    from fashionops.agents.state import RunContext as _RC
    from fashionops.tools import AGENT_SCOPES

    builders = builders or agent_node_builders()

    def ctx_for(agent: str) -> Any:
        return _RC(
            gateway=ctx.gateway,
            model=ctx.model,
            actor=f"{agent}_agent",
            scopes=AGENT_SCOPES.get(agent, frozenset()),
            task_id=ctx.task_id,
            router=ctx.router,
        )

    graph = StateGraph(TaskState)
    graph.add_node(PLAN, make_plan_node(ctx))
    graph.add_node(HUB, make_hub_node())
    graph.add_node(APPROVAL_GATE, make_approval_gate_node())
    graph.add_node(FINALIZE, make_finalize_node())
    for agent, factory in builders.items():
        graph.add_node(agent, _wrap_agent(agent, factory(ctx_for(agent))))

    graph.add_edge(START, PLAN)
    graph.add_edge(PLAN, HUB)
    graph.add_edge(FINALIZE, END)
    for agent in builders:
        graph.add_edge(agent, HUB)
    graph.add_edge(APPROVAL_GATE, HUB)

    # 条件边的**返回值必须是节点名**（或 END）。
    # 这里只做查表，不做决策 —— 决策在 hub 里，已经写进 state["route"] 了。
    destinations = [*builders.keys(), APPROVAL_GATE, FINALIZE]
    graph.add_conditional_edges(HUB, _route_from_state, destinations)

    return graph.compile(checkpointer=checkpointer) if checkpointer else graph.compile()


def _route_from_state(state: TaskState) -> str:
    """读 ``route`` 决定下一个节点。纯查表，不写状态。"""
    route = state.get("route")
    if not route or route in (_ROUTE_FINALIZE,):
        return FINALIZE
    if route == _ROUTE_APPROVAL:
        return APPROVAL_GATE
    step = step_of(state, str(route))
    if step is None:
        return FINALIZE
    agent = step.get("agent")
    return str(agent) if agent else FINALIZE


__all__ = [
    "APPROVAL_GATE",
    "FINALIZE",
    "HUB",
    "PLAN",
    "agent_node_builders",
    "build_orchestrator",
]
