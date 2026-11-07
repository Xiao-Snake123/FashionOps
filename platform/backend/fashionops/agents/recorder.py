"""任务落库 —— 把"跑过的任务"真的写进数据库。

## 为什么必须做这件事

`tasks` / `task_events` / `agent_runs` 三张表原来只有 seed 灌的假数据，
跑真实 Agent 时一行都不写。后果是：

* 前端任务列表显示的是**假任务**，和实际跑过的任务毫无关系
* SSE 没东西可推（`task_events` 是它的数据源）
* 出问题要复盘时，查不到"这次任务在哪一步、用了什么参数、花了多少 token"

这是"看起来功能都在、实际是个演示壳"的典型。补上它，
项目才从"能跑的脚本"变成"能用的系统"。

## 为什么单独一层而不写进节点里

节点应该只做**决策**。写库属于"运行记录"，是编排层的职责。
如果让每个节点自己写库：

1. 节点变得不可单测（跑一次要连库）
2. 每加一个 Agent 要重复写一遍落库代码
3. 写库失败会让业务逻辑失败 —— **记录不该有这个权力**

所以这里用**订阅式**：编排层跑图时逐节点收更新，由 recorder 统一落库。
写库失败只记日志、不打断任务。**记录是旁路，不是主路。**
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from fashionops.cache import invalidate_namespace
from fashionops.db import session_scope
from fashionops.db.base import utcnow
from fashionops.db.models import AgentRun, Task, TaskEvent

logger = logging.getLogger("fashionops.recorder")

#: 事件类型。前端 SSE 按这个字段决定怎么渲染。
EVENT_STARTED = "task_started"
EVENT_PLANNED = "task_planned"
EVENT_NODE = "node_finished"
EVENT_APPROVAL = "approval_required"
EVENT_RESUMED = "task_resumed"
EVENT_FINISHED = "task_finished"
EVENT_FAILED = "task_failed"
#: 人工驳回导致的终止。刻意**不复用** ``task_failed``：
#: 在事件流里看到 ``task_failed`` 时读的人会去查错误栈，
#: 而取消根本没有错误 —— 是人做的决定。混淆这两件事会误导排查方向。
EVENT_CANCELLED = "task_cancelled"

#: 编排层自身的节点，不是业务 Agent —— 它们不该进 ``agent_runs``
#: （那张表是"Agent 成功率/延迟/成本"的数据源，混进编排节点会把指标算歪）
_ORCHESTRATION_NODES = frozenset({"plan", "hub", "approval_gate", "finalize"})

#: StepRecord.status → AgentRun.status。
#: 两张表的取值约定不同：AgentRun 用的是 success/error/timeout，
#: 直接把 "succeeded" 写进去会让按 status 过滤的查询永远查不到数据。
_RUN_STATUS = {
    "succeeded": "success",
    "failed": "error",
    "skipped": "success",
    "pending": "success",
    "running": "success",
}


class TaskRecorder:
    """任务运行记录器。

    所有方法都是**尽力而为**：写库失败只告警，不抛异常。理由见模块 docstring。
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self._seq_cache: dict[str, int] = {}
        #: 写入失败次数。存在的意义是让"记录被静默丢弃"变成**可断言**的事实 ——
        #: 之前的事件丢失 bug 只留了一行 warning，测试和监控都看不见。
        self.write_failures = 0

    # ------------------------------------------------------------------
    # 任务主表
    # ------------------------------------------------------------------
    def start(
        self,
        *,
        task_no: str,
        user_request: str,
        task_type: str = "generic",
        created_by: str = "user",
    ) -> None:
        """建任务行。已存在就复用（支持重跑同一个 task_no）。"""

        def _do() -> None:
            with session_scope() as session:
                existing = session.scalar(select(Task).where(Task.task_no == task_no))
                if existing is None:
                    session.add(
                        Task(
                            task_no=task_no,
                            # title 是必填的，没有更好的来源就用需求本身截断
                            title=user_request[:120] or task_no,
                            user_request=user_request,
                            task_type=task_type,
                            status="running",
                            progress=0,
                            thread_id=task_no,
                            created_by=created_by,
                            started_at=utcnow(),
                        )
                    )
                else:
                    existing.status = "running"
                    existing.progress = 0
                    existing.started_at = utcnow()
                    existing.finished_at = None
                    existing.error_message = None

        self._safe(_do)
        self._touch_summary_cache()
        self._seq_cache[task_no] = self._current_seq(task_no)
        self.event(task_no, EVENT_STARTED, {"user_request": user_request, "node": "start"})

    def _touch_summary_cache(self) -> None:
        """任务状态一变，``/api/v1/summary`` 里的 running/failed_tasks 就过期了。

        这里主动让它失效，而不是干等 30 秒 TTL：用户刚点了"批准"，
        回头看到"运行中 0"这种滞后，比多一次 Redis INCR 难受得多。

        缓存被关闭或 Redis 不可用时这是个空操作
        （见 `cache.invalidate_namespace` 的异常处理）。
        """
        invalidate_namespace("summary")

    def finish(
        self,
        *,
        task_no: str,
        status: str,
        progress: int = 100,
        result: dict[str, Any] | None = None,
        total_tokens: int = 0,
        total_cost: float | Decimal = 0,
        llm_calls: int = 0,
        error_message: str | None = None,
    ) -> None:
        def _do() -> None:
            with session_scope() as session:
                task = session.scalar(select(Task).where(Task.task_no == task_no))
                if task is None:
                    return
                task.status = status
                task.progress = progress
                task.result = result
                task.total_tokens = total_tokens
                task.total_cost = Decimal(str(total_cost))
                task.llm_calls = llm_calls
                task.error_message = error_message
                task.finished_at = utcnow()
                # 任务已经结束了，待审批内容要清掉 ——
                # 否则界面上会一直挂着一个"待确认"的红点，而任务早就跑完了。
                # 审批历史不会丢：它在 task_events 的 approval_required /
                # task_resumed 两条事件里。
                task.approval_payload = None

        self._safe(_do)
        self._touch_summary_cache()
        if status == "cancelled":
            event_type, level = EVENT_CANCELLED, "warn"
        elif status == "failed":
            event_type, level = EVENT_FAILED, "error"
        else:
            event_type, level = EVENT_FINISHED, "info"
        self.event(
            task_no,
            event_type,
            {
                "node": "finalize",
                "status": status,
                "total_tokens": total_tokens,
                "total_cost": float(total_cost),
            },
            level=level,
        )

    def mark_waiting_approval(self, task_no: str, approval: Any) -> None:
        """任务停在人工闸口上。"""

        def _do() -> None:
            with session_scope() as session:
                task = session.scalar(select(Task).where(Task.task_no == task_no))
                if task is None:
                    return
                task.status = "waiting_approval"
                task.approval_payload = (
                    {
                        "action": getattr(approval, "action", ""),
                        "reason": getattr(approval, "reason", ""),
                        "risk": getattr(approval, "risk", "medium"),
                        **getattr(approval, "payload", {}),
                    }
                    if approval is not None
                    else None
                )

        self._safe(_do)
        self.event(task_no, EVENT_APPROVAL, self._approval_payload(approval))

    def save_plan(self, task_no: str, plan: dict[str, Any] | None) -> None:
        """把计划同时写进 ``tasks.dag`` 和事件流 —— 前端 DAG 图读这个。"""

        def _do() -> None:
            with session_scope() as session:
                task = session.scalar(select(Task).where(Task.task_no == task_no))
                if task is not None:
                    task.dag = plan

        self._safe(_do)
        self.event(task_no, EVENT_PLANNED, {"node": "plan", "plan": plan or {}})

    def update_progress(self, task_no: str, *, status: str, progress: int, node: str = "") -> None:
        def _do() -> None:
            with session_scope() as session:
                task = session.scalar(select(Task).where(Task.task_no == task_no))
                if task is not None:
                    task.status = status
                    task.progress = progress
                    if node:
                        task.current_node = node

        self._safe(_do)
        self._touch_summary_cache()

    # ------------------------------------------------------------------
    # 事件流（SSE 数据源）
    # ------------------------------------------------------------------
    def event(self, task_no: str, kind: str, payload: dict[str, Any] | None = None, *, level: str = "info") -> None:
        """追加一条事件。

        ``seq`` 以内存计数为准，**但首次用到某个任务时必须从库里取基线**。

        踩过的坑：``build_runner`` 每次都会新建一个 ``TaskRecorder``，
        resume 时拿到的是新实例，``_seq_cache`` 是空的 —— 于是 seq 从 1 重新开始，
        撞上 ``uq_task_event_seq`` 唯一约束，插入失败被 ``_safe`` 吞掉。
        表现是"批准之后的所有事件都不见了"，而且**日志里只有一行 warning**。

        这个 bug 特别值得记：它是"记录失败不影响业务"这个设计**自己掩盖**的 ——
        如果没有那条容错，插入失败会直接报错，一眼就能看到。
        """

        def _do() -> None:
            with session_scope() as session:
                task = session.scalar(select(Task).where(Task.task_no == task_no))
                if task is None:
                    return
                if task_no not in self._seq_cache:
                    self._seq_cache[task_no] = self._max_seq(session, task.id)
                seq = self._seq_cache[task_no] + 1
                body = payload or {}
                session.add(
                    TaskEvent(
                        task_id=task.id,
                        seq=seq,
                        event_type=kind,
                        node_name=str(body.get("node") or "") or None,
                        agent_name=str(body.get("agent") or "") or None,
                        level=level,
                        message=str(body.get("summary") or body.get("status") or "")[:2000] or None,
                        payload=body,
                        duration_ms=int(body.get("duration_ms") or 0) or None,
                    )
                )
                self._seq_cache[task_no] = seq
                if body.get("node"):
                    task.current_node = str(body["node"])

        self._safe(_do)

    # ------------------------------------------------------------------
    # 单次 Agent 执行
    # ------------------------------------------------------------------
    def record_run(
        self,
        *,
        task_no: str,
        agent_name: str,
        status: str,
        latency_ms: int = 0,
        total_tokens: int = 0,
        cost: float | Decimal = 0,
        tool_call_count: int = 0,
        output_summary: str = "",
        error_code: str | None = None,
    ) -> None:
        """写一条 ``agent_runs``。这张表是评测与成本归因的数据源。"""

        def _do() -> None:
            with session_scope() as session:
                task = session.scalar(select(Task).where(Task.task_no == task_no))
                session.add(
                    AgentRun(
                        task_id=task.id if task else None,
                        agent_name=agent_name,
                        node_name=agent_name,
                        total_tokens=total_tokens,
                        cost=Decimal(str(cost)),
                        latency_ms=latency_ms or None,
                        status=_RUN_STATUS.get(status, "success"),
                        tool_call_count=tool_call_count,
                        output_digest=output_summary[:64] if output_summary else None,
                        error_code=error_code,
                    )
                )

        self._safe(_do)

    def record_node(self, task_no: str, node: str, update: dict[str, Any]) -> None:
        """把一个节点的状态更新落库（事件 + agent_runs）。编排层每跑完一个节点调一次。"""
        records = update.get("steps") or []
        step = records[-1] if records else None
        status = getattr(step, "status", "succeeded") if step else "succeeded"
        is_agent = node not in _ORCHESTRATION_NODES

        self.event(
            task_no,
            EVENT_NODE,
            {
                "node": node,
                "agent": node if is_agent else "",
                "status": status,
                "summary": getattr(step, "summary", "") if step else "",
                "tool_calls": list(getattr(step, "tool_calls", []) or []) if step else [],
                "duration_ms": int(getattr(step, "duration_ms", 0) or 0) if step else 0,
                "tokens": int(getattr(step, "tokens", 0) or 0) if step else 0,
                "progress": update.get("progress"),
            },
            level="error" if status == "failed" else "info",
        )

        if is_agent:
            self.record_run(
                task_no=task_no,
                agent_name=node,
                status=status,
                latency_ms=int(getattr(step, "duration_ms", 0) or 0) if step else 0,
                total_tokens=int(getattr(step, "tokens", 0) or 0) if step else 0,
                cost=getattr(step, "cost", 0) or 0 if step else 0,
                tool_call_count=len(getattr(step, "tool_calls", []) or []) if step else 0,
                output_summary=(getattr(step, "summary", "") or "") if step else "",
                error_code=(getattr(step, "error", None) or None) if step else None,
            )

        if update.get("progress") is not None:
            self.update_progress(task_no, status="running", progress=int(update["progress"]), node=node)

    # ------------------------------------------------------------------
    @staticmethod
    def _approval_payload(approval: Any) -> dict[str, Any]:
        if approval is None:
            return {"node": "approval_gate"}
        return {
            "node": "approval_gate",
            "action": getattr(approval, "action", ""),
            "reason": getattr(approval, "reason", ""),
            "risk": getattr(approval, "risk", "medium"),
            "payload": getattr(approval, "payload", {}),
        }

    def _max_seq(self, session: Any, task_id: int) -> int:
        """取某个任务当前的最大 seq。"""
        return int(session.scalar(select(func.max(TaskEvent.seq)).where(TaskEvent.task_id == task_id)) or 0)

    def _current_seq(self, task_no: str) -> int:
        try:
            with session_scope() as session:
                task = session.scalar(select(Task).where(Task.task_no == task_no))
                if task is None:
                    return 0
                return self._max_seq(session, task.id)
        except Exception:  # noqa: BLE001
            return 0

    def _safe(self, fn: Any) -> None:
        if not self.enabled:
            return
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            # 记录失败不能把任务带崩（这是旁路，不是主路），
            # 但**必须计数**：只留一行 warning 的话，
            # "事件被静默丢了一半"这种事在测试和监控里完全看不见。
            self.write_failures += 1
            logger.warning(
                "任务记录写入失败第 %d 次（已忽略）：%s: %s",
                self.write_failures,
                type(exc).__name__,
                exc,
            )


def null_recorder() -> Iterator[TaskRecorder]:
    """什么都不记的 recorder，给单测用。"""
    yield TaskRecorder(enabled=False)


def read_events(task_no: str, *, since_seq: int = 0, limit: int = 500) -> list[dict[str, Any]]:
    """按 seq 增量读事件 —— SSE 与 ``/events`` 接口共用这一个取数函数。

    **字段名刻意和 ``TaskEventOut`` 保持一致**（``node_name`` / ``agent_name`` /
    ``created_at`` …）。一开始这里用的是 ``node`` / ``agent`` / ``occurred_at``，
    结果同一个东西在 REST 和 SSE 两条路上长得不一样 ——
    前端要写两套解析，而且改一处必漏另一处。

    额外多一个 ``payload``：REST 那边不暴露（可能含大字段），
    但 SSE 的消费方需要它渲染细节。
    """
    with session_scope() as session:
        task = session.scalar(select(Task).where(Task.task_no == task_no))
        if task is None:
            return []
        rows = session.scalars(
            select(TaskEvent)
            .where(TaskEvent.task_id == task.id, TaskEvent.seq > since_seq)
            .order_by(TaskEvent.seq)
            .limit(limit)
        ).all()
        return [
            {
                "id": r.id,
                "seq": r.seq,
                "event_type": r.event_type,
                "node_name": r.node_name,
                "agent_name": r.agent_name,
                "level": r.level,
                "message": r.message,
                "duration_ms": r.duration_ms,
                "created_at": _iso(r.created_at),
                "payload": r.payload or {},
            }
            for r in rows
        ]


def task_snapshot(task_no: str) -> dict[str, Any] | None:
    """任务的当前快照，给 API 用。"""
    with session_scope() as session:
        task = session.scalar(select(Task).where(Task.task_no == task_no))
        if task is None:
            return None
        return {
            "task_no": task.task_no,
            "title": task.title,
            "status": task.status,
            "progress": task.progress,
            "current_node": task.current_node,
            "dag": task.dag,
            "result": task.result,
            "approval_payload": task.approval_payload,
            "error_message": task.error_message,
            "llm_calls": task.llm_calls,
            "total_tokens": task.total_tokens,
            "total_cost": float(task.total_cost or 0),
            "started_at": _iso(task.started_at),
            "finished_at": _iso(task.finished_at),
        }


def normalize_dag(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """把库里的计划统一成一种形状，让前端只处理一种。

    ## 为什么放在 recorder 里

    因为 ``tasks.dag`` 就是这一层写进去的 —— "写进去的形状"和
    "读出来的兼容逻辑"放在一起，改一处不会漏另一处。
    （这段从被删掉的 `planner.py` 搬来，逻辑一字未改。）

    ## 库里存着三种历史形状

    * 旧（seed 灌的演示任务）：``{nodes: [{id, name, agent, interrupt}], edges: [[from, to]]}``
    * W4 起的真任务：``{reasoning, steps: [{step_id, agent, goal, depends_on, approval_required}]}``
    * 现在的真任务：``{mode: "todo_list", todos: [...], steps: [...]}`` ——
      它自带 ``steps``，直接原样返回

    兼容逻辑放后端而不是前端：前端一旦要同时认几种形状，
    每处渲染都得写 ``if`` 分支；以后加第四种时改的地方会翻倍。

    不改库数据：已写入的行改造要写迁移脚本，而旧演示任务早晚会被清掉，
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


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


__all__ = [
    "EVENT_APPROVAL",
    "EVENT_CANCELLED",
    "EVENT_FAILED",
    "EVENT_FINISHED",
    "EVENT_NODE",
    "EVENT_PLANNED",
    "EVENT_RESUMED",
    "EVENT_STARTED",
    "TaskRecorder",
    "normalize_dag",
    "null_recorder",
    "read_events",
    "task_snapshot",
]
