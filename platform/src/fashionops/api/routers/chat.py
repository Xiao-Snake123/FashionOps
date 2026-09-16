"""对话接口 —— 会话管理 + 流式消息 + 对话内审批。

## 为什么发消息用 POST + ``text/event-stream``，而不是原生 ``EventSource``

浏览器原生的 ``EventSource`` **只能发 GET、不能带自定义请求头** ——
所以任务事件流那边不得不把令牌放在查询串里（`?token=`），代价是令牌会进
访问日志、浏览器历史、Referer。

对话这边用 ``fetch`` + ``ReadableStream`` 读流：POST 能带上
``Authorization`` 头，也就能正常表达"发一条消息"这个语义。
代价是前端要自己解析 SSE 帧格式 —— 但那只是几行 split，换掉一个凭据泄漏面很划算。

代价二：断线不自动重连。**但这个场景不需要**：消息在生成过程中就落库了，
用户刷新页面时走 ``GET /conversations/{no}`` 就能拿到已经生成的部分。

## 事件格式

每个事件是一行 ``data: {json}``。JSON 里的 ``type`` 字段区分类型
（见 `chat/agent.py` 的表格）。**刻意不写 SSE 的 ``event:`` 名字字段** ——
理由和任务事件流一样：类型已经写在 data 里了，再写一遍就是同一份信息存两处，
代价是前端要为每个类型各注册一个监听器，于是后端新增类型时前端会静默漏掉。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from fashionops.chat import agent, store
from fashionops.db import get_db
from fashionops.security.deps import Principal, record_audit, require_permission
from fashionops.security.permissions import PERM_CHAT_USE

logger = logging.getLogger("fashionops.api.chat")

router = APIRouter(prefix="/api/v1/conversations", tags=["chat"])

#: 单条用户消息长度上限。和 LLM 的上下文预算对齐 ——
#: 一条 4000 字的消息已经够占满小半窗口了，再长更可能是误粘贴。
MAX_MESSAGE_CHARS = 4000


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class CreateConversationRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class ApprovalDecisionRequest(BaseModel):
    """对话内审批的决定。

    **没有 ``by`` 字段** —— 决定人取自令牌。和任务审批同一个理由：
    允许调用方声明"我是谁"，审批留痕就没有意义了。
    """

    approved: bool
    reason: str = Field(default="", max_length=500)


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------
def _require_conversation(session: Session, conversation_no: str, principal: Principal) -> Any:
    """取会话并做归属检查。

    ## 为什么在**进流之前**检查，而不是在生成器里

    一旦开始 ``StreamingResponse``，HTTP 状态码已经发出去了（200）。
    生成器里再抛 404/403 只能变成一个 ``error`` 事件 —— 前端拿到的是
    "200 然后流里说没权限"，而不是一个干净的 403。

    所以"这场对话存不存在、是不是你的"必须在构造响应之前判完。
    这也意味着**每个流式端点都要先做一次同步的前置检查**。
    """
    row = store.get_conversation(session, conversation_no)
    if row is None:
        raise HTTPException(status_code=404, detail=f"会话 {conversation_no} 不存在")
    # 超级管理员可以看所有会话（运维需要排查问题），其余人只能看自己的。
    # **不是自己的一律 404 而不是 403**：403 等于确认了"这个编号存在"，
    # 会让人能枚举出系统里有多少场对话。
    if not principal.is_superuser and row.user_id != principal.user_id:
        raise HTTPException(status_code=404, detail=f"会话 {conversation_no} 不存在")
    return row


def _sse(events: Iterator[dict[str, Any]]) -> Iterator[str]:
    """把事件字典编成 SSE 帧。"""
    for event in events:
        payload = json.dumps(event, ensure_ascii=False, default=str)
        yield f"data: {payload}\n\n"


#: 流式响应头。``X-Accel-Buffering: no`` 是给 nginx 看的 ——
#: 不关掉反代缓冲的话事件会被攒着一起发，"实时"就没了。
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="新建对话（需要 chat:use 权限）",
)
def create_conversation(
    payload: CreateConversationRequest,
    request: Request,
    principal: Principal = Depends(require_permission(PERM_CHAT_USE)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    from fashionops.config import get_settings

    # 直接拿返回的字典，**不能建完再查一次**：
    # 路由的 session 在鉴权阶段已经读过数据，REPEATABLE READ 的快照已固定，
    # 它看不见另一个事务刚提交的这一行（详见 store.create_conversation 的说明）
    conversation = store.create_conversation(
        username=principal.username,
        user_id=principal.user_id,
        title=payload.title or "新对话",
        model=get_settings().model_name,
    )
    record_audit(
        session,
        principal=principal,
        action="chat.create",
        resource=f"/api/v1/conversations/{conversation['conversation_no']}",
        detail={"conversation_no": conversation["conversation_no"]},
        request=request,
    )
    return {"conversation": conversation}


@router.get("", summary="我的会话列表（需要 chat:use 权限）")
def list_conversations(
    principal: Principal = Depends(require_permission(PERM_CHAT_USE)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    # 管理员看全部（排查用），其余人只看自己的。
    # 传 None 给 store 表示"不限归属"。
    user_id = None if principal.is_superuser else principal.user_id
    rows = store.list_conversations(session, user_id=user_id, limit=limit)
    return {
        "items": [store.serialize_conversation(r) for r in rows],
        "total": len(rows),
    }


@router.get("/{conversation_no}", summary="会话详情 + 历史消息")
def get_conversation(
    conversation_no: str,
    principal: Principal = Depends(require_permission(PERM_CHAT_USE)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    row = _require_conversation(session, conversation_no, principal)
    messages = store.history(session, row.id)
    pending = store.pending_for_conversation(session, row.id)
    return {
        "conversation": store.serialize_conversation(row),
        "messages": [store.serialize_message(m) for m in messages],
        # 待审批的也一起返回：用户刷新页面后要能看到"还有一张卡片没点"
        "pending_approvals": [store.serialize_approval(a) for a in pending],
    }


# ---------------------------------------------------------------------------
# 发消息 / 审批
# ---------------------------------------------------------------------------
@router.post(
    "/{conversation_no}/messages",
    summary="发一条消息（SSE 流式返回 Agent 的回答）",
)
def send_message(
    conversation_no: str,
    payload: SendMessageRequest,
    request: Request,
    principal: Principal = Depends(require_permission(PERM_CHAT_USE)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> StreamingResponse:
    # 前置检查必须在构造响应之前 —— 见 _require_conversation 的说明
    _require_conversation(session, conversation_no, principal)

    record_audit(
        session,
        principal=principal,
        action="chat.message",
        resource=f"/api/v1/conversations/{conversation_no}",
        # 只记长度不记内容：对话内容本身已经在 chat_messages 里了，
        # 审计表存一份全文等于把隐私数据抄第二份，而且两份还要保持一致
        detail={"conversation_no": conversation_no, "chars": len(payload.content)},
        request=request,
    )

    return StreamingResponse(
        _sse(
            agent.stream_turn(
                conversation_no=conversation_no,
                principal=principal,
                user_text=payload.content,
            )
        ),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post(
    "/{conversation_no}/approvals/{approval_no}",
    summary="确认 / 驳回对话里的写操作（SSE 流式返回后续回答）",
)
def decide_approval(
    conversation_no: str,
    approval_no: str,
    payload: ApprovalDecisionRequest,
    request: Request,
    principal: Principal = Depends(require_permission(PERM_CHAT_USE)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> StreamingResponse:
    _require_conversation(session, conversation_no, principal)

    record_audit(
        session,
        principal=principal,
        action="chat.approve" if payload.approved else "chat.reject",
        resource=f"/api/v1/conversations/{conversation_no}/approvals/{approval_no}",
        detail={"approval_no": approval_no, "approved": payload.approved, "reason": payload.reason},
        # 驳回记 `blocked` 而不是 `success`/`error`：它不是失败（系统没坏），
        # 但也不该算成功执行了一次动作。统计"被拦下多少次"要靠这个值分组。
        status="success" if payload.approved else "blocked",
        request=request,
    )

    try:
        events = agent.resolve_approval(
            conversation_no=conversation_no,
            principal=principal,
            approval_no=approval_no,
            approved=payload.approved,
            reason=payload.reason,
        )
        # 这里不能直接在响应里生成 —— 生成器是惰性的，
        # `resolve_approval` 前面的校验要等到第一次 next() 才跑，
        # 那时状态码已经发出去了。所以先手工吃第一个事件，
        # 让那些"审批单不存在 / 已处理过 / 没权限"的异常在**响应之前**抛出。
        first = next(events)
    except StopIteration:
        raise HTTPException(status_code=409, detail="这条审批没有可继续的内容") from None
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    def _with_first() -> Iterator[str]:
        yield from _sse(iter([first]))
        yield from _sse(events)

    return StreamingResponse(_with_first(), media_type="text/event-stream", headers=_SSE_HEADERS)


__all__ = ["router"]
