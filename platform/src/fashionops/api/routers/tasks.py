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

## 鉴权：权限是**两层**的

1. **接口层**：谁能调这个接口（`require_permission`）
2. **传导层**：Agent 跑起来之后能调哪些工具 ——
   这里用的是 `effective_scopes(用户权限, Agent 权限集)` 的**交集**

只做第 1 层是不够的。第 1 层只挡住了"直接调接口"，
而用户完全可以说一句"帮我发封邮件给所有人" —— 模型会去调 `mail.send`，
用的是 Agent 的身份。如果 Agent 的权限不受提交人影响，
那么一个只有 `task:submit` 的人就能达成他没有的 `mail:send`。
**交集那行代码是整个 RBAC 设计里唯一真正防住越权的地方。**

## SSE 的令牌怎么传

浏览器的原生 `EventSource` **不能设置请求头**，所以 `?token=` 是唯一选择。
详见 `security/deps.get_stream_principal` 里的权衡说明。
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fashionops.agents.execution import (
    TASK_AGENT,
    TaskBusyError,
    claim,
    enqueue_resume,
    is_running,
    release,
    submit,
)
from fashionops.api.schemas import (
    Page,
    TaskDetailOut,
    TaskEventOut,
    TaskOut,
    TaskTodoOut,
)
from fashionops.db import get_db
from fashionops.db.models import Task, TaskTodo
from fashionops.security.deps import (
    StreamPrincipal,
    record_audit,
    require_permission,
)
from fashionops.security.permissions import (
    PERM_TASK_APPROVE,
    PERM_TASK_READ,
    PERM_TASK_SUBMIT,
)
from fashionops.security.service import Principal, effective_scopes

logger = logging.getLogger("fashionops.api.tasks")

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

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
    """提交任务的入参。

    **刻意没有 `created_by` 字段。** 提交人一律取自访问令牌 ——
    让请求体声明"我是谁"等于把审计变成自证，"谁交办的"这个问题就不可信了。
    （旧版有这个字段，改成鉴权后删掉了。前端如果还传，pydantic 会忽略。）
    """

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


class ApprovalDecision(BaseModel):
    """审批决定。

    **同样没有 `by` 字段**：审批人取自令牌。
    否则任何人都能以别人的名义批准一封要发出去的邮件，
    而 HITL 闸口存在的全部意义就是"**谁**批准了"。
    """

    approved: bool = Field(description="批准 / 驳回")
    reason: str = Field(default="", max_length=500, description="驳回理由或备注")


class TaskActionResponse(BaseModel):
    task_no: str
    status: str
    message: str


# ---------------------------------------------------------------------------
# 提交 / 确认
# ---------------------------------------------------------------------------
@router.post(
    "",
    response_model=TaskActionResponse,
    summary="提交任务（后台异步执行，需要 task:submit 权限）",
)
def submit_task(
    payload: SubmitTaskRequest,
    request: Request,
    principal: Principal = Depends(require_permission(PERM_TASK_SUBMIT)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> TaskActionResponse:
    from fashionops.tools import AGENT_SCOPES

    # 用户权限向下传导：Agent 只能用"提交人本来就有的"权限。
    # 这个交集是**唯一真实的越权防线**，不能省。
    granted = effective_scopes(principal, AGENT_SCOPES[TASK_AGENT])

    try:
        task_no = submit(
            user_request=payload.user_request,
            task_type=payload.task_type,
            # **提交人来自令牌，不来自请求体**。用请求体里的值等于让调用方
            # 自己声明"我是谁"，审计就完全失去意义了。
            created_by=principal.username,
            created_by_id=principal.user_id,
            scopes=granted,
            task_no=payload.task_no,
        )
    except TaskBusyError as exc:
        # 领域异常 → HTTP 语义的翻译在这里做。
        # execution 那一层不知道 HTTP 是什么，它也不能知道。
        raise HTTPException(
            status_code=409,
            detail=f"任务 {exc.task_no} 正在执行中，请等它跑完或换一个任务号",
        ) from exc

    record_audit(
        session,
        principal=principal,
        action="task.submit",
        resource=f"/api/v1/tasks/{task_no}",
        detail={
            "task_no": task_no,
            "task_type": payload.task_type,
            "request": payload.user_request[:500],
            # 把交集结果记进审计：出了越权争议时，
            # "当时他有哪些权限"和"Agent 实际拿到了哪些"都要能回答
            "granted_scopes": sorted(granted),
            "via": "http",
        },
        request=request,
    )

    return TaskActionResponse(
        task_no=task_no,
        status="pending",
        message="任务已受理。订阅 GET /api/v1/tasks/{task_no}/stream 看实时进度",
    )


@router.post(
    "/{task_no}/approve",
    response_model=TaskActionResponse,
    summary="人工确认（批准 / 驳回，需要 task:approve 权限）",
)
def approve_task(
    task_no: str,
    payload: ApprovalDecision,
    request: Request,
    principal: Principal = Depends(require_permission(PERM_TASK_APPROVE)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> TaskActionResponse:
    """批准/驳回挂在人工闸口的任务。

    ## 审批人**只能来自令牌**

    请求体里没有 `by` 字段，这是刻意的。如果让前端传"我是谁"，
    那么任何人都能以别人的名义批准一封要发出去的邮件 ——
    而 HITL 闸口存在的全部意义就是"**谁**批准了"。审计里那个名字必须可信。

    ## 为什么批准和驳回都要记审计

    驳回也是决策。"这封邮件为什么没发出去"和"为什么发出去了"
    是同等重要的两个问题，只记批准的话，前一个问题的答案就丢了。
    """
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

    # ---- 职责分离（可配置）----
    # 默认关闭：小团队里提交人往往就是唯一能批的人，硬拦会让流程走不动。
    # 但它**必须能被打开** —— 有合规要求时（比如财务审批）这条是强制的。
    from fashionops.config import get_settings

    settings = get_settings()
    if (
        settings.approval_require_separate_user
        and payload.approved
        and task.created_by
        and task.created_by == principal.username
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                "不能批准自己提交的任务（FASHIONOPS_APPROVAL_REQUIRE_SEPARATE_USER=true）。"
                "请让另一位有审批权限的同事处理"
            ),
        )

    try:
        claim(task_no)
    except TaskBusyError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"任务 {exc.task_no} 正在执行中，请等它跑完再确认",
        ) from exc

    try:
        from fashionops.db.base import utcnow

        # 审批结果直接落库（除了 recorder 里的审批请求载荷之外，
        # 这里也记一份"人"的维度），这样"谁批的"不依赖事件流的完整性
        task.approved_by = principal.username
        task.approved_by_id = principal.user_id
        task.approved_at = utcnow()
        task.approval_reason = payload.reason or ""
        session.flush()

        record_audit(
            session,
            principal=principal,
            action="task.approve" if payload.approved else "task.reject",
            resource=f"/api/v1/tasks/{task_no}",
            detail={
                "task_no": task_no,
                "approved": payload.approved,
                "reason": payload.reason,
                "task_owner": task.created_by,
            },
            # 驳回记 `blocked` 而不是 `success`/`error`：
            # 它不是失败（系统没坏），但也不该算成功执行了一次动作。
            # 统计"被拦下多少次"时，这个值是唯一的分组依据。
            status="success" if payload.approved else "blocked",
            request=request,
        )
    except Exception:
        release(task_no)
        raise

    enqueue_resume(
        task_no,
        {
            "approved": payload.approved,
            "by": principal.username,
            # 审批人的**数字 id** 也要传：审批单上存的是 user_id（外键），
            # 只存名字的话，那个人改名/被删之后这条记录就对不上人了。
            "by_id": principal.user_id,
            "reason": payload.reason,
        },
    )
    verb = "批准" if payload.approved else "驳回"
    return TaskActionResponse(
        task_no=task_no, status="running", message=f"已{verb}，任务从断点继续执行"
    )


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
@router.get("", response_model=Page[TaskOut], summary="任务列表（需要 task:read 权限）")
def list_tasks(
    principal: Principal = Depends(require_permission(PERM_TASK_READ)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    status: str | None = None,
    task_type: str | None = None,
) -> Page[TaskOut]:
    del principal
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


@router.get(
    "/{task_no}",
    response_model=TaskDetailOut,
    summary="任务详情（含 DAG、挂起状态、审批载荷）",
)
def get_task(
    task_no: str,
    principal: Principal = Depends(require_permission(PERM_TASK_READ)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> TaskDetailOut:
    del principal
    task = session.scalar(select(Task).where(Task.task_no == task_no))
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_no} 不存在")

    todos = session.scalars(
        select(TaskTodo).where(TaskTodo.task_id == task.id).order_by(TaskTodo.seq)
    ).all()

    detail = TaskDetailOut.model_validate(task)
    # 这两个是**进程内**状态，不在库里，得手工补 ——
    # status 落库有延迟，光看它会以为任务还在跑
    detail.is_running = is_running(task_no)
    detail.is_pending = task.status in _RUNNING_STATES
    detail.todos = [TaskTodoOut.model_validate(row) for row in todos]
    return detail


@router.get(
    "/{task_no}/events",
    response_model=list[TaskEventOut],
    summary="任务事件流（增量拉取，断线后靠 since_seq 续上）",
)
def get_events(
    task_no: str,
    principal: Principal = Depends(require_permission(PERM_TASK_READ)),  # noqa: B008
    since_seq: int = Query(0, ge=0, description="只取 seq 大于该值的事件"),
) -> list[TaskEventOut]:
    del principal
    # 直接复用 SSE 的取数函数 —— 两条路走同一份实现，
    # 避免"REST 和 SSE 字段名不一样"这种要写两套解析的坑
    from fashionops.agents.recorder import read_events

    return [TaskEventOut.model_validate(e) for e in read_events(task_no, since_seq=since_seq)]


@router.get("/{task_no}/stream", summary="SSE 实时进度流（令牌从 ?token= 传）")
def stream_task(
    task_no: str,
    principal: StreamPrincipal,
    since_seq: int = Query(0, ge=0, description="只推 seq 大于该值的事件"),
    last_event_id: str | None = Query(
        None, alias="Last-Event-ID", description="浏览器 EventSource 断线重连时自动带上"
    ),
) -> StreamingResponse:
    """Server-Sent Events。

    ``Last-Event-ID`` 会被当成 ``since_seq`` 用 —— 浏览器原生 ``EventSource``
    重连时会自动带上它，所以**前端不用写重连逻辑**，也不会重复收到看过的事件。

    **令牌只能从查询串传**：原生 `EventSource` 不支持自定义请求头。
    代价和缓解手段见 `security/deps.get_stream_principal`。
    """
    if not principal.has(PERM_TASK_READ):
        # SSE 端点手工判权限（不用 require_permission）是因为它的主体来自
        # 另一个依赖（StreamPrincipal）。用 403 而不是 401：
        # EventSource 收到 401 会**无限重连**，403 才会放弃 ——
        # 用错状态码会让一个没权限的页面挂在那里疯狂重连。
        raise HTTPException(status_code=403, detail="没有查看任务事件的权限")

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
        # ``id`` 供断线续传（浏览器重连时会自动带上 ``Last-Event-ID``）。
        #
        # 刻意**不写** ``event: <名字>``：事件类型已经在 ``data.event_type`` 里了，
        # 再在 SSE 的 ``event`` 字段上写一遍就是同一份信息存两处 ——
        # 代价是前端得为每个类型各注册一个监听器，于是后端新增一种事件时
        # 前端会**静默漏掉**（不报错，只是不显示）。不具名的事件全部落到
        # ``EventSource.onmessage``，一个 handler 就能接住任意类型。
        return f"id: {event['seq']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

    while time.monotonic() < deadline:
        events = read_events(task_no, since_seq=cursor)
        for event in events:
            cursor = int(event["seq"])
            yield frame(event)
        idle_rounds = 0 if events else idle_rounds + 1

        snapshot = task_snapshot(task_no)
        if snapshot is None:
            payload = json.dumps(
                # ``type`` 是控制帧的判别字段；数据帧用的是 ``event_type``，两者不重叠
                {"type": "task_error", "detail": f"任务 {task_no} 不存在"},
                ensure_ascii=False,
            )
            yield f"data: {payload}\n\n"
            return

        if snapshot["status"] not in _RUNNING_STATES:
            # 收尾前再补读一次：任务可能在本次轮询的间隙刚写完最后两条事件
            for event in read_events(task_no, since_seq=cursor):
                cursor = int(event["seq"])
                yield frame(event)
            done = json.dumps(
                {"type": "done", "status": snapshot["status"], "seq": cursor}, ensure_ascii=False
            )
            yield f"data: {done}\n\n"
            return

        # 心跳：长时间没事件就发个注释行，防止中间代理掐掉空闲连接。
        # SSE 里以 ":" 开头的行是注释，客户端会忽略。
        if idle_rounds and idle_rounds % 25 == 0:
            yield ": keep-alive\n\n"

        time.sleep(_SSE_POLL_S)


# ---------------------------------------------------------------------------
def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


#: 组装执行上下文那一套（原来的 `build_runner`）已经删掉了：
#: 新架构里"跑任务"就是 `agents/loop.run_task`，没有图要编译、
#: 没有 checkpointer 要传 —— 所以这里不再有对外出口。
__all__ = ["router"]
