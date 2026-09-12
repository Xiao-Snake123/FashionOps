"""任务规划 —— 把一句话需求拆成可执行的 DAG。

## 为什么"模型出计划"必须配"代码校验计划"

让模型自由生成执行计划是很有吸引力的 —— 灵活、像"真正的 Agent"。
但它会稳定地产出**跑不通的计划**，而且是静默的：

* 编出一个不存在的 Agent（``"image_agent"``、``"report_agent"``）
* ``depends_on`` 指向一个根本没定义的 step_id
* 步骤之间形成环（A 等 B、B 等 A）→ 图跑不起来，表现为"卡住"
* 同一个 Agent 出现两次 → 我们的图是**静态构建**的，一个 Agent 一个节点，
  路由到时候分不清"现在该跑第几次"

这些都不会让模型自己察觉 —— 它以为计划是合理的。
所以这里是**两道关**：

    模型自由发挥  →  Pydantic 校验（字段类型/长度）
                  →  语义校验（validate_plan：Agent 存在、依赖存在、无环、有入口）
                  →  不过就带着**具体问题**回喂重试
                  →  还不过就回退到代码生成的默认计划

## 为什么最后要兜底而不是报错

"模型规划失败"不应该让整个任务失败。用户要的是"选 3 款女装并出报表"，
这件事的链路是**已知且固定**的 —— 规划只是让它更灵活。
所以兜底计划按关键词做确定性拆分（``default_plan_for``），
**任务照样能跑完**，只是在轨迹里标注"用了兜底计划"。

这条原则值得单独说：**规划是优化项，不是关键路径**。
把它做成关键路径，等于让每次任务都有概率因为一句没写好而全盘失败。

## 已知：mock 模式下规划**必然**走兜底

``MockModelClient`` 只保证"schema 合法"，不保证"语义合法" —— 它不可能知道
``depends_on`` 要指向另一个 step 的 id，也不可能知道多个 step 的 ``agent`` 要互不相同。
所以 mock 生成的计划稳定地报 ``duplicate_agent`` / ``unknown_dependency``，
然后走兜底链路。**这是设计内的降级，不是 bug**：

* 用 ``--mock`` 跑演示时，看到"规划回退到默认链路"是正常的
* 要测**非兜底**路径，就用 ``MockModelClient(script=[...])`` 脚本化一份合法计划，
  或者直接调 ``build_plan`` / ``validate_plan``（它们都是纯函数，好测）
* 真实模型走的是 JSON schema 约束（``agent`` 已经是 Literal），约束在结构层就生效
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field

from fashionops.agents.state import (
    RunContext,
    TaskState,
    check_budget,
    step_update,
)
from fashionops.llm import Message, structured_call

#: 已经有节点实现的 Agent。
#:
#: **做成 Literal 而不是普通 str**，这样约束会进到暴露给模型的 JSON schema 里 ——
#: 模型在**结构层面**就编不出不存在的 Agent 名。比"生成完再校验然后回喂重试"
#: 少一轮往返，而且 mock 生成器也能自动产出合法值。
#:
#: ``validate_plan`` 里的 ``unknown_agent`` 检查仍然保留，用来兜从 checkpoint
#: 读回的外部计划 —— 那时已经不过 schema 了。
AgentName = Literal["selection", "analytics", "notification"]

EXECUTABLE_AGENTS: tuple[str, ...] = get_args(AgentName)

#: 单次任务的步骤数上限。既是成本控制，也是合理性检查 ——
#: 一句话需求拆出 12 步，大概率是模型在凑数。
MAX_STEPS = 6


# ---------------------------------------------------------------------------
# 计划契约
# ---------------------------------------------------------------------------
class PlannedStep(BaseModel):
    step_id: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^[a-z][a-z0-9_]*$",
        description="步骤唯一标识，小写英文/数字/下划线，如 selection、fetch_data",
    )
    agent: AgentName = Field(description="执行该步骤的 Agent")
    goal: str = Field(
        max_length=200, description="这一步要达成什么，一句话。会在界面上显示给运营看"
    )
    depends_on: list[str] = Field(
        default_factory=list, description="依赖的 step_id 列表。没有依赖就是入口步骤"
    )
    approval_required: bool = Field(
        default=False,
        description=(
            "是否需要人工确认后才执行。**有对外不可撤销副作用的步骤必须设为 true**"
            "（发邮件、商品上架、改广告预算）"
        ),
    )


class TaskPlan(BaseModel):
    reasoning: str = Field(description="一句话说明你为什么这么拆")
    steps: list[PlannedStep] = Field(min_length=1, max_length=MAX_STEPS)


@dataclass(frozen=True)
class PlanIssue:
    """一条计划问题。带 hint 是为了让重试真的能改对。"""

    code: str
    message: str
    hint: str

    def to_prompt_line(self) -> str:
        return f"- [{self.code}] {self.message} → {self.hint}"


_SYSTEM = (
    "你是任务规划器。把用户的一句话需求拆成一张执行 DAG。\n"
    "硬性要求：\n"
    "1. `agent` 只能从下面给定的可用 Agent 列表里选，**不要发明新的 Agent 名**\n"
    "2. 每个 step_id 唯一，小写英文；同一 Agent 只能出现一次\n"
    "3. `depends_on` 里的 id 必须是计划里已存在的 step_id，不能有环\n"
    "4. 必须至少有一个无依赖的入口步骤\n"
    "5. **有对外不可撤销副作用的步骤必须 approval_required=true** ——"
    "发邮件、商品上架、改广告预算属于这一类；纯查询和生成报表不需要\n"
    "6. 不要为了凑数拆步骤。用户只要一件事就只写一步\n"
    "7. 步骤顺序要符合业务逻辑：先有数据，再分析，最后才对外发通知\n"
)


# ---------------------------------------------------------------------------
# 语义校验（纯函数，好测）
# ---------------------------------------------------------------------------
def requested_agents(user_request: str) -> list[str]:
    """按关键词推断用户**明确要求**了哪些能力。

    用途不是"替模型做规划"，而是当模型的计划明显漏掉用户点名要的东西时
    把它拦回去重做。真实碰到过：用户说"选 2 款女装并出报表发给 ops@x.com"，
    模型把"选品 + 分析"合并成一个步骤（agent 只写了 selection），
    结果报表没生成、下游节点报"缺少上游结论"失败。

    这种漏拆在结构上完全合法（无环、依赖存在、Agent 名合法），
    单靠 schema 校验抳不住 —— 但用关键词检测是一个**确定性**的判断，
    比再问一次模型"你确定吗"可靠得多。
    """
    return [
        agent
        for agent, patterns in _KEYWORDS.items()
        if any(_matches_request(p, user_request) for p in patterns)
    ]


def validate_plan(
    plan: TaskPlan,
    *,
    executable: tuple[str, ...] = EXECUTABLE_AGENTS,
    max_steps: int = MAX_STEPS,
    user_request: str | None = None,
) -> list[PlanIssue]:
    """校验计划的**语义**合法性。返回空列表表示通过。

    这里检查的都是"Pydantic 管不到、但会让图跑不通"的问题。
    """
    issues: list[PlanIssue] = []
    steps = plan.steps

    if not steps:
        return [PlanIssue("empty_plan", "计划里没有任何步骤", "至少给出一个步骤")]
    if len(steps) > max_steps:
        issues.append(
            PlanIssue(
                "too_many_steps",
                f"步骤数 {len(steps)} 超过上限 {max_steps}",
                "合并可以一次做完的步骤，或只保留必要的",
            )
        )

    ids = [s.step_id for s in steps]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        issues.append(
            PlanIssue("duplicate_step_id", f"step_id 重复：{', '.join(dupes)}", "每个 step_id 必须唯一")
        )

    # 同一 Agent 只能出现一次：图是静态构建的，一个 Agent 一个节点，
    # 出现两次的话路由函数分不清"现在该跑第几次"
    agents = [s.agent for s in steps]
    dup_agents = sorted({a for a in agents if agents.count(a) > 1})
    if dup_agents:
        issues.append(
            PlanIssue(
                "duplicate_agent",
                f"同一个 Agent 被用了多次：{', '.join(dup_agents)}",
                "每个 Agent 最多出现一次；一次做完的活儿合并成一个步骤",
            )
        )

    unknown = sorted({a for a in agents if a not in executable})
    if unknown:
        issues.append(
            PlanIssue(
                "unknown_agent",
                f"不存在的 Agent：{', '.join(unknown)}",
                f"只能从这些里选：{', '.join(executable)}",
            )
        )

    known_ids = set(ids)
    for step in steps:
        for dep in step.depends_on:
            if dep == step.step_id:
                issues.append(
                    PlanIssue(
                        "self_dependency",
                        f"步骤 {step.step_id} 依赖自己",
                        "去掉这个自依赖",
                    )
                )
            elif dep not in known_ids:
                issues.append(
                    PlanIssue(
                        "unknown_dependency",
                        f"步骤 {step.step_id} 依赖了不存在的 {dep}",
                        f"depends_on 只能是这些之一：{', '.join(sorted(known_ids))}",
                    )
                )

    # 只有上面都没问题时再查环和入口，否则会产出一堆重复且看不懂的报错
    if not issues:
        order = topological_order(steps)
        if order is None:
            issues.append(
                PlanIssue(
                    "cycle",
                    "步骤之间存在循环依赖，图跑不起来",
                    "检查 depends_on，改成单向依赖（先查询 → 再分析 → 最后通知）",
                )
            )
        elif not graph_has_entry(steps):
            issues.append(
                PlanIssue(
                    "no_entry",
                    "没有无依赖的入口步骤，不知道从哪里开始",
                    "至少让一个步骤的 depends_on 为空",
                )
            )

    # 用户点名要的能力不能被计划漏掉 —— 见 ``requested_agents`` 的说明
    if not issues and user_request:
        wanted = requested_agents(user_request)
        planned = {s.agent for s in steps}
        missing = [a for a in wanted if a not in planned]
        if missing:
            duties = {
                "selection": "拉候选商品并筛选打分",
                "analytics": "聚合销售数据并生成报表",
                "notification": "把结论写成邮件发出去",
            }
            issues.append(
                PlanIssue(
                    "missing_requested_agent",
                    f"用户需求里提到了 {', '.join(duties[a] for a in missing)}"
                    f"（对应 Agent：{', '.join(missing)}），但计划里没有这一步",
                    "把这些能力各拆成独立步骤，不要和别的步骤合并；"
                    "一个步骤只能由一个 Agent 执行",
                )
            )
    return issues


def topological_order(steps: list[PlannedStep]) -> list[str] | None:
    """Kahn 拓扑排序。有环返回 ``None``。

    顺带产出执行顺序 —— 所有依赖都满足的步骤按**计划里的原始顺序**选，
    这样同一份计划每次跑出来的顺序完全一样（可复现）。
    """
    pending = {s.step_id: set(s.depends_on) for s in steps}
    order: list[str] = []
    while pending:
        ready = [s.step_id for s in steps if s.step_id in pending and not pending[s.step_id]]
        if not ready:
            return None  # 剩下的都有依赖 → 有环
        for step_id in ready:
            order.append(step_id)
            del pending[step_id]
        for deps in pending.values():
            deps.difference_update(ready)
    return order


def graph_has_entry(steps: list[PlannedStep]) -> bool:
    return any(not s.depends_on for s in steps)


# ---------------------------------------------------------------------------
# 兜底计划
# ---------------------------------------------------------------------------
#: 关键词 → Agent。用的是"运营真的会怎么说话"，不是技术术语。
#:
#: **用正则而不是纯子串**：运营说"帮我选 3 款女装"时中间夹了数字和空格，
#: 纯子串匹配 ``"选款"`` 命中不了 —— 结果就是兜底计划把选品那一步丢掉，
#: 而用户明明要的就是选品。
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "selection": (r"选\s*\d*\s*款", "选品", "挑款", "挑选", "上新", "款式"),
    "analytics": ("报表", "分析", "数据", "日报", "周报", "月报", "经营", "指标"),
    "notification": ("发给", "邮件", "通知", "告知", "汇报"),
}


def _matches_request(pattern: str, request: str) -> bool:
    return re.search(pattern, request) is not None


def default_plan_for(user_request: str) -> TaskPlan:
    """按关键词做确定性拆分。模型规划失败时的兜底。

    故意做得**保守**：只认明确出现的关键词，认不出来就给"选品 + 数据分析"
    这个最安全的组合（纯只读 + 生成文件，没有任何对外副作用）。
    """
    picked = [
        agent
        for agent, patterns in _KEYWORDS.items()
        if any(_matches_request(p, user_request) for p in patterns)
    ]
    if not picked:
        picked = ["selection", "analytics"]

    # 固定成业务上的正确顺序，不按关键词出现的先后
    order = [a for a in EXECUTABLE_AGENTS if a in picked]

    goals = {
        "selection": "筛选出符合需求的候选款式",
        "analytics": "汇总经营数据并产出分析报表",
        "notification": "把结论与报表通知给相关人",
    }
    steps: list[PlannedStep] = []
    prev: str | None = None
    for agent in order:
        steps.append(
            PlannedStep(
                step_id=agent,
                agent=agent,
                goal=goals[agent],
                depends_on=[prev] if prev else [],
                # 兜底计划按代码规则判定副作用，不靠模型自觉：
                # 通知会发邮件出去，属于不可撤销的对外动作
                approval_required=(agent == "notification"),
            )
        )
        prev = agent

    return TaskPlan(
        reasoning="模型未能给出合法计划，按需求关键词回退到默认链路：" + " → ".join(order),
        steps=steps,
    )


# ---------------------------------------------------------------------------
# 节点工厂
# ---------------------------------------------------------------------------
@dataclass
class PlanOutcome:
    plan: TaskPlan
    attempts: int
    issues: list[PlanIssue]
    used_fallback: bool
    tokens: int
    cost: Decimal
    llm_calls: int


def make_plan_node(
    ctx: RunContext,
    *,
    max_repair: int = 2,
    executable: tuple[str, ...] = EXECUTABLE_AGENTS,
) -> Callable[[TaskState], dict[str, Any]]:
    """构造规划节点。"""

    def plan_node(state: TaskState) -> dict[str, Any]:
        started = time.perf_counter()
        check_budget(state)

        outcome = build_plan(ctx, state, max_repair=max_repair, executable=executable)

        plan = outcome.plan
        progress = 5
        findings = {
            **state.get("findings", {}),
            "plan": {
                "reasoning": plan.reasoning,
                "steps": [s.model_dump() for s in plan.steps],
                "used_fallback": outcome.used_fallback,
                "attempts": outcome.attempts,
            },
        }

        extra: dict[str, Any] = {
            "plan": plan.model_dump(),
            "findings": findings,
            "status": "running",
            "progress": progress,
        }

        if outcome.used_fallback:
            summary = f"规划回退到默认链路（{len(plan.steps)} 步）：{plan.reasoning}"
            extra["errors"] = [
                *state.get("errors", []),
                "任务规划失败，已回退到默认链路："
                + "；".join(i.message for i in outcome.issues[:3]),
            ]
        else:
            summary = f"已拆解为 {len(plan.steps)} 步：" + " → ".join(s.agent for s in plan.steps)

        return step_update(
            state,
            node="plan",
            agent="orchestrator",
            status="succeeded",
            summary=summary,
            duration_ms=_ms(started),
            tokens=outcome.tokens,
            cost=outcome.cost,
            llm_calls=outcome.llm_calls,
            progress=progress,
            extra=extra,
        )

    return plan_node


def build_plan(
    ctx: RunContext,
    state: TaskState,
    *,
    max_repair: int = 2,
    executable: tuple[str, ...] = EXECUTABLE_AGENTS,
) -> PlanOutcome:
    """调模型出计划 → 校验 → 有问题就带着问题回喂重试 → 都不行就兜底。

    单独抽出来是为了能在不构图的情况下直接测这段循环。
    """
    request = state.get("user_request", "")
    tokens = 0
    cost = Decimal("0")
    calls = 0

    messages = [
        Message.system(_SYSTEM),
        Message.user(
            f"可用 Agent 列表：{', '.join(executable)}\n"
            f"各 Agent 的职责：\n"
            f"- selection：拉候选商品、按硬约束过滤、多维打分排序，产出候选清单\n"
            f"- analytics：聚合销售数据、算指标告警、生成 Excel 报表\n"
            f"- notification：把结论和附件写成邮件发给相关人\n\n"
            f"用户需求：{request}"
        ),
    ]

    issues: list[PlanIssue] = []
    for attempt in range(1, max_repair + 2):
        result = structured_call(ctx.model, messages, TaskPlan)
        calls += 1
        tokens += result.response.usage.total_tokens
        cost += result.response.cost

        if not result.ok:
            issues = [
                PlanIssue("invalid_output", "结构化输出校验失败", "严格按给定 schema 返回 JSON")
            ]
        else:
            plan = result.value
            assert isinstance(plan, TaskPlan)
            issues = validate_plan(plan, executable=executable, user_request=request)
            if not issues:
                return PlanOutcome(
                    plan=plan,
                    attempts=attempt,
                    issues=[],
                    used_fallback=False,
                    tokens=tokens,
                    cost=cost,
                    llm_calls=calls,
                )

        # 把**具体问题**回喂，而不是只说"你的计划不对"。
        # 只说不对的话模型会原地重试一次差不多的输出。
        if attempt <= max_repair:
            messages = [
                *messages,
                Message.assistant(result.response.content or "{}"),
                Message.user(
                    "上面这份计划有问题，请修正后重新给出完整计划：\n"
                    + "\n".join(i.to_prompt_line() for i in issues[:6])
                    + "\n\n只返回修正后的 JSON。"
                ),
            ]

    return PlanOutcome(
        plan=default_plan_for(request),
        attempts=max_repair + 1,
        issues=issues,
        used_fallback=True,
        tokens=tokens,
        cost=cost,
        llm_calls=calls,
    )


def blocked_steps(steps: list[dict[str, Any]], failed: set[str]) -> set[str]:
    """失败步骤的**传递下游**：直接或间接依赖失败步骤的都要跳过。

    为什么必须传递而不是只看直接依赖：
    A 失败 → B 依赖 A → C 依赖 B。只看直接依赖的话 C 会被认为"可以跑"，
    因为 C 的依赖 B 既没成功也没失败（它被跳过了）—— 于是 C 带着
    空输入跑出一个看起来正常的结果。

    返回的不包含 ``failed`` 本身（那是"失败"，不是"被牵连"）。
    """
    deps = {s.get("step_id"): set(s.get("depends_on") or []) for s in steps}
    blocked = set(failed)
    changed = True
    while changed:
        changed = False
        for step_id, need in deps.items():
            if step_id in blocked or not step_id:
                continue
            if need & blocked:
                blocked.add(step_id)
                changed = True
    return blocked - set(failed)


def plan_route_target(
    state: TaskState, completed: set[str], *, blocked: set[str] | None = None
) -> str | None:
    """按计划算出下一个该执行的 step_id。

    已完成的、以及被上游失败牵连的步骤都会跳过；
    没有可执行的了就返回 ``None``（交给 hub 去 finalize）。
    """
    blocked = blocked or set()
    plan = state.get("plan") or {}
    for step in plan.get("steps", []):
        step_id = step.get("step_id")
        if not step_id or step_id in completed or step_id in blocked:
            continue
        if all(d in completed for d in step.get("depends_on", [])):
            return step_id
    return None


def step_of(state: TaskState, step_id: str) -> dict[str, Any] | None:
    for step in (state.get("plan") or {}).get("steps", []):
        if step.get("step_id") == step_id:
            return step
    return None


def plan_as_text(plan: dict[str, Any] | None) -> str:
    """把计划渲染成一行文字，给日志和界面用。"""
    if not plan:
        return "（无计划）"
    parts = []
    for step in plan.get("steps", []):
        mark = "*" if step.get("approval_required") else ""
        parts.append(f"{step.get('step_id')}{mark}")
    return " → ".join(parts)


def normalize_dag(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """把库里的 DAG 统一成一种形状，让前端只处理一种。

    库里存着**两种历史形状**：

    * 旧（seed 灌的演示任务）：``{nodes: [{id, name, agent, interrupt}], edges: [[from, to]]}``
    * 新（W4 起的真任务）：``{reasoning, steps: [{step_id, agent, goal, depends_on, approval_required}]}``

    兼容逻辑放后端而不是前端，理由是：前端一旦要同时认两种形状，
    每处渲染都得写 ``if`` 分支；以后要加第三种形状时改的地方会翻倍。

    归一化放在 ``TaskOut`` 的校验器里（**所有出口共用**），而不是
    只在详情接口里调一次 —— 否则列表和详情会返回两种形状，
    前端还是得写两套。

    不改库数据：已写入的行改造要写迁移脚本，而且旧演示任务早晚会被清掉，
    在读取时兼容成本最低。
    """
    if not raw:
        return None
    if "steps" in raw:
        return raw  # 已经是新形状

    nodes = raw.get("nodes") or []
    edges = raw.get("edges") or []
    if not nodes:
        return raw

    incoming: dict[str, list[str]] = {}
    for edge in edges:
        if isinstance(edge, (list, tuple)) and len(edge) == 2:
            incoming.setdefault(str(edge[1]), []).append(str(edge[0]))

    steps = [
        {
            "step_id": n.get("id"),
            "agent": n.get("agent") or "",
            # 旧形状的 name 是给人看的步骤名，对应新形状的 goal
            "goal": n.get("name") or "",
            "depends_on": incoming.get(str(n.get("id")), []),
            "approval_required": bool(n.get("interrupt")),
        }
        for n in nodes
    ]
    return {
        "reasoning": "（历史任务：由 seed 生成的演示数据，已归一化为新形状）",
        "steps": steps,
        "legacy": True,
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


__all__ = [
    "EXECUTABLE_AGENTS",
    "MAX_STEPS",
    "AgentName",
    "PlanIssue",
    "PlanOutcome",
    "PlannedStep",
    "TaskPlan",
    "blocked_steps",
    "build_plan",
    "default_plan_for",
    "graph_has_entry",
    "make_plan_node",
    "normalize_dag",
    "plan_as_text",
    "plan_route_target",
    "requested_agents",
    "step_of",
    "topological_order",
    "validate_plan",
]
