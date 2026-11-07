"""客服处理接口 —— 单条 + 批量，都是 SSE 流式。

## 为什么批量也走 SSE 而不是"后台跑 + 轮询"

12 秒/条 × 10 条 = 2 分钟。做成同步 POST 会超时，做成"返回 batch_id 再轮询"
就要引入一个新概念（批次表、批次状态、批次清理）—— 三张表换一个进度条。

SSE 直接把这个需求解决掉，而且**复用项目里已有的那套**（任务事件流、对话
都是这个模式）。前端拿到的是一条条进度，不用新概念。

## 资源归属

`business.py` 拥有**工单的读取**（它是只读路由）；这里拥有**工单的处理**。
不把处理塞进 `business.py` 是因为那个模块的前提是"不改变任何状态"，
加一个会覆盖工单判断的写接口会破坏那个前提。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any, Final

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from fashionops.db import get_db
from fashionops.db.base import utcnow
from fashionops.db.models import Ticket
from fashionops.security.deps import Principal, record_audit, require_permission
from fashionops.security.permissions import PERM_TICKET_HANDLE

logger = logging.getLogger("fashionops.api.service")

router = APIRouter(prefix="/api/v1/service", tags=["service"])

#: 一次批量最多处理多少条。和 CLI 的默认值不同 ——
#: 接口给的是**上限**而不是默认值：调用方必须显式说要做多少，
#: 免得"我就点了一下"结果跑了 60 条、花掉几分钟和一堆 token。
MAX_BATCH = 50

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class HandleBatchRequest(BaseModel):
    limit: int = Field(default=10, ge=1, le=MAX_BATCH, description="最多处理多少条")
    all: bool = Field(
        default=False,
        description="连**已处理过**的也重跑一遍。默认只处理 open/processing 的 —— 重跑会覆盖上一轮判断",
    )


def _sse(events: Iterator[dict[str, Any]]) -> Iterator[str]:
    for event in events:
        yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


def _require_ticket(session: Session, ticket_no: str) -> Ticket:
    row = session.scalar(select(Ticket).where(Ticket.ticket_no == ticket_no))
    if row is None:
        raise HTTPException(status_code=404, detail=f"工单 {ticket_no} 不存在")
    return row


#: 人工处理阶段的取值。**写死在后端**：前端下拉框和后端校验共用一份，
#: 否则两处迟早对不上，而"下拉里能选、提交时报错"是最没必要的那类 bug。
HUMAN_STATUSES: Final[tuple[str, ...]] = (
    "pending",  # 待接单
    "contacted",  # 已联系客户
    "waiting_customer",  # 等客户回复
    "resolved",  # 已解决
)

HUMAN_STATUS_LABEL: Final[dict[str, str]] = {
    "pending": "待接单",
    "contacted": "已联系客户",
    "waiting_customer": "等客户回复",
    "resolved": "已解决",
}


class TicketAssignRequest(BaseModel):
    assignee: str | None = Field(
        default=None,
        max_length=64,
        description="负责人用户名。**传空串/null = 退回未派单**",
    )
    note: str | None = Field(default=None, max_length=255)


class TicketProgressRequest(BaseModel):
    human_status: str = Field(description="人工处理阶段")
    note: str | None = Field(default=None, max_length=255, description="这一条的说明")


@router.post("/tickets/{ticket_no}/assign", summary="派单 / 改派 / 退回未派单")
def assign_ticket(
    ticket_no: str,
    payload: TicketAssignRequest,
    request: Request,
    principal: Principal = Depends(require_permission(PERM_TICKET_HANDLE)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, object]:
    """派单是**唯一改变"这条工单归谁"**的动作，所以必须留痕：
    事后被问"这单为什么压在张三那儿三天"，要能答出是谁什么时候派的。

    允许退回未派单（传空）：改派和撤回是同一个操作的两面，
    分成两个接口反而要处理"退回要不要写原因"这种没必要的分歧。
    """
    ticket = _require_ticket(session, ticket_no)
    previous = ticket.assignee
    new_owner = (payload.assignee or "").strip() or None
    if new_owner == previous:
        return {
            "ticket_no": ticket_no,
            "assignee": ticket.assignee,
            "changed": False,
            "message": "负责人没有变化",
        }

    ticket.assignee = new_owner
    ticket.assigned_at = utcnow() if new_owner else None
    if payload.note:
        ticket.human_note = payload.note[:255]
        ticket.human_updated_at = utcnow()

    record_audit(
        session,
        principal=principal,
        action="ticket.assign",
        resource=f"/api/v1/service/tickets/{ticket_no}",
        detail={"ticket_no": ticket_no, "from": previous, "to": new_owner, "note": payload.note},
        request=request,
    )
    verb = "已派给 " + (new_owner or "（无人）") if new_owner else "已退回未派单"
    return {"ticket_no": ticket_no, "assignee": ticket.assignee, "changed": True, "message": verb}


@router.post("/tickets/{ticket_no}/progress", summary="更新人工处理进度（阶段 + 备注）")
def update_ticket_progress(
    ticket_no: str,
    payload: TicketProgressRequest,
    request: Request,
    principal: Principal = Depends(require_permission(PERM_TICKET_HANDLE)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, object]:
    """记"人做到哪一步了"，**和 Agent 的判断互不覆盖**。

    这是整个客服页最容易设计错的地方：Agent 会写 `resolution`，
    人也在解决同一件事。让两者抢同一个字段的话，
    "机器说解决了、人还在等退款"这个状态就表达不出来 ——
    而它恰恰是最需要被看见的状态。
    """
    ticket = _require_ticket(session, ticket_no)
    status = payload.human_status.strip()
    if status not in HUMAN_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown 阶段 {status!r}。可选：{' / '.join(HUMAN_STATUSES)}",
        )

    ticket.human_status = status
    ticket.human_updated_at = utcnow()
    if payload.note:
        ticket.human_note = payload.note[:255]
    # 第一次有人动它，就默认这个人接了单 ——
    # "更新了进度但没派单"在界面上是自相矛盾的状态
    if not ticket.assignee:
        ticket.assignee = principal.username
        ticket.assigned_at = ticket.human_updated_at

    record_audit(
        session,
        principal=principal,
        action="ticket.progress",
        resource=f"/api/v1/service/tickets/{ticket_no}",
        detail={"ticket_no": ticket_no, "human_status": status, "note": payload.note},
        request=request,
    )
    return {
        "ticket_no": ticket_no,
        "human_status": ticket.human_status,
        "human_status_label": HUMAN_STATUS_LABEL.get(status, status),
        "assignee": ticket.assignee,
        "human_note": ticket.human_note,
        "message": f"进度已更新为「{HUMAN_STATUS_LABEL.get(status, status)}」",
    }


@router.get("/tickets/statuses", summary="人工处理阶段的可选值（含中文名）")
def ticket_statuses(
    _: Principal = Depends(require_permission(PERM_TICKET_HANDLE)),  # noqa: B008
) -> dict[str, object]:
    """下拉框的选项从后端取，不在前端写死 —— 加一个阶段只改后端一处。"""
    return {
        "human_statuses": [{"value": value, "label": HUMAN_STATUS_LABEL.get(value, value)} for value in HUMAN_STATUSES]
    }


# ---------------------------------------------------------------------------
# 单条
# ---------------------------------------------------------------------------
@router.post(
    "/tickets/{ticket_no}/handle",
    summary="处理一条工单（两层：代码快车道 + Agent 兜底，SSE 推进度）",
)
def handle_one(
    ticket_no: str,
    request: Request,
    principal: Principal = Depends(require_permission(PERM_TICKET_HANDLE)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> StreamingResponse:
    # 前置校验必须在构造响应之前 —— 一旦开始流式，状态码已经发出去了
    _require_ticket(session, ticket_no)
    record_audit(
        session,
        principal=principal,
        action="ticket.handle",
        resource=f"/api/v1/service/tickets/{ticket_no}",
        detail={"ticket_no": ticket_no, "mode": "single"},
        request=request,
    )
    return StreamingResponse(_sse(_stream_one(ticket_no)), media_type="text/event-stream", headers=_SSE_HEADERS)


def _stream_one(ticket_no: str) -> Iterator[dict[str, Any]]:
    from fashionops.service.agent import build_service_context, handle_ticket

    yield {"type": "start", "total": 1}
    try:
        outcome = handle_ticket(ticket_no, ctx=build_service_context(), persist=True)
    except Exception as exc:  # noqa: BLE001
        logger.exception("处理工单 %s 失败", ticket_no)
        yield {"type": "error", "detail": f"{type(exc).__name__}: {exc}"}
        yield {"type": "done", "stats": {"total": 1, "failed": 1}}
        return
    yield {"type": "ticket", "outcome": outcome.to_dict()}
    yield {"type": "done", "stats": _stats([outcome.to_dict()])}


# ---------------------------------------------------------------------------
# 批量
# ---------------------------------------------------------------------------
@router.post(
    "/tickets/handle-pending",
    summary="批量处理工单（默认只处理未处理的，SSE 逐条推进度）",
)
def handle_pending(
    payload: HandleBatchRequest,
    request: Request,
    principal: Principal = Depends(require_permission(PERM_TICKET_HANDLE)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> StreamingResponse:
    stmt = select(Ticket.ticket_no).order_by(Ticket.created_at)
    if not payload.all:
        # 默认只碰没处理过的。重跑会**覆盖**上一轮判断（包括人工改过的），
        # 所以必须显式要求，不能默认就干
        stmt = stmt.where(Ticket.status.in_(("open", "processing")))
    stmt = stmt.limit(payload.limit)
    ticket_nos = list(session.scalars(stmt))

    record_audit(
        session,
        principal=principal,
        action="ticket.handle_batch",
        resource="/api/v1/service/tickets/handle-pending",
        detail={"count": len(ticket_nos), "all": payload.all, "limit": payload.limit},
        request=request,
    )
    return StreamingResponse(_sse(_stream_batch(ticket_nos)), media_type="text/event-stream", headers=_SSE_HEADERS)


def _stream_batch(ticket_nos: list[str]) -> Iterator[dict[str, Any]]:
    from fashionops.service.agent import build_service_context, handle_ticket

    yield {"type": "start", "total": len(ticket_nos), "tickets": ticket_nos}
    if not ticket_nos:
        yield {"type": "done", "stats": _stats([])}
        return

    # 复用同一个上下文：网关带回来的限流/熔断状态要在这一批里共享，
    # 不然"批处理"就看不出下游的真实压力
    ctx = build_service_context()
    results: list[dict[str, Any]] = []
    for index, ticket_no in enumerate(ticket_nos, 1):
        try:
            outcome = handle_ticket(ticket_no, ctx=ctx, persist=True)
            payload = outcome.to_dict()
            results.append(payload)
            yield {
                "type": "ticket",
                "index": index,
                "total": len(ticket_nos),
                "outcome": payload,
            }
        except Exception as exc:  # noqa: BLE001
            # 单条失败不能中断整批 —— 否则一条坏工单会让后面 49 条都不处理
            logger.exception("批处理中 %s 失败", ticket_no)
            payload = {"ticket_no": ticket_no, "error": f"{type(exc).__name__}: {exc}"}
            results.append(payload)
            yield {
                "type": "ticket",
                "index": index,
                "total": len(ticket_nos),
                "outcome": payload,
            }
    yield {"type": "done", "stats": _stats(results)}


def _stats(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    """批量的汇总数字。入参就是 `outcome.to_dict()` 的列表。

    **快车道占比和转人工率要单独报** —— 它们是这个架构的两个核心指标：
    快车道占比说明"省了多少次模型调用"，转人工率说明"有多少必须人来"。
    只报一个"处理了 N 条"等于什么都没说。
    """
    total = len(outcomes)
    fast = sum(1 for r in outcomes if r.get("lane") == "fast")
    return {
        "total": total,
        "fast_lane": fast,
        "agent_lane": total - fast,
        "escalated": sum(1 for r in outcomes if r.get("escalated")),
        "failed": sum(1 for r in outcomes if r.get("error")),
        "tokens": sum(int(r.get("tokens") or 0) for r in outcomes),
    }


__all__ = ["router"]
