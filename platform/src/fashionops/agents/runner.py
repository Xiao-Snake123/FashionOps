"""任务运行器 —— 编排层的对外入口。

## 为什么要有这一层

``build_orchestrator`` 只是"造一台机器"，谁按启动键、跑的时候记不记档、
停下来之后状态存在哪，都不是图本身该管的事。

这一层负责三件事：

1. **跑图**：用 ``graph.stream(stream_mode="updates")`` 而不是 ``invoke`` ——
   流式才能**逐节点**拿到更新，从而做到"每跑完一个节点就落一次档"。
   用 ``invoke`` 的话要等整个任务跑完才拿到结果，中途中断的任务在库里
   什么痕迹都没有。
2. **记账**：每个节点的更新交给 ``TaskRecorder`` 落库（事件流 + agent_runs）
3. **判停**：区分"跑完了"和"停在人工闸口上" —— 这两种情况对调用方
   是完全不同的语义，不能都返回一个 dict 让人自己去猜

## 为什么"停在闸口"不是失败

``graph.stream`` 遇到 ``interrupt()`` 会正常结束迭代并给出中断载荷，
**不抛异常**。所以这里不需要 try/except 去捕获中断 —— 那是个常见误解，
写成捕获异常反而会把真正的错误也当成中断吞掉。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from langgraph.types import Command

from fashionops.agents.checkpoint import (
    get_checkpointer,
    latest_state,
    pending_nodes,
    thread_config,
)
from fashionops.agents.orchestrator import build_orchestrator
from fashionops.agents.recorder import (
    EVENT_RESUMED,
    TaskRecorder,
    task_snapshot,
)
from fashionops.agents.state import RunContext, new_state

logger = logging.getLogger("fashionops.runner")

#: 表示"脚本暂停在人工闸口"的节点名
APPROVAL_NODE = "approval_gate"

#: 中断载荷在 stream 输出里的特殊键
_INTERRUPT_KEY = "__interrupt__"


@dataclass
class RunOutcome:
    """一次 submit / resume 的结果。

    刻意把 ``interrupted`` 和 ``status`` 分开：
    状态是"任务本身走到哪了"，``interrupted`` 是"这次调用是不是停下来了"。
    混成一个字段的话，调用方没法区分"任务失败"和"任务在等人确认"。
    """

    task_no: str
    status: str
    interrupted: bool = False
    pending: list[str] = field(default_factory=list)
    approval: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    #: 本次调用实际跑过的节点，按顺序
    executed_nodes: list[str] = field(default_factory=list)

    @property
    def waiting_approval(self) -> bool:
        return self.interrupted and APPROVAL_NODE in self.pending

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_no": self.task_no,
            "status": self.status,
            "interrupted": self.interrupted,
            "waiting_approval": self.waiting_approval,
            "pending_nodes": self.pending,
            "approval": self.approval,
            "result": self.result,
            "error": self.error,
            "executed_nodes": self.executed_nodes,
        }


class TaskRunner:
    """把"提交一个任务"这件事封装成一个可调用的对象。"""

    def __init__(
        self,
        ctx: RunContext,
        *,
        recorder: TaskRecorder | None = None,
        checkpointer: Any = None,
    ) -> None:
        self.ctx = ctx
        self.recorder = recorder or TaskRecorder()
        self.checkpointer = checkpointer or get_checkpointer()
        self.graph = build_orchestrator(ctx, checkpointer=self.checkpointer)

    # ------------------------------------------------------------------
    def submit(
        self,
        *,
        task_no: str,
        user_request: str,
        task_type: str = "generic",
        created_by: str = "user",
    ) -> RunOutcome:
        """提交一个新任务并跑到"完成或停在闸口"。"""
        self.recorder.start(
            task_no=task_no,
            user_request=user_request,
            task_type=task_type,
            created_by=created_by,
        )
        state = new_state(task_no=task_no, user_request=user_request, task_type=task_type)
        return self._drive(task_no, self.graph.stream(state, self._config(task_no), stream_mode="updates"))

    def resume(self, *, task_no: str, decision: Any) -> RunOutcome:
        """人工确认后从断点继续。

        ``decision`` 可以是 ``True`` / ``{"approved": True, "by": "张三"}`` 等，
        解释逻辑在 ``orchestrator._read_approved``。
        """
        self.recorder.event(task_no, EVENT_RESUMED, {"node": APPROVAL_NODE, "decision": _safe_decision(decision)})
        command = Command(resume=decision)
        return self._drive(
            task_no, self.graph.stream(command, self._config(task_no), stream_mode="updates")
        )

    # ------------------------------------------------------------------
    def _config(self, task_no: str) -> dict[str, Any]:
        return thread_config(task_no, self.ctx)

    def _drive(self, task_no: str, stream: Any) -> RunOutcome:
        """消费 stream，逐节点记账，最后判停。"""
        executed: list[str] = []
        interrupt_payload: dict[str, Any] | None = None

        for chunk in stream:
            for node, update in chunk.items():
                if node == _INTERRUPT_KEY:
                    interrupt_payload = _first_interrupt(update)
                    continue
                executed.append(node)
                self.recorder.record_node(task_no, node, update if isinstance(update, dict) else {})
                if node == "plan" and isinstance(update, dict):
                    self.recorder.save_plan(task_no, update.get("plan"))

        return self._settle(task_no, executed, interrupt_payload)

    def _settle(
        self, task_no: str, executed: list[str], interrupt_payload: dict[str, Any] | None
    ) -> RunOutcome:
        """判断这次调用是"跑完了"还是"停在闸口"。"""
        pending = pending_nodes(self.graph, task_no)
        snapshot = task_snapshot(task_no) or {}
        state = latest_state(self.checkpointer, task_no) or {}
        if APPROVAL_NODE in pending:
            approval = state.get("approval")
            payload = interrupt_payload or (
                {
                    "action": getattr(approval, "action", ""),
                    "reason": getattr(approval, "reason", ""),
                    "payload": getattr(approval, "payload", {}),
                }
                if approval
                else None
            )
            self.recorder.mark_waiting_approval(task_no, approval)
            return RunOutcome(
                task_no=task_no,
                status="waiting_approval",
                interrupted=True,
                pending=pending,
                approval=payload,
                result=snapshot.get("result"),
                executed_nodes=executed,
            )

        # **图状态才是权威来源**，不是数据库快照。
        #
        # 踩过的坑：中间节点调 update_progress 时会把 tasks.status 写成
        # "running"，而 finalize 把图状态改成了 "failed"。如果先读快照，
        # 读到的永远是 "running" —— 任务在库里永远不结束。
        status = str(state.get("status") or snapshot.get("status") or "succeeded")
        self.recorder.finish(
            task_no=task_no,
            status=status,
            progress=int(state.get("progress") or 100),
            result=state.get("result") or snapshot.get("result"),
            total_tokens=int(state.get("total_tokens") or 0),
            total_cost=state.get("total_cost") or 0,
            llm_calls=int(state.get("llm_calls") or 0),
            error_message=_first_error(state),
        )
        return RunOutcome(
            task_no=task_no,
            status=status,
            interrupted=False,
            pending=[],
            result=state.get("result") or snapshot.get("result"),
            error=_first_error(state),
            executed_nodes=executed,
        )

    # ------------------------------------------------------------------
    def current(self, task_no: str) -> dict[str, Any]:
        """不跑图，只读当前快照 —— 前端刷新页面时用这个。"""
        snapshot = task_snapshot(task_no) or {}
        snapshot["pending_nodes"] = pending_nodes(self.graph, task_no)
        state = latest_state(self.checkpointer, task_no) or {}
        snapshot["plan"] = state.get("plan")
        snapshot["steps"] = [_step_dict(s) for s in state.get("steps", [])]
        snapshot["findings"] = state.get("findings") or {}
        return snapshot


def _first_interrupt(update: Any) -> dict[str, Any] | None:
    """从 stream 的中断载荷里取第一个。"""
    items = update if isinstance(update, (list, tuple)) else [update]
    for item in items:
        value = getattr(item, "value", None)
        if isinstance(value, dict):
            return value
    return None


def _safe_decision(decision: Any) -> Any:
    """审计里记决策时去掉可能很大的字段。"""
    if isinstance(decision, dict):
        return {k: v for k, v in decision.items() if k in ("approved", "approve", "by", "reason")}
    return decision


def _step_dict(step: Any) -> dict[str, Any]:
    if hasattr(step, "model_dump"):
        return step.model_dump(mode="json")
    return dict(step) if isinstance(step, dict) else {}


def _first_error(state: dict[str, Any]) -> str | None:
    errors = state.get("errors") or []
    return str(errors[-1])[:500] if errors else None


__all__ = [
    "APPROVAL_NODE",
    "RunOutcome",
    "TaskRunner",
]
