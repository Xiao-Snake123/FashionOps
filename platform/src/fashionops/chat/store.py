"""对话域的持久化 —— 会话 / 消息 / 审批的读写。

## 为什么每个函数都自己开事务，而不是复用请求的 session

对话是**流式**的：一个回合可能跑几十秒，期间要一直往库里写消息
（用户能在界面上看到自己说的话，即使 Agent 还没答完）。

而 HTTP 请求那套事务语义不适合它：

1. `get_db` 的提交发生在**响应发出之后**（见 `docs/11` §6 记的那条），
   也就是整条流跑完之后 —— 中途崩了就一条消息都不剩，
   用户刷新页面会发现整场对话消失了。
2. 流式生成器随时可能被客户端断开（关页面），那时请求级的 session
   可能已经关了。**往一个已关闭的 session 上写会静默失败或抛异常。**
3. 更根本的：一次对话回合没有"要么全成要么全败"的语义。
   用户问了三句、Agent 答了两句，这两句就是发生过的事实，
   不该因为第三句失败而全部回滚。

所以这里每个写操作都是**自己一个短事务、立刻提交**。
代价是丢掉了跨写操作的原子性 —— 对聊天记录来说，这正是想要的。
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fashionops.db import session_scope
from fashionops.db.base import utcnow
from fashionops.db.models_chat import (
    MSG_ROLE_ASSISTANT,
    MSG_ROLE_TOOL,
    MSG_ROLE_USER,
    ChatApproval,
    ChatMessage,
    Conversation,
)
from fashionops.gateway.registry import to_llm_name
from fashionops.llm import Message, ToolCall

#: 喂给模型的对话轮数上限。
#:
#: 不加限制的话，一场聊了 200 句的对话每次请求都要把全部历史发过去 ——
#: token 成本随轮数线性涨，而且迟早撞上上下文窗口。
#:
#: 20 轮 ≈ 40 条消息，对"查个库存、出份报表"这种任务量级足够了。
#: 真正需要长记忆时应该做摘要压缩，而不是把上限调大。
MAX_CONTEXT_MESSAGES = 40


def new_conversation_no() -> str:
    return f"CH{utcnow():%Y%m%d}{uuid.uuid4().hex[:6].upper()}"


def new_approval_no() -> str:
    return f"AP{utcnow():%Y%m%d}{uuid.uuid4().hex[:6].upper()}"


# ===========================================================================
# 会话
# ===========================================================================
def serialize_conversation(row: Conversation) -> dict:
    """会话的对外形状。和消息/审批一样，**只在这里定义一次**。"""
    return {
        "conversation_no": row.conversation_no,
        "title": row.title,
        "status": row.status,
        "username": row.username,
        "model": row.model,
        "message_count": row.message_count,
        "llm_calls": row.llm_calls,
        "tool_calls": row.tool_calls,
        "total_tokens": row.total_tokens,
        "total_cost": float(row.total_cost or 0),
        "last_message_at": row.last_message_at.isoformat() if row.last_message_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def create_conversation(
    *,
    username: str,
    user_id: int | None,
    title: str = "新对话",
    model: str = "",
) -> dict:
    """建一场会话，返回它的可序列化字典（**不是 id**）。

    ## 为什么返回字典而不是让调用方再查一次

    因为查不到。这个函数用的是自己的短事务，而调用方（HTTP 路由）手里那个
    session 在鉴权阶段已经跑过查询了 —— **MySQL 默认是 REPEATABLE READ**，
    事务的快照在第一次读的时候就固定了，之后别的事务提交的新行
    **在这个 session 里永远看不见**。

    表现是一个 500：``'NoneType' object has no attribute 'conversation_no'``。
    而且它不会在 SQLite 测试里出现（SQLite 的隔离语义不同），
    只在真实 MySQL 上炸 —— 又一个"测试绿、真库红"。

    直接把自己刚写的东西返回去，既避开这个问题，也少一次查询。
    """
    with session_scope() as session:
        row = Conversation(
            conversation_no=new_conversation_no(),
            title=title[:200] or "新对话",
            username=username,
            user_id=user_id,
            model=model,
            last_message_at=utcnow(),
        )
        # created_at / updated_at 是数据库列的 default，flush 之后才回填到对象上。
        # 不 flush 就序列化会拿到 None —— 而且依然是**只在真实库里错**
        session.add(row)
        session.flush()
        return serialize_conversation(row)


def get_conversation(session: Session, conversation_no: str) -> Conversation | None:
    return session.scalar(
        select(Conversation).where(Conversation.conversation_no == conversation_no)
    )


def list_conversations(
    session: Session, *, user_id: int | None, limit: int = 50
) -> list[Conversation]:
    stmt = select(Conversation).order_by(Conversation.updated_at.desc()).limit(limit)
    if user_id is not None:
        stmt = stmt.where(Conversation.user_id == user_id)
    return list(session.scalars(stmt))


def rename(conversation_id: int, title: str) -> None:
    with session_scope() as session:
        row = session.get(Conversation, conversation_id)
        if row is not None:
            row.title = title[:200]


# ===========================================================================
# 消息
# ===========================================================================
def _next_seq(session: Session, conversation_id: int) -> int:
    """会话内递增序号。

    用 ``MAX(seq)+1`` 而不是自增列：序号必须是**会话内**连续的，
    自增 id 是全局的，拿它做断线续传的游标会让前端收到别的会话的空洞。

    并发写同一个会话理论上会撞（唯一约束会挡住）。实际场景是一个用户
    在一个对话里顺序交互，撞不上 —— 真撞上了报错也比静默错序好。
    """
    current = session.scalar(
        select(func.max(ChatMessage.seq)).where(ChatMessage.conversation_id == conversation_id)
    )
    return int(current or 0) + 1


def append_message(conversation_id: int, *, role: str, content: str = "", **fields) -> dict:
    """追加一条消息，返回它的可序列化字典。

    返回 dict 而不是 ORM 对象：ORM 对象出了 session 就是游离态，
    访问未加载的属性会抛 ``DetachedInstanceError`` ——
    而这个值要被 SSE 序列化后发出去，必须能安全地在 session 外读。
    """
    with session_scope() as session:
        row = ChatMessage(
            conversation_id=conversation_id,
            seq=_next_seq(session, conversation_id),
            role=role,
            content=content,
            **fields,
        )
        session.add(row)
        session.flush()
        payload = serialize_message(row)
        _bump(session, conversation_id, row)
        return payload


def serialize_message(row: ChatMessage) -> dict:
    """消息的对外形状。**这里是唯一的定义处** ——

    前端渲染和 SSE 推送必须用同一份，否则会出现"刷新后有、实时推送没有"
    这种很难查的字段缺失。
    """
    return {
        "id": row.id,
        "seq": row.seq,
        "role": row.role,
        "content": row.content,
        "tool_call_id": row.tool_call_id,
        "tool_name": row.tool_name,
        "tool_arguments": row.tool_arguments,
        "tool_result": row.tool_result,
        "tool_ok": row.tool_ok,
        "tool_duration_ms": row.tool_duration_ms,
        "task_no": row.task_no,
        "model": row.model,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "cost": float(row.cost or 0),
        "latency_ms": row.latency_ms,
        "finish_reason": row.finish_reason,
        "interrupted": row.interrupted,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _bump(session: Session, conversation_id: int, row: ChatMessage) -> None:
    """更新会话上的汇总值。

    冗余一份是为了会话列表页不用去 JOIN 聚合几十万行消息。
    每条消息写一次是"读多写少"的正确取舍 —— 列表页每次都要读，
    而消息只在对话进行时写。
    """
    conversation = session.get(Conversation, conversation_id)
    if conversation is None:
        return
    conversation.message_count = int(conversation.message_count or 0) + 1
    conversation.total_tokens = int(conversation.total_tokens or 0) + int(
        row.prompt_tokens or 0
    ) + int(row.completion_tokens or 0)
    conversation.total_cost = (conversation.total_cost or 0) + (row.cost or 0)
    if row.role == MSG_ROLE_TOOL:
        conversation.tool_calls = int(conversation.tool_calls or 0) + 1
    conversation.last_message_at = utcnow()


def bump_llm(conversation_id: int, *, calls: int = 1, model: str | None = None) -> None:
    with session_scope() as session:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            return
        conversation.llm_calls = int(conversation.llm_calls or 0) + calls
        if model:
            conversation.model = model


def history(session: Session, conversation_id: int, *, limit: int | None = None) -> list[ChatMessage]:
    stmt = (
        select(ChatMessage)
        .where(ChatMessage.conversation_id == conversation_id)
        .order_by(ChatMessage.seq)
    )
    rows = list(session.scalars(stmt))
    if limit is not None and len(rows) > limit:
        rows = rows[-limit:]
    return rows


# ===========================================================================
# 消息 → LLM 上下文
# ===========================================================================
def to_llm_messages(rows: list[ChatMessage], *, system_prompt: str) -> list[Message]:
    """把库里的消息重放成模型能看懂的上下文。

    ## 工具名必须从内部名转回**对外名**

    库里存的是内部名（``erp.query_inventory``，点号分层，人看着顺眼）；
    但模型看到的是对外名（``erp__query_inventory``，因为 OpenAI 的 function name
    不允许点号）。重放时**一定要转回去**。

    不转的后果非常隐蔽：历史里会出现一个"调用了 ``erp.query_inventory``"的
    assistant 消息，而本轮声明的函数叫 ``erp__query_inventory`` —— 模型看到的是
    **一对不上号的例子**。它会从中学到"工具名长成 ``mail.policy`` 这样、
    而且直接写在正文里就行"，于是接下来把工具调用**当成文本吐出来**：

        回合2 流式文本： mail.policy\n{}
        回合4 流式文本： task.submit\n{"request": "..."}

    表现是"模型不会用工具了"，而实际是**我们喂给它的历史是错的**。
    而且它只在**第二轮之后**才发作（第一轮没有历史可学），所以特别像
    "模型不稳定"。

    ## 另一个必须处理的坑：孤立的 tool 消息

    裁剪历史时如果一刀切在 ``assistant(tool_calls)`` 和它对应的
    ``tool`` 结果之间，就会留下一条**没有父请求的 tool 消息**。
    OpenAI 兼容接口会直接 400：

        messages with role 'tool' must be a response to a preceding
        message with 'tool_calls'

    而且是**上线后才炸**：只有对话足够长、恰好触发裁剪才会出现。
    所以裁完之后要把前面的孤立 tool 消息丢掉。
    """
    messages: list[Message] = [Message.system(system_prompt)]
    for row in rows:
        if row.role == MSG_ROLE_USER:
            messages.append(Message.user(row.content))
        elif row.role == MSG_ROLE_ASSISTANT:
            calls = None
            if row.tool_name and row.tool_call_id:
                calls = [
                    ToolCall(
                        id=row.tool_call_id,
                        name=to_llm_name(row.tool_name),
                        arguments=row.tool_arguments or {},
                    )
                ]
            messages.append(Message.assistant(row.content, tool_calls=calls))
        elif row.role == MSG_ROLE_TOOL:
            messages.append(
                Message.tool(
                    tool_call_id=row.tool_call_id or "",
                    name=to_llm_name(row.tool_name) if row.tool_name else None,
                    content=row.content,
                )
            )
        # system 消息不入库（每次由代码重新拼），所以这里不处理

    return _drop_orphan_tools(messages)


def _drop_orphan_tools(messages: list[Message]) -> list[Message]:
    """丢掉前面没有 ``assistant(tool_calls)`` 的 tool 消息。"""
    known_ids: set[str] = set()
    out: list[Message] = []
    for message in messages:
        if message.role == "assistant":
            for call in message.tool_calls or []:
                known_ids.add(call.id)
            out.append(message)
            continue
        if message.role == "tool":
            if message.tool_call_id in known_ids:
                out.append(message)
            # 孤立的那条直接丢 —— 留着会让整个请求 400
            continue
        out.append(message)
    return out


# ===========================================================================
# 审批
# ===========================================================================
def create_approval(
    *,
    conversation_id: int,
    message_id: int | None,
    tool_call_id: str,
    tool_name: str,
    arguments: dict | None,
    preview: str = "",
    reason: str = "",
) -> dict:
    with session_scope() as session:
        row = ChatApproval(
            approval_no=new_approval_no(),
            conversation_id=conversation_id,
            message_id=message_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_arguments=arguments,
            preview=preview,
            reason=reason,
        )
        session.add(row)
        session.flush()
        return serialize_approval(row)


def serialize_approval(row: ChatApproval) -> dict:
    return {
        "id": row.id,
        "approval_no": row.approval_no,
        "conversation_id": row.conversation_id,
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
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def pending_for_conversation(session: Session, conversation_id: int) -> list[ChatApproval]:
    return list(
        session.scalars(
            select(ChatApproval)
            .where(
                ChatApproval.conversation_id == conversation_id,
                ChatApproval.status == "pending",
            )
            .order_by(ChatApproval.id)
        )
    )


def get_approval(session: Session, approval_no: str) -> ChatApproval | None:
    return session.scalar(select(ChatApproval).where(ChatApproval.approval_no == approval_no))


__all__ = [
    "MAX_CONTEXT_MESSAGES",
    "append_message",
    "bump_llm",
    "create_approval",
    "create_conversation",
    "get_approval",
    "get_conversation",
    "history",
    "list_conversations",
    "new_approval_no",
    "new_conversation_no",
    "pending_for_conversation",
    "rename",
    "serialize_approval",
    "serialize_conversation",
    "serialize_message",
    "to_llm_messages",
]
