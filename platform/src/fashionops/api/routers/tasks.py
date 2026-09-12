"""任务接口 —— 提交、查询、人工确认、SSE 实时推流。

## 这是项目里**第一组写接口**

前面那些接口全部只读，因为它们只是"让前端有数据可渲染"。
从这里开始，接口会**真的触发 Agent 跑任务**，所以有几条硬规矩：

1. **写操作走工具网关**（和 Agent 用同一套限流/审计/权限）。
   直连 Session 的话，前端一个按钮就能绕过所有治理 ——
   而"绕过治理的入口"只要存在一个，治理就等于没有。
2. **跑图不能在请求线程里同步跑完**。一次任务几十秒，
   同步跑会超时，浏览器刷新也断了。所以提交后立刻返回 ``task_no``，
   任务在后台线程里跑，进度通过 SSE 推给前端。
3. **SSE 必须支持断线重连**。``task_events`` 有单调递增的 ``seq``，
   客户端带上 ``Last-Event-ID`` 就能只收增量 ——
   这是"别把实时推送做成一次性订阅"的关键：网络抖一下就要重拉全量、
   甚至错过中间状态，体验会很难受。

## 为什么用后台线程而不是 Celery

这一阶段的目标是"把链路跑通、把数据落库"，不是"上生产级队列"。
``ThreadPoolExecutor`` 够用且不引入新中间件。
真要多机部署时把 ``_EXECUTOR.submit`` 换成投递到队列即可 ——
任务状态本来就在数据库和 checkpoint 里，不依赖进程内存。
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fashionops.api.schemas import Page, TaskEventOut, TaskOut
from fashionops.db import get_db
from fashionops.db.models import Task, TaskEvent

logger = logging.getLogger("fashionops.api.tasks")

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

#: 后台任务线程池。刻意开小：等模型是 IO 密集型，
#: 但并发太多会同时打满模型配额和内存，反而更慢。
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="task")

#: 正在跑的任务号。防止同一任务被并发提交两次 ——
#: 同一个 thread_id 上跑两个图会让 checkpoint 状态互相覆盖。
_RUNNING: set[str] = set()
_RUNNING_LOCK = threading.Lock()

#: SSE 轮询间隔。任务量级是"几十秒一次状态变化"，0.4 秒足够灵敏又不吵。
_SSE_POLL_S = 0.4
#: SSE 连接最长存活时间。到点让客户端重连 —— 有 seq 就不会丢事件，
#: 比让连接无限挂着更省资源。
_SSE_MAX_S = 600

#: 这些状态表示"任务还在进行中"
_RUNNING_STATES = ("pending", "running", "waiting_approval")


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------
class SubmitTaskRequest(BaseModel):
    user_request: str = Field(
        min_length=2,
        max_length=2000,
        description="用户的自然语言需求，例如「帮我选 3 款秋季女装并出报表」",
    )
    task_type: str = Field(default="generic", max_length=64)
    task_no: str | None = Field(
        default=None,
        max_length=64,
        description="自定义任务号。留空自动生成；传已存在的号会重跑该任务",
    )
    created_by: str = Field(default="user", max_length=64)


class ApprovalDecision(BaseModel):
    approved: bool = Field(description="批准 / 驳回")
    by: str = Field(default="user", max_length=64, description="审批人")
    reason: str = Field(default="", max_length=500, description="驳回理由或备注")


class TaskActionResponse(BaseModel):
    task_no: str
    status: str
    message: str


# ---------------------------------------------------------------------------
# 组装 runner
# ---------------------------------------------------------------------------
def build_runner(task_no: str, *, task_id: int | None = None) -> Any:
    """组装一个能跑图并记账的 TaskRunner。抽成函数是为了让测试能直接调。"""
    from fashionops.agents.checkpoint import get_checkpointer
    from fashionops.agents.recorder import TaskRecorder
    from fashionops.agents.runner import TaskRunner
    from fashionops.agents.state import RunContext
    from fashionops.config import get_settings
    from fashionops.gateway import (
        ToolGateway,
        get_circuit_breaker,
        get_rate_limiter,
        registry,
    )
    from fashionops.llm import get_model_client
    from fashionops.tools import AGENT_SCOPES, register_all_tools

    # 注册表是进程级单例，重复注册会抛异常，所以先判空
    if not len(registry):
        register_all_tools(registry)

    settings = get_settings()
    gateway = ToolGateway(
        registry,
        rate_limiter=get_rate_limiter(settings.cache_backend),
        breaker=get_circuit_breaker(settings.cache_backend),
    )
    ctx = RunContext(
        gateway=gateway,
        model=get_model_client(settings),
        actor=f"orchestrator:{task_no}",
        scopes=AGENT_SCOPES["orchestrator"],
        task_id=task_id,
    )
    return TaskRunner(ctx, recorder=TaskRecorder(), checkpointer=get_checkpointer(settings))


# ---------------------------------------------------------------------------
# 提交 / 确认
# ---------------------------------------------------------------------------
@router.post("", response_model=TaskActionResponse, summary="提交任务（后台异步执行）")
def submit_task(payload: SubmitTaskRequest) -> TaskActionResponse:
    task_no = payload.task_no or _new_task_no()
    _claim(task_no)
    _EXECUTOR.submit(
        _run_guarded, task_no, payload.user_request, payload.task_type, payload.created_by
    )
    return TaskActionResponse(
        task_no=task_no,
        status="running",
        message="任务已提交。订阅 GET /api/v1/tasks/{task_no}/stream 看实时进度",
    )


@router.post(
    "/{task_no}/approve", response_model=TaskActionResponse, summary="人工确认（批准 / 驳回）"
)
def approve_task(
    task_no: str, payload: ApprovalDecision, session: Session = Depends(get_db)
) -> TaskActionResponse:
    task = session.scalar(select(Task).where(Task.task_no == task_no))
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_no} 不存在")
    # 用数据库里的 status 判断"是不是在等确认"，而不是去构图查 pending 节点 ——
    # status 是 recorder 写进去的事实，读一次就够，不必为此构造一张图
    if task.status != "waiting_approval":
        raise HTTPException(
            status_code=409,
            detail=f"任务 {task_no} 当前状态是 {task.status}，没有等待确认的步骤",
        )

    _claim(task_no)
    _EXECUTOR.submit(
        _resume_guarded,
        task_no,
        {"approved": payload.approved, "by": payload.by, "reason": payload.reason},
    )
    verb = "批准" if payload.approved else "驳回"
    return TaskActionResponse(
        task_no=task_no, status="running", message=f"已{verb}，任务从断点继续执行"
    )


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
@router.get("", response_model=Page[TaskOut], summary="任务列表")
def list_tasks(
    session: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    status: str | None = None,
    task_type: str | None = None,
) -> Page[TaskOut]:
    stmt = select(Task)
    if status:
        stmt = stmt.where(Task.status == status)
    if task_type:
        stmt = stmt.where(Task.task_type == task_type)
    stmt = stmt.order_by(Task.created_at.desc())

    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(stmt.offset((page - 1) * page_size).limit(page_size)).all()
    return Page[TaskOut](
        items=[TaskOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{task_no}", summary="任务详情（含 DAG、执行轨迹、挂起状态）")
def get_task(task_no: str, session: Session = Depends(get_db)) -> dict[str, Any]:
    task = session.scalar(select(Task).where(Task.task_no == task_no))
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_no} 不存在")

    return {
        "task_no": task.task_no,
        "title": task.title,
        "user_request": task.user_request,
        "task_type": task.task_type,
        "status": task.status,
        "progress": task.progress,
        "current_node": task.current_node,
        "dag": task.dag,
        "result": task.result,
        # 这个字段非空 = 任务停在人工闸口，前端据此弹确认框
        "approval_payload": task.approval_payload,
        "error_message": task.error_message,
        "llm_calls": task.llm_calls,
        "total_tokens": task.total_tokens,
        "total_cost": float(task.total_cost or 0),
        "is_running": task_no in _RUNNING,
        "is_pending": task.status in _RUNNING_STATES,
        "started_at": _iso(task.started_at),
        "finished_at": _iso(task.finished_at),
        "created_at": _iso(task.created_at),
    }


@router.get(
    "/{task_no}/events",
    response_model=list[TaskEventOut],
    summary="任务事件流（增量拉取，断线后靠 since_seq 续上）",
)
def get_events(
    task_no: str,
    session: Session = Depends(get_db),
    since_seq: int = Query(0, ge=0, description="只取 seq 大于该值的事件"),
) -> list[TaskEventOut]:
    task = session.scalar(select(Task).where(Task.task_no == task_no))
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_no} 不存在")
    rows = session.scalars(
        select(TaskEvent)
        .where(TaskEvent.task_id == task.id, TaskEvent.seq > since_seq)
        .order_by(TaskEvent.seq)
    ).all()
    return [TaskEventOut.model_validate(r) for r in rows]


@router.get("/{task_no}/stream", summary="SSE 实时进度流")
def stream_task(
    task_no: str,
    since_seq: int = Query(0, ge=0, description="只推 seq 大于该值的事件"),
    last_event_id: str | None = Query(
        None, alias="Last-Event-ID", description="浏览器 EventSource 断线重连时自动带上"
    ),
) -> StreamingResponse:
    """Server-Sent Events。

    ``Last-Event-ID`` 会被当成 ``since_seq`` 用 —— 浏览器原生 ``EventSource``
    重连时会自动带上它，所以**前端不用写重连逻辑**，也不会重复收到看过的事件。
    """
    cursor = since_seq
    if last_event_id:
        # 头里可能是任意字符串，解析失败就忽略 —— 退回 since_seq 即可，
        # 最多少收几条事件，不该因此 500
        with contextlib.suppress(ValueError):
            cursor = max(cursor, int(last_event_id))

    return StreamingResponse(
        _event_source(task_no, cursor),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 关掉反代缓冲，否则事件会被攒着一起发，"实时"就没了
            "X-Accel-Buffering": "no",
        },
    )


def _event_source(task_no: str, cursor: int) -> Any:
    """逐条产出 SSE 帧。任务结束或超时后收尾退出。"""
    from fashionops.agents.recorder import read_events, task_snapshot

    deadline = time.monotonic() + _SSE_MAX_S
    idle_rounds = 0

    def frame(event: dict[str, Any]) -> str:
        # SSE 规范：id 供断线续传，event 是事件名，data 是载荷
        return (
            f"id: {event['seq']}\n"
            f"event: {event['event_type']}\n"
            f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        )

    while time.monotonic() < deadline:
        events = read_events(task_no, since_seq=cursor)
        for event in events:
            cursor = int(event["seq"])
            yield frame(event)
        idle_rounds = 0 if events else idle_rounds + 1

        snapshot = task_snapshot(task_no)
        if snapshot is None:
            payload = json.dumps({"detail": f"任务 {task_no} 不存在"}, ensure_ascii=False)
            yield f"event: error\ndata: {payload}\n\n"
            return

        if snapshot["status"] not in _RUNNING_STATES:
            # 收尾前再补读一次：任务可能在本次轮询的间隙刚写完最后两条事件
            for event in read_events(task_no, since_seq=cursor):
                cursor = int(event["seq"])
                yield frame(event)
            done = json.dumps({"status": snapshot["status"], "seq": cursor}, ensure_ascii=False)
            yield f"event: done\ndata: {done}\n\n"
            return

        # 心跳：长时间没事件就发个注释行，防止中间代理掐掉空闲连接。
        # SSE 里以 ":" 开头的行是注释，客户端会忽略。
        if idle_rounds and idle_rounds % 25 == 0:
            yield ": keep-alive\n\n"

        time.sleep(_SSE_POLL_S)


# ---------------------------------------------------------------------------
# 后台执行（异常必须在这里兜住）
# ---------------------------------------------------------------------------
def _run_guarded(task_no: str, user_request: str, task_type: str, created_by: str) -> None:
    """后台线程里跑任务。

    **异常必须在这里兜住。** 线程里抛出去的异常没人接，
    表现是"任务永远停在 running"而日志里什么都没有 —— 最难查的一类问题。
    """
    runner = None
    try:
        runner = build_runner(task_no)
        runner.submit(
            task_no=task_no,
            user_request=user_request,
            task_type=task_type,
            created_by=created_by,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("任务 %s 执行失败", task_no)
        _mark_failed(task_no, runner, exc)
    finally:
        _release(task_no)


def _resume_guarded(task_no: str, decision: dict[str, Any]) -> None:
    runner = None
    try:
        runner = build_runner(task_no)
        runner.resume(task_no=task_no, decision=decision)
    except Exception as exc:  # noqa: BLE001
        logger.exception("任务 %s 恢复失败", task_no)
        _mark_failed(task_no, runner, exc)
    finally:
        _release(task_no)


def _mark_failed(task_no: str, runner: Any, exc: Exception) -> None:
    """尽力把失败状态写进去。写不进去也至少要留下日志。"""
    try:
        from fashionops.agents.recorder import TaskRecorder

        recorder = runner.recorder if runner is not None else TaskRecorder()
        recorder.finish(
            task_no=task_no, status="failed", error_message=f"{type(exc).__name__}: {exc}"
        )
    except Exception:  # noqa: BLE001
        logger.exception("任务 %s 的失败状态也没能写进数据库", task_no)


# ---------------------------------------------------------------------------
def _claim(task_no: str) -> None:
    with _RUNNING_LOCK:
        if task_no in _RUNNING:
            raise HTTPException(
                status_code=409,
                detail=f"任务 {task_no} 正在执行中，请等它跑完或换一个任务号",
            )
        _RUNNING.add(task_no)


def _release(task_no: str) -> None:
    with _RUNNING_LOCK:
        _RUNNING.discard(task_no)


def _new_task_no() -> str:
    from fashionops.db.base import utcnow

    return f"T{utcnow():%y%m%d%H%M%S}{uuid.uuid4().hex[:4].upper()}"


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


__all__ = ["build_runner", "router"]
