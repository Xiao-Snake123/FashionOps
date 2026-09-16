"""审批单的落库 —— **任务侧**的读写。

对话侧的那套在 ``chat/store.py``（它和消息表绑在一起，读写都围绕
``conversation_id`` + ``message_id``）。任务侧不需要消息，只需要
"哪一个任务的第几步、等的是哪次工具调用"，所以单独一份，放在
``approval`` 包里而不是塞进 ``chat``：任务执行链不该反向依赖对话链。

两边共用**同一条表**（``chat_approvals``，见 `db/models_chat.Approval`
的类注释），所以字段含义必须一致 —— ``preview`` 是人话摘要、
``status`` 取 ``pending/approved/rejected``，这些约定在
`db/models_chat.py` 里，这里不重新定义。

## 为什么闸口要在**工具执行之前**就把参数存下来

不存的话，"批准"时只能靠模型再说一遍它想干什么 —— 而那时对话历史
早已不在（任务是无人的后台执行）。存下 ``tool_name`` + ``tool_arguments``
之后，"人批准的"和"实际执行的"是同一份字节，
这也是审计能回答"他到底批了什么"的前提。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from fashionops.approval.policy import describe_preview
from fashionops.db import session_scope
from fashionops.db.base import utcnow
from fashionops.db.models_chat import (
    APPROVAL_STATUS_APPROVED,
    APPROVAL_STATUS_PENDING,
    APPROVAL_STATUS_REJECTED,
    Approval,
)


def new_approval_no() -> str:
    """审批单号。前缀和对话侧保持一致（``AP`` + 日期 + 6 位随机）。"""
    return f"AP{utcnow():%Y%m%d}{uuid.uuid4().hex[:6].upper()}"


def open_for_todo(
    *,
    task_no: str,
    todo_seq: int,
    tool_call_id: str,
    tool_name: str,
    arguments: dict | None,
    reason: str = "",
) -> dict:
    """给"某个任务的第几步"开一张审批单，返回它的对外形状。

    ``preview`` 由 :func:`describe_preview` 生成 ——
    **不是模型写的**。让人读模型自由发挥的一段话来决定批不批，
    等于闸口的判据又回到模型手里了。
    """
    preview = describe_preview(tool_name, arguments)
    with session_scope() as session:
        row = Approval(
            approval_no=new_approval_no(),
            task_no=task_no,
            todo_seq=todo_seq,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_arguments=arguments,
            preview=preview,
            reason=reason or f"{tool_name} 会产生对外不可撤销的副作用，需要人工确认",
        )
        session.add(row)
        session.flush()
        return serialize(row)


def pending_for_task(task_no: str) -> dict | None:
    """这个任务当前挂着的那张审批单（应该最多一张）。"""
    with session_scope() as session:
        row = session.scalar(
            select(Approval)
            .where(Approval.task_no == task_no, Approval.status == APPROVAL_STATUS_PENDING)
            .order_by(Approval.id.desc())
        )
        return None if row is None else serialize(row)


def decide(
    approval_no: str,
    *,
    approved: bool,
    decided_by: str,
    decided_by_id: int | None,
    reason: str = "",
    execution_result: dict | None = None,
) -> dict | None:
    """落审批结果。

    ``execution_result`` 在批准时是"实际执行成了什么"，驳回时是拒绝本身。
    无论哪种都要写 —— 只记"批没批"的话，"批准之后到底发生了什么"
    这个最关键的问题就没有答案。
    """
    with session_scope() as session:
        row = session.scalar(select(Approval).where(Approval.approval_no == approval_no))
        if row is None:
            return None
        row.status = APPROVAL_STATUS_APPROVED if approved else APPROVAL_STATUS_REJECTED
        row.decided_by = decided_by
        row.decided_by_id = decided_by_id
        row.decided_at = utcnow()
        row.decision_reason = reason
        if execution_result is not None:
            row.execution_result = execution_result
        session.flush()
        return serialize(row)


def get(approval_no: str) -> dict | None:
    with session_scope() as session:
        row = session.scalar(select(Approval).where(Approval.approval_no == approval_no))
        return None if row is None else serialize(row)


def serialize(row: Approval) -> dict:
    """字段名与 ``chat/store.serialize_approval`` 保持一致 ——
    前端一个组件渲染两边，形状不一样就要写两套解析。"""
    return {
        "id": row.id,
        "approval_no": row.approval_no,
        "conversation_id": row.conversation_id,
        "task_no": row.task_no,
        "todo_seq": row.todo_seq,
        "message_id": row.message_id,
        "tool_call_id": row.tool_call_id,
        "tool_name": row.tool_name,
        "tool_arguments": row.tool_arguments,
        "preview": row.preview,
        "reason": row.reason,
        "status": row.status,
        "decided_by": row.decided_by,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "decision_reason": row.decision_reason,
        "execution_result": row.execution_result,
    }


__all__ = [
    "decide",
    "get",
    "new_approval_no",
    "open_for_todo",
    "pending_for_task",
    "serialize",
]
