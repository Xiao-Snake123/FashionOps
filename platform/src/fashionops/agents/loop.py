"""任务执行循环 —— **一个 Agent + 显式 todo 列表**。

这是新架构的执行层，替代原来的「LangGraph 图 + hub 路由 + 三个专用节点」
（见 docs/12-AI协作交接.md §5）。原来的机器长这样：

```
plan(模型) → hub(按 DAG 拓扑选下一步) → selection/analytics/notification → finalize
```

现在长这样：

```
plan_todos(代码) → for todo in todos: 同一个 Agent 跑一步 → finalize
```

## 为什么可以这么简单

原架构的三样东西都是为「计划是模型出的 DAG」服务的：

* **hub 路由** —— 因为执行顺序由计划决定，所以要有个中心节点按拓扑选下一步
* **图校验 / 环检测 / 传递阻断** —— 因为模型给的图可能不成图
* **三个专用节点** —— 因为一个节点对应一种"角色"

计划改由代码按关键词产出（`workplan.plan_todos`）、且是**线性列表**之后：
顺序就是 seq 从小到大，不存在路由；不可能有环；"选品那步"和"分析那步"
只是同一个 Agent 的两次调用，区别只在**工具目录**和**目标描述**。

## 为什么每一步都要重新裁工具目录

`TaskTodo.kind` 决定这一步能看到哪些工具（`KIND_TOOLS`）。
选品那一步看不到 `mail.send` —— 这不是权限（权限另有求交），
而是**注意力**：给模型 11 个工具和给它 2 个工具，它选错工具的概率差一个量级。
原来靠"每个 Agent 一个提示词"，现在靠"每一步一个目录"，效果一样但没有角色。

## 审批为什么不阻塞线程（交接文档 D6）

跑到需要人工确认的工具（`needs_approval`）时，**不执行**，而是：

1. 落一张审批单（工具名 + 完整入参 + 人话摘要）
2. todo 状态置 ``waiting_approval``，任务置 ``waiting_approval``
3. **这一趟就结束了** —— 函数返回，线程释放

人批准后由 `resume_task` 重新开跑（新线程），从那张审批单里取回
"当时要调什么工具、参数是什么"。原架构用 LangGraph 的 ``interrupt()`` +
checkpoint 挂住整个图，代价是：线程/状态一直悬着、必须依赖 checkpointer、
而且 resume 的语义（``Command(resume=...)``）只有 LangGraph 懂。
这里换成"落库 + 重新开跑"，需要持久化的东西只剩一张表的一行。

**"重新开跑"不会重复做已经做完的事**：todo 状态是 ``done`` 的会被跳过，
所以恢复成本是 O(未完成的步骤)，不是 O(整个任务)。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from sqlalchemy import delete, select

from fashionops.agents.workplan import TodoSpec, plan_payload, plan_todos
from fashionops.approval import records as approval_records
from fashionops.approval.policy import needs_approval
from fashionops.db import session_scope
from fashionops.db.base import utcnow
from fashionops.db.models import (
    TODO_STATUS_DONE,
    TODO_STATUS_FAILED,
    TODO_STATUS_PENDING,
    TODO_STATUS_RUNNING,
    TODO_STATUS_SKIPPED,
    TODO_STATUS_WAITING,
    Task,
    TaskTodo,
)
from fashionops.gateway.core import CallContext, ToolGateway
from fashionops.gateway.registry import from_llm_name, registry

logger = logging.getLogger("fashionops.agents.loop")

#: 执行 Agent 的名字。它同时是 ``agent_runs.agent_name`` 和
#: ``AGENT_SCOPES`` 的键 —— 新架构里任务侧只有这一个 Agent。
TASK_AGENT_NAME = "task"

#: 单步最多几轮工具调用。和原节点的 8 步保持一致：
#: 真实的选品/分析步骤（查候选 → 打分 → 必要时补查）用不到 8 轮，
#: 超过说明模型在绕圈，必须硬性截断，否则就是拿 token 打水漂。
MAX_STEPS_PER_TODO = 8

#: **这一步必须调用的工具**，不调用就算没做完。
#:
#: 为什么要这条硬规则：实测 glm-4-flash 在通知步骤里**稳定地不调** mail.send
#: —— 它把邮件正文写得漂漂亮亮，然后说"已起草"，一步就结束了。
#: 于是任务被标成 succeeded、收件人什么也没收到，用户以为发出去了。
#: 更糟的是它顺手编了一个 asset_no 当附件。
#:
#: 这不是"提示词写清楚"能解决的（写清楚了它也不调），所以改成代码兜底：
#: 少了这个工具就再提醒一次；还不调就**判失败** ——
#: 宁可让任务失败并写明原因，也不能报一个假的成功。
_KIND_REQUIRED_TOOL: dict[str, str] = {"notification": "mail.send"}

#: 上一步结论进下一步上下文时截断到多少字。
#: 不截断的话，出报表那步会把选品全表的 JSON 全背进 prompt，
#: 而它真正需要的只是"选了几款、报表在哪"这类结论。
_CONTEXT_SUMMARY_CHARS = 800

#: 每类工作能看到哪些工具。**kind 决定目录**（见模块 docstring）。
#: 顺序即模型看到的顺序 —— 把"先查再算"的顺序写进目录列表，比写进提示词可靠。
KIND_TOOLS: dict[str, tuple[str, ...]] = {
    # 选品：拉候选 → 打分排序。理由文字由模型自己写（那是它的强项），
    # 但分数和"为什么给高分"必须来自 selection.rank_candidates。
    "selection": (
        "erp.query_candidates",
        "selection.rank_candidates",
        "erp.query_inventory",
    ),
    # 分析：拉汇总 → 阈值判告警 → 出报表。
    # 注意这里**没有** report.export_excel：报表要走 render_business_report，
    # 它把列和数字格式都定好了，模型不用（也不该）自己拼三张表。
    "analytics": (
        "erp.query_sales_summary",
        "analytics.detect_alerts",
        "analytics.render_business_report",
    ),
    # 通知：只有发信。起草邮件正文是模型的事，发送是工具的事。
    "notification": ("mail.send",),
    # 认不出意图时的兜底：只给只读工具。
    # 单步 generic 通常是"查个东西"，给它写工具只会让它乱动。
    "generic": (
        "erp.query_candidates",
        "erp.query_inventory",
        "erp.query_sales_summary",
        "kb.search",
    ),
}

_KIND_GUIDE: dict[str, str] = {
    "selection": (
        "先调 erp.query_candidates 拉候选商品（按用户给的条件传参，limit 用默认值即可），"
        "再把返回的 candidates **原样**传给 selection.rank_candidates 打分排序"
        "（top_n 按用户要求的款数）。\n"
        "结论里列出选中的款式：spu_code、总分、一两句推荐理由。"
        "理由必须引用打分返回里的具体数字（销量、毛利率、库存），**不要自己造数字**。"
        "如果有商品被硬约束挡掉，简单说明挡掉的原因。"
    ),
    "analytics": (
        "先调 erp.query_sales_summary 分别按 group_by=category 和 group_by=channel 查一次"
        "（days 用用户说的时间范围，没说就用 30）。\n"
        "把 totals 和两个 groups 传给 analytics.detect_alerts 拿告警结论，"
        "再调 analytics.render_business_report 生成 Excel 报表（把两次查询结果原样传进去）。\n"
        "结论里必须包含：整体 GMV、订单数、最值得注意的一到两条告警（引用工具给的数值和 level）、"
        "以及报表的 asset_no 和下载地址。**严重程度以工具返回的 level 为准，不要自己判断**。"
    ),
    "notification": (
        "用中文起草一封给相关人的邮件：说清楚背景、结论，有报表就把 asset_no / URL 写进去。\n"
        "**必须调 mail.send 真正发送** —— 只把邮件内容写出来不算完成这一步"
        "（实测过：模型写了一封措辞完整的邮件就说「已起草」，任务被标成成功，"
        "而收件人什么也没收到，用户以为发出去了）。\n"
        "正文里的**每个数字都必须来自前面步骤的结论或工具返回**；"
        "这一步手上没有数据，就如实写「本次没有需要核对的数据」，**不要编数字**。\n"
        "系统会在这次发送真正执行前拦下等人工确认 —— 那是设计，不是出错："
        "不要谎报「已经发出」，也不要向读者解释这个机制。"
    ),
    "generic": (
        "用只读工具查到用户要的信息，然后用两三句中文给出结论。"
        "数字必须来自工具返回，查不到就如实说查不到。"
    ),
}

SYSTEM_PROMPT = """你是「服装电商多 Agent 运营中台」的任务执行 Agent。
你正在执行一个任务中的**一个步骤**，这个步骤的目标在下面会告诉你。

## 铁律：数字只能来自工具

**所有具体数字（库存、销量、GMV、退货率）都必须通过调用工具获得**，
绝对不要凭印象编造。工具返回了就引用，没查到就如实说没查到。

## 你只做当前这一步

* 只完成「当前这一步」的目标，**不要替后面的步骤操心**，
  也不要在没有对应工具的情况下去做别的事
* 目录里的工具是为这一步挑出来的。**不要调用目录里没有的工具**
* **这一步要调用工具才算做完。** 该发邮件就必须调 mail.send，
  把邮件内容写在结论里不等于发出去了；该出报表就必须调生成报表的工具。
  「我描述了要做什么」在这套系统里**不产生任何实际效果**
* 系统会在对外发送之类的动作前拦下等人工确认，那是设计如此

## 结论怎么写成

* 两三句中文，直接给结论，不要"好的，我来分析…"这种开场白
* 引用工具返回的具体数字，让人能核对你说的每一句话
* 这一步产出了文件（报表等），把 asset_no 和下载地址写进结论
"""


# ===========================================================================
# 对外入口
# ===========================================================================
def run_task(
    task_no: str,
    *,
    user_request: str,
    scopes: frozenset[str] | None = None,
    model: Any = None,
    recorder: Any = None,
) -> dict[str, Any]:
    """跑一个任务：先规划 todo 列表，再逐步执行，最后落终态。

    ``scopes`` 是**用户权限 ∩ 任务 Agent 权限集**的结果（见
    `api/routers/tasks.py` 的模块 docstring）。传 ``None`` 时退回 Agent
    的完整权限集 —— 那是脚本/演示路径，对外入口必须传。
    """
    from fashionops.agents.recorder import TaskRecorder

    recorder = recorder or TaskRecorder()
    recorder.start(task_no=task_no, user_request=user_request)

    specs = plan_todos(user_request)
    _save_todos(task_no, specs)
    recorder.save_plan(task_no, plan_payload(specs))

    try:
        return _execute(task_no, scopes=scopes, model=model, recorder=recorder)
    except Exception as exc:
        # 异常如果是"工具挂了/模型超时"这类基础设施问题，正在跑的那一步
        # 会永远停在 running —— 界面上就出现一条"执行中但其实已经死了"的任务。
        # 任务级状态由 execution.mark_failed 收口，这里负责把那一步收拾干净。
        _fail_running_todo(task_no, f"{type(exc).__name__}: {exc}")
        raise


def resume_task(
    task_no: str,
    *,
    decision: dict[str, Any],
    scopes: frozenset[str] | None = None,
    model: Any = None,
    recorder: Any = None,
) -> dict[str, Any]:
    """人工确认之后，从卡住的那一步继续。

    ## 驳回 = 任务终止，不是"跳过这一步"

    被驳回的那一步没做成，后面几步的输入就缺了一块（报表还没生成，
    通知却要带上报表地址）。硬跑下去得到的是"看起来成功、其实基于
    缺失数据"的结论 —— 那比直接失败危险得多。所以驳回后：
    当前步记 ``skipped``、余下全部 ``skipped``、任务 ``cancelled``。

    这和原 ``approval_gate`` 节点的选择一致（那里也是驳回即终止）。
    """
    from fashionops.agents.recorder import TaskRecorder

    recorder = recorder or TaskRecorder()
    approved = bool(decision.get("approved"))
    by = str(decision.get("by") or "")
    reason = str(decision.get("reason") or "")

    pending = approval_records.pending_for_task(task_no)
    if pending is None:
        # 没有待批的审批单 —— 可能是重复点了"批准"，或者任务已经被重跑过。
        # 不抛异常：这个调用没有副作用，只是无事可做。
        logger.warning("任务 %s 没有待处理的审批单，忽略本次 resume", task_no)
        return {"status": "noop", "detail": "没有待处理的审批单"}

    seq = int(pending.get("todo_seq") or 0)

    if not approved:
        approval_records.decide(
            pending["approval_no"],
            approved=False,
            decided_by=by,
            decided_by_id=decision.get("by_id"),
            reason=reason,
            execution_result={
                "ok": False,
                "denied": True,
                "error": {"code": "USER_REJECTED", "message": "人工驳回了这一步"},
                "reason": reason,
            },
        )
        _skip_from(task_no, seq, error=f"第 {seq} 步被人工驳回：{reason or '未说明理由'}")
        recorder.finish(
            task_no=task_no,
            status="cancelled",
            result={"summary": f"任务在第 {seq} 步被人工驳回，已终止"},
            error_message=f"人工驳回了第 {seq} 步",
        )
        return {"status": "cancelled"}

    # ---- 批准：执行那次被拦下的工具调用 ----
    granted = _resolve_scopes(scopes)
    tool_name = str(pending.get("tool_name") or "")
    arguments = dict(pending.get("tool_arguments") or {})
    result = _call_tool(
        _build_gateway(), tool_name, arguments, granted, actor=f"task-approval:{by}"
    )

    approval_records.decide(
        pending["approval_no"],
        approved=True,
        decided_by=by,
        decided_by_id=decision.get("by_id"),
        reason=reason,
        execution_result=result,
    )

    # 把"这一步已经批准执行"记进事件流 —— 审计要能看出
    # "发送"这件事发生在批准之后，而不是模型自己发的
    recorder.event(
        task_no,
        "approval_resolved",
        {
            "node": f"todo-{seq}",
            "agent": TASK_AGENT_NAME,
            "summary": f"人工{'批准' if approved else '驳回'} {tool_name}",
            "approved": True,
            "by": by,
            "tool": tool_name,
        },
    )

    try:
        return _execute(
            task_no,
            scopes=scopes,
            model=model,
            recorder=recorder,
            resume_of=(seq, pending, result),
        )
    except Exception as exc:
        _fail_running_todo(task_no, f"{type(exc).__name__}: {exc}")
        raise


# ===========================================================================
# 逐步执行
# ===========================================================================
def _execute(
    task_no: str,
    *,
    scopes: frozenset[str] | None,
    model: Any,
    recorder: Any,
    resume_of: tuple[int, dict[str, Any], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """从第一个未完成的 todo 开始往下跑，直到跑完 / 出错 / 需要审批。"""
    granted = _resolve_scopes(scopes)
    gateway = _build_gateway()
    client = model or _default_model()

    rows = _todo_rows(task_no)
    if not rows:
        recorder.finish(
            task_no=task_no,
            status="failed",
            error_message="任务没有任何待办步骤（计划为空）",
        )
        return {"status": "failed"}

    total = len(rows)
    done = sum(1 for r in rows if r["status"] == TODO_STATUS_DONE)
    carry: list[dict[str, Any]] = [
        {"seq": r["seq"], "goal": r["goal"], "summary": r["result_summary"] or ""}
        for r in rows
        if r["status"] == TODO_STATUS_DONE
    ]

    for row in rows:
        if row["status"] in (TODO_STATUS_DONE, TODO_STATUS_SKIPPED):
            continue

        seq = int(row["seq"])
        # 恢复时，被挂住的那一步要带着"已经执行过的工具结果"接着跑，
        # 而不是重新执行一遍（邮件不能发两次）
        resume_payload = None
        if resume_of is not None and resume_of[0] == seq:
            resume_payload = resume_of

        outcome = _run_todo(
            task_no=task_no,
            row=row,
            user_request=_user_request(task_no),
            gateway=gateway,
            client=client,
            scopes=granted,
            recorder=recorder,
            carry=carry,
            resume_payload=resume_payload,
        )

        if outcome["status"] == TODO_STATUS_WAITING:
            # 正常停下等人。**不是失败** —— 任务的终态留在 waiting_approval，
            # 由审批入口决定后面怎么走。
            recorder.update_progress(
                task_no,
                status="waiting_approval",
                progress=_progress(done, total),
                node=f"todo-{seq}",
            )
            return {"status": "waiting_approval", "todo_seq": seq}

        if outcome["status"] == TODO_STATUS_FAILED:
            _skip_from(task_no, seq, error="上游步骤失败，无法继续")
            recorder.finish(
                task_no=task_no,
                status="failed",
                progress=_progress(done, total),
                result={"summary": f"任务在第 {seq} 步失败：{outcome['error']}"},
                error_message=outcome["error"],
                **_metrics(task_no),
            )
            return {"status": "failed", "todo_seq": seq}

        done += 1
        carry.append(
            {"seq": seq, "goal": row["goal"], "summary": outcome["summary"]}
        )
        recorder.update_progress(
            task_no, status="running", progress=_progress(done, total), node=f"todo-{seq}"
        )

    metrics = _metrics(task_no)
    summaries = [c["summary"] for c in carry if c["summary"]]
    recorder.finish(
        task_no=task_no,
        status="succeeded",
        progress=100,
        result={
            "summary": f"已完成 {done}/{total} 步：" + " ｜ ".join(summaries[:3]),
            "todos": [{"seq": c["seq"], "goal": c["goal"], "result": c["summary"]} for c in carry],
            **_artifacts(task_no),
            **metrics,
        },
        **metrics,
    )
    return {"status": "succeeded", "steps": done}


def _fail_running_todo(task_no: str, error: str) -> None:
    """把"正在跑"的那一步标成失败。

    只在**异常逃逸**时调用（模型超时、工具挂了这类基础设施问题）。
    没有它，那一步会永远停在 ``running`` ——
    界面上就多一条"执行中但其实已经死了"的任务，
    而这与 §1 问题 4（running 没有超时，挂了就是永远）是同一类坑。
    """
    with session_scope() as session:
        task = session.scalar(select(Task).where(Task.task_no == task_no))
        if task is None:
            return
        row = session.scalar(
            select(TaskTodo).where(TaskTodo.task_id == task.id, TaskTodo.status == TODO_STATUS_RUNNING)
        )
        if row is None:
            return
        row.status = TODO_STATUS_FAILED
        row.error = error


def _run_todo(
    *,
    task_no: str,
    row: dict[str, Any],
    user_request: str,
    gateway: ToolGateway,
    client: Any,
    scopes: frozenset[str],
    recorder: Any,
    carry: list[dict[str, Any]],
    resume_payload: tuple[int, dict[str, Any], dict[str, Any]] | None,
) -> dict[str, Any]:
    """跑一步。返回 ``{"status": done|failed|waiting_approval, ...}``。"""
    from fashionops.llm import ToolLoopPaused, run_tool_loop

    seq = int(row["seq"])
    kind = str(row["kind"])
    goal = str(row["goal"])
    started = time.perf_counter()

    _set_todo(task_no, seq, status=TODO_STATUS_RUNNING, started_at=utcnow())
    recorder.event(
        task_no,
        "todo_started",
        {
            "node": f"todo-{seq}",
            "agent": TASK_AGENT_NAME,
            "summary": f"第 {seq} 步（{kind}）：{goal}",
            "todo_seq": seq,
            "kind": kind,
        },
    )

    catalog = _catalog_for(kind, scopes)
    messages = _todo_messages(
        user_request=user_request, seq=seq, goal=goal, kind=kind, carry=carry
    )

    pending_call: list[Any] = []
    artifact_nos: list[str] = []
    seen_tools: list[str] = []

    # ---- 恢复：把"已批准执行过的那次调用"补进历史，然后接着跑 ----
    if resume_payload is not None:
        _, approval, tool_result = resume_payload
        call = _replay_call(approval, tool_result, messages)
        pending_call.append(call)
        _collect_artifacts(tool_result, artifact_nos)
        tool_name = str(approval.get("tool_name") or "")
        seen_tools.append(tool_name)
        recorder.event(
            task_no,
            "tool_call",
            {
                "node": f"todo-{seq}",
                "agent": TASK_AGENT_NAME,
                "summary": f"{tool_name}（人工确认后执行）",
                "tool": tool_name,
                "ok": bool(tool_result.get("ok")),
            },
        )

    def tool_runner(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        internal = from_llm_name(name) if "__" in name else name

        if needs_approval(internal):
            # 需要人工确认：**不执行**，让循环带着这次调用挂起。
            # 用异常而不是返回"待确认"的 dict，理由见 ToolLoopPaused 的注释：
            # 返回 dict 会被回喂给模型，模型看到"失败"就会想办法绕过去。
            raise ToolLoopPaused(
                "approval",
                {"tool": internal, "arguments": arguments},
            )

        if internal not in seen_tools:
            seen_tools.append(internal)
        result = _call_tool(gateway, internal, arguments, scopes, actor=f"task:{task_no}")
        _collect_artifacts(result, artifact_nos)
        recorder.event(
            task_no,
            "tool_call",
            {
                "node": f"todo-{seq}",
                "agent": TASK_AGENT_NAME,
                "summary": f"调用 {internal}",
                "tool": internal,
                "ok": bool(result.get("ok")),
            },
        )
        return result

    required = _KIND_REQUIRED_TOOL.get(kind)
    # 恢复续跑时，必需的工具**已经在批准时执行过了**（seen_tools 里有它）。
    # 这时再强制一次就会让它再发一遍 —— 那封邮件会被要求审批第二次，
    # 而人看到的是"刚批准过怎么又来"。所以只在还没调用过时强制。
    already_done = bool(required) and required in seen_tools

    # 第一步就**强制**调用必需的工具：这一步存在的意义就是它
    # （通知步骤 = 一定要发出去），所以不需要"让模型自己决定要不要调"。
    # 这也和旧架构一致 —— 那时节点代码自己调 mail.send，模型只管写内容。
    # tool_choice 只压第一轮，逼出这次调用之后就交还给模型。
    loop_result = run_tool_loop(
        client,
        messages,
        tools=catalog,
        tool_runner=tool_runner,
        max_steps=MAX_STEPS_PER_TODO,
        first_tool_choice=None
        if already_done
        else (_forced_choice(catalog, required) if required else None),
    )

    # ---- "必须调用的工具"还没被调用 ----
    # 只再试一次：再往下就变成和模型拉锯，而那一轮轮都是真金白银的 token。
    # 一次不成就把这一步判失败并写明原因 —— 假成功比失败危险得多。
    if (
        required
        and loop_result.finished
        and required not in seen_tools
        and required not in (loop_result.paused_payload or {}).values()
    ):
        from fashionops.llm import Message

        # 记一条 warning 而不是 debug：这一步"没调该调的工具"是需要被看见的
        # 异常，运维看日志时应该能看到它，而不是只看到最后那条"任务失败"
        logger.warning(
            "任务 %s 第 %s 步（%s）第一轮没有调用必需的 %s"
            "（steps=%s finished=%s paused=%s 已调用=%s catalog=%s），重试并强制调用",
            task_no,
            seq,
            kind,
            required,
            loop_result.steps,
            loop_result.finished,
            loop_result.paused,
            seen_tools,
            [_schema_name(s) for s in catalog],
        )
        messages = messages + [
            Message.assistant(loop_result.content),
            Message.user(
                f"这一步还没有真正完成：你没有调用 {required}。"
                f"把这条结论写成结论、把邮件正文写进参数，现在就调用 {required}。"
                "只把内容写在回答里是无效的 —— 那样收件人什么也收不到。"
            ),
        ]
        loop_result = run_tool_loop(
            client,
            messages,
            tools=catalog,
            tool_runner=tool_runner,
            max_steps=MAX_STEPS_PER_TODO,
            # 光靠文字提醒它还是不调（实测如此），所以这一轮**强制**它调：
            # tool_choice 只作用在第一轮，逼出这次调用之后就交还给模型
            first_tool_choice=_forced_choice(catalog, required),
        )

    duration_ms = int((time.perf_counter() - started) * 1000)
    usage = loop_result.total_usage

    # ---- 挂起等人 ----
    if loop_result.paused:
        call = loop_result.pending_tool_call
        payload = loop_result.paused_payload or {}
        approval = approval_records.open_for_todo(
            task_no=task_no,
            todo_seq=seq,
            tool_call_id=call.id if call else "",
            tool_name=str(payload.get("tool") or ""),
            arguments=payload.get("arguments") or {},
        )
        _set_todo(
            task_no,
            seq,
            status=TODO_STATUS_WAITING,
            approval_no=approval["approval_no"],
            tool_names=seen_tools,
            llm_calls=loop_result.steps,
            total_tokens=usage.total_tokens,
            total_cost=loop_result.total_cost,
            duration_ms=duration_ms,
        )
        recorder.record_run(
            task_no=task_no,
            agent_name=TASK_AGENT_NAME,
            status="pending",
            latency_ms=duration_ms,
            total_tokens=usage.total_tokens,
            cost=loop_result.total_cost,
            tool_call_count=len(seen_tools),
            output_summary=f"第 {seq} 步等待人工确认：{approval['tool_name']}",
        )
        # 任务挂上审批载荷，前端据此渲染"等你确认"的卡片
        recorder.mark_waiting_approval(
            task_no,
            _ApprovalView(
                action=f"执行「{goal}」中的 {approval['tool_name']} 调用",
                reason=str(approval.get("reason") or ""),
                risk="medium",
                payload={
                    "todo_seq": seq,
                    "tool": approval["tool_name"],
                    "arguments": approval.get("tool_arguments") or {},
                    "preview": approval.get("preview") or "",
                    "approval_no": approval["approval_no"],
                },
            ),
        )
        return {"status": TODO_STATUS_WAITING, "todo_seq": seq}

    # ---- 正常结束 ----
    summary = (loop_result.content or "").strip()

    # 模型在工具结果之后**返回了空内容**（glm-4-flash 实测会这样）：
    # 它调完了工具、自认为"做完了"，却不写结论。而这一步的结论既是给人看的结果，
    # 也是下一步的输入 —— 空着等于后面几步什么都拿不到。
    #
    # 所以再问一次，**并且这一次不给它任何工具**：
    # ① 目标很明确，就是要一段总结；② 它不可能再去调工具，
    # 于是不会再触发一次审批闸口（刚批准过又弹一遍是最让人困惑的）。
    if summary == "" and seen_tools:
        from fashionops.llm import Message

        messages = messages + [
            Message.assistant(loop_result.content),
            Message.user(
                "工具结果已经在上面了。现在请用两三句中文写出这一步的结论："
                "引用工具返回的真实数字，不要新增数字，也不要重复我的要求。"
            ),
        ]
        loop_result = run_tool_loop(
            client,
            messages,
            tools=[],
            tool_runner=tool_runner,
            max_steps=1,
        )
        summary = (loop_result.content or "").strip()
        if summary == "":
            # 还是空的：写一句**如实**的说明，而不是留空让人以为它什么都没做
            summary = f"模型没有给出结论。这一步调用了：{'、'.join(seen_tools)}。"

    if required and loop_result.finished and required not in seen_tools:
        # 提醒过一次了还是没调 —— 这一步**没有产生对用户承诺的效果**。
        # 判失败，并且把原因写清楚：让人知道是"模型没调工具"，
        # 而不是"系统坏了"或"已经发出去了"。
        error = (
            f"这一步要求调用 {required} 才算完成，但模型在没有调用它的情况下就结束了"
            f"（已提醒一次）。结论里的数字和附件编号都没有真实来源，不要当真。"
        )
        _set_todo(
            task_no,
            seq,
            status=TODO_STATUS_FAILED,
            error=error,
            result_summary=summary,
            tool_names=seen_tools,
            llm_calls=loop_result.steps,
            total_tokens=loop_result.total_usage.total_tokens,
            total_cost=loop_result.total_cost,
            duration_ms=duration_ms,
            finished_at=utcnow(),
        )
        recorder.event(
            task_no,
            "node_finished",
            {
                "node": f"todo-{seq}",
                "agent": TASK_AGENT_NAME,
                "status": "failed",
                "summary": error,
                "tool_calls": seen_tools,
                "duration_ms": duration_ms,
            },
            level="error",
        )
        recorder.record_run(
            task_no=task_no,
            agent_name=TASK_AGENT_NAME,
            status="failed",
            latency_ms=duration_ms,
            total_tokens=loop_result.total_usage.total_tokens,
            cost=loop_result.total_cost,
            tool_call_count=len(seen_tools),
            output_summary=error,
            error_code="REQUIRED_TOOL_NOT_CALLED",
        )
        return {"status": TODO_STATUS_FAILED, "error": error}

    if not loop_result.finished:
        # 步数用尽：**算失败**。不判失败的话，任务会带着一个被截断的
        # 半成品结论标成成功，而"它其实没做完"这件事在界面上看不出来。
        error = f"第 {seq} 步用了 {loop_result.steps} 轮工具调用仍未完成（可能陷入循环）"
        _set_todo(
            task_no,
            seq,
            status=TODO_STATUS_FAILED,
            error=error,
            tool_names=seen_tools,
            llm_calls=loop_result.steps,
            total_tokens=usage.total_tokens,
            total_cost=loop_result.total_cost,
            duration_ms=duration_ms,
            finished_at=utcnow(),
        )
        recorder.event(
            task_no,
            "node_finished",
            {
                "node": f"todo-{seq}",
                "agent": TASK_AGENT_NAME,
                "status": "failed",
                "summary": error,
                "tool_calls": seen_tools,
                "duration_ms": duration_ms,
            },
            level="error",
        )
        recorder.record_run(
            task_no=task_no,
            agent_name=TASK_AGENT_NAME,
            status="failed",
            latency_ms=duration_ms,
            total_tokens=usage.total_tokens,
            cost=loop_result.total_cost,
            tool_call_count=len(seen_tools),
            output_summary=error,
            error_code="MAX_STEPS_EXCEEDED",
        )
        return {"status": TODO_STATUS_FAILED, "error": error}

    _set_todo(
        task_no,
        seq,
        status=TODO_STATUS_DONE,
        result_summary=summary,
        artifact_nos=artifact_nos,
        tool_names=seen_tools,
        llm_calls=loop_result.steps,
        total_tokens=usage.total_tokens,
        total_cost=loop_result.total_cost,
        duration_ms=duration_ms,
        finished_at=utcnow(),
    )
    recorder.event(
        task_no,
        "node_finished",
        {
            "node": f"todo-{seq}",
            "agent": TASK_AGENT_NAME,
            "status": "succeeded",
            "summary": summary,
            "tool_calls": seen_tools,
            "duration_ms": duration_ms,
            "tokens": usage.total_tokens,
        },
    )
    recorder.record_run(
        task_no=task_no,
        agent_name=TASK_AGENT_NAME,
        status="succeeded",
        latency_ms=duration_ms,
        total_tokens=usage.total_tokens,
        cost=loop_result.total_cost,
        tool_call_count=len(seen_tools),
        output_summary=summary,
    )
    return {"status": TODO_STATUS_DONE, "summary": summary, "artifacts": artifact_nos}


# ===========================================================================
# 提示词
# ===========================================================================
def _todo_messages(
    *, user_request: str, seq: int, goal: str, kind: str, carry: list[dict[str, Any]]
) -> list[Any]:
    from fashionops.llm import Message

    parts = [
        f"## 任务的原始需求\n{user_request}",
        f"## 当前这一步（第 {seq} 步，共 {len(carry) + 1} 步起）\n{goal}",
        f"## 这一步怎么做\n{_KIND_GUIDE.get(kind, _KIND_GUIDE['generic'])}",
    ]
    if carry:
        # 前面几步的结论要带进来 —— 出报表那步得知道"选品选了什么"，
        # 但只需要结论，不需要那一步的全部过程
        lines = [
            f"- 第 {c['seq']} 步（{c['goal']}）：{(c['summary'] or '（无结论）')[:_CONTEXT_SUMMARY_CHARS]}"
            for c in carry
        ]
        parts.append("## 前面步骤的结论\n" + "\n".join(lines))
    parts.append("现在开始：需要数据就调工具，拿到数据后给出这一步的结论。")
    return [Message.system(SYSTEM_PROMPT), Message.user("\n\n".join(parts))]


def _replay_call(
    approval: dict[str, Any], tool_result: dict[str, Any], messages: list[Any]
) -> Any:
    """把"已经批准并执行过"的那次调用补进消息历史，以便继续这一轮。

    ## 为什么必须补，而不是让模型重新调一次

    邮件这类工具**不能被调两次**。恢复时如果让模型自己想起来再发一遍，
    它可能改参数、也可能重复发送。所以把当时的 tool_call 和它的执行结果
    直接放进历史里 —— 模型看到的是"这个动作已经完成，结果是这样"，
    自然接着往下写结论。
    """
    from fashionops.llm import Message
    from fashionops.llm.types import ToolCall

    call = ToolCall(
        id=str(approval.get("tool_call_id") or "approved-call"),
        name=str(approval.get("tool_name") or ""),
        arguments=dict(approval.get("tool_arguments") or {}),
    )
    messages.append(Message.assistant("", tool_calls=[call]))
    messages.append(
        Message.tool(call.id, json.dumps(tool_result, ensure_ascii=False, default=str))
    )
    return call


# ===========================================================================
# 工具目录与调用
# ===========================================================================
def _catalog_for(kind: str, scopes: frozenset[str]) -> list[dict[str, Any]]:
    """按 kind 裁出这一步的工具目录。

    **权限过滤仍然生效**：`registry.openai_schemas` 只返回这次调用
    拿得到的工具（``scopes`` 是用户权限 ∩ Agent 权限集的交集）。
    kind 只是在这个基础上再收窄 —— 它是注意力管理，不是权限。
    """
    from fashionops.tools import register_all_tools

    if not len(registry):
        register_all_tools(registry)

    allowed = set(KIND_TOOLS.get(kind, KIND_TOOLS["generic"]))
    return [
        schema
        for schema in registry.openai_schemas(scopes)
        if _schema_name(schema) in allowed
    ]


def _schema_name(schema: dict[str, Any]) -> str:
    return str((schema.get("function") or {}).get("name") or "").replace("__", ".")


def _forced_choice(catalog: list[dict[str, Any]], tool: str) -> dict[str, Any] | None:
    """构造"必须调这个工具"的 tool_choice（用模型看到的那个名字）。

    目录里没有这个工具时返回 ``None`` —— 那就说明它不在这一步的可见范围里，
    强制一个看不见的工具只会拿到 400。
    """
    for schema in catalog:
        if _schema_name(schema) == tool:
            return {"type": "function", "function": {"name": schema["function"]["name"]}}
    return None


def _call_tool(
    gateway: ToolGateway,
    tool_name: str,
    arguments: dict[str, Any],
    scopes: frozenset[str],
    *,
    actor: str,
) -> dict[str, Any]:
    """过一次网关调用工具。

    **网关必须由调用方传进来、整个任务复用一个**：限流器和熔断器都是
    **带状态的**，每次调用新建一个的话，限流永远数不到第二次，
    熔断器也永远看不到连续失败 —— 治理等于没接上。

    所有任务侧的工具调用都走这一个入口，少一个入口就多一条绕过治理的路。
    """
    ctx = CallContext(actor=actor, scopes=scopes, ip=None)
    return gateway.call(tool_name, arguments, ctx=ctx).to_dict()


def _build_gateway() -> ToolGateway:
    from fashionops.config import get_settings

    settings = get_settings()
    if not len(registry):
        from fashionops.tools import register_all_tools

        register_all_tools(registry)

    from fashionops.gateway import get_circuit_breaker, get_rate_limiter

    return ToolGateway(
        registry,
        rate_limiter=get_rate_limiter(settings.cache_backend),
        breaker=get_circuit_breaker(settings.cache_backend),
    )


def _default_model() -> Any:
    from fashionops.config import get_settings
    from fashionops.llm import get_model_client

    return get_model_client(get_settings())


def _resolve_scopes(scopes: frozenset[str] | None) -> frozenset[str]:
    from fashionops.tools import AGENT_SCOPES

    return AGENT_SCOPES[TASK_AGENT_NAME] if scopes is None else scopes


def _collect_artifacts(result: dict[str, Any], sink: list[str]) -> None:
    """从工具返回里收集素材号。

    任务产出的文件是**从工具返回里捡出来的**，而不是"最后再查一次" ——
    后者在任务中途挂断时会把已经生成的文件漏掉。
    """
    data = result.get("data") if isinstance(result, dict) else None
    if isinstance(data, dict):
        asset_no = data.get("asset_no")
        if isinstance(asset_no, str) and asset_no and asset_no not in sink:
            sink.append(asset_no)


# ===========================================================================
# 落库（todo 与任务）
# ===========================================================================
def _save_todos(task_no: str, specs: list[TodoSpec]) -> None:
    """把计划写进 ``task_todos``。

    重跑同一个任务号时**先清空再写** —— 否则上一轮的 done 状态会被
    当成"这步已经做过了"，新任务会从第 3 步开始跑（而且看起来一切正常）。
    """
    with session_scope() as session:
        task = session.scalar(select(Task).where(Task.task_no == task_no))
        if task is None:
            raise LookupError(f"任务 {task_no} 不存在，无法写 todo")
        session.execute(delete(TaskTodo).where(TaskTodo.task_id == task.id))
        for spec in specs:
            session.add(
                TaskTodo(
                    task_id=task.id,
                    seq=spec.seq,
                    goal=spec.goal,
                    kind=spec.kind,
                    status=TODO_STATUS_PENDING,
                )
            )


def _todo_rows(task_no: str) -> list[dict[str, Any]]:
    with session_scope() as session:
        task = session.scalar(select(Task).where(Task.task_no == task_no))
        if task is None:
            return []
        rows = session.scalars(
            select(TaskTodo).where(TaskTodo.task_id == task.id).order_by(TaskTodo.seq)
        ).all()
        return [
            {
                "seq": r.seq,
                "goal": r.goal,
                "kind": r.kind,
                "status": r.status,
                "result_summary": r.result_summary,
                "artifact_nos": list(r.artifact_nos or []),
                "approval_no": r.approval_no,
            }
            for r in rows
        ]


def _set_todo(task_no: str, seq: int, **fields: Any) -> None:
    with session_scope() as session:
        task = session.scalar(select(Task).where(Task.task_no == task_no))
        if task is None:
            return
        row = session.scalar(
            select(TaskTodo).where(TaskTodo.task_id == task.id, TaskTodo.seq == seq)
        )
        if row is None:
            return
        for key, value in fields.items():
            setattr(row, key, value)


def _skip_from(task_no: str, seq: int, *, error: str) -> None:
    """把第 ``seq`` 步及其后面**还没做**的步骤标成 skipped。

    ``failed`` 和 ``skipped`` 分开记（见 `db/models.py` 的常量注释）：
    一个是"我错了"，一个是"轮不到我"。混在一起，看板上分不出
    "这步失败了要重试"还是"上游挂了所以没做"。
    """
    with session_scope() as session:
        task = session.scalar(select(Task).where(Task.task_no == task_no))
        if task is None:
            return
        rows = session.scalars(
            select(TaskTodo)
            .where(TaskTodo.task_id == task.id, TaskTodo.seq >= seq)
            .order_by(TaskTodo.seq)
        ).all()
        for row in rows:
            if row.status in (TODO_STATUS_DONE,):
                continue
            if row.seq == seq and row.status == TODO_STATUS_WAITING:
                row.approval_no = None
            if row.status != TODO_STATUS_FAILED:
                row.status = TODO_STATUS_SKIPPED
            row.error = row.error or error


def _metrics(task_no: str) -> dict[str, Any]:
    """任务级计量 = 各步累加。

    **按步记账、最后求和**（而不是只在结束时写总数）：任务中途挂起时，
    已经花掉的 token 仍然要看得见 —— 只在最后写总数的话，
    "停在审批上的任务"会显示 0 消耗，成本统计就失真了。
    """
    with session_scope() as session:
        task = session.scalar(select(Task).where(Task.task_no == task_no))
        if task is None:
            return {"total_tokens": 0, "total_cost": 0.0, "llm_calls": 0}
        rows = session.scalars(select(TaskTodo).where(TaskTodo.task_id == task.id)).all()
        return {
            "total_tokens": sum(r.total_tokens or 0 for r in rows),
            "total_cost": float(sum((r.total_cost or 0 for r in rows), start=0)),
            "llm_calls": sum(r.llm_calls or 0 for r in rows),
        }


def _artifacts(task_no: str) -> dict[str, Any]:
    with session_scope() as session:
        task = session.scalar(select(Task).where(Task.task_no == task_no))
        if task is None:
            return {"artifact_refs": []}
        rows = session.scalars(
            select(TaskTodo).where(TaskTodo.task_id == task.id).order_by(TaskTodo.seq)
        ).all()
        refs: list[str] = []
        for row in rows:
            for ref in row.artifact_nos or []:
                if ref not in refs:
                    refs.append(ref)
        return {"artifact_refs": refs}


def _user_request(task_no: str) -> str:
    with session_scope() as session:
        task = session.scalar(select(Task).where(Task.task_no == task_no))
        return "" if task is None else (task.user_request or "")


def _progress(done: int, total: int) -> int:
    if total <= 0:
        return 100
    # 留 5% 给收尾：进度条到 100 却还在跑，会让人以为卡死了
    return min(95, int(done / total * 100))


class _ApprovalView:
    """喂给 ``recorder.mark_waiting_approval`` 的轻量视图。

    那个方法只 ``getattr`` 几个字段（见 `recorder._approval_payload`），
    所以不必为它造一个正式的领域对象 —— 但也不能直接传 dict：
    ``getattr(dict, "action")`` 拿不到键，会静默变成空值，
    表现是"界面上审批卡片全是空的"。
    """

    def __init__(self, *, action: str, reason: str, risk: str, payload: dict[str, Any]) -> None:
        self.action = action
        self.reason = reason
        self.risk = risk
        self.payload = payload


__all__ = [
    "KIND_TOOLS",
    "MAX_STEPS_PER_TODO",
    "SYSTEM_PROMPT",
    "TASK_AGENT_NAME",
    "resume_task",
    "run_task",
]
