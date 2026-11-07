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
    APPROVAL_STATUS_PENDING,
    MSG_ROLE_ASSISTANT,
    MSG_ROLE_TOOL,
    MSG_ROLE_USER,
    Approval,
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
        #: 摘要覆盖到第几条消息（0 = 还没压缩过）。
        #:
        #: 刻意放进列表里：之前超出窗口的历史是**静默丢弃**的，
        #: 用户觉得"它忘了"，而排查时没有任何线索。带上这个数字，
        #: "它到底压到哪儿了"就变成一个能看见的事实。
        "memory_summary_upto_seq": int(row.memory_summary_upto_seq or 0),
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
    return session.scalar(select(Conversation).where(Conversation.conversation_no == conversation_no))


def list_conversations(
    session: Session,
    *,
    user_id: int | None,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
) -> list[Conversation]:
    stmt = select(Conversation).order_by(Conversation.updated_at.desc()).offset(offset).limit(limit)
    if user_id is not None:
        stmt = stmt.where(Conversation.user_id == user_id)
    if status is not None:
        stmt = stmt.where(Conversation.status == status)
    return list(session.scalars(stmt))


def count_conversations(session: Session, *, user_id: int | None, status: str | None = None) -> int:
    """符合条件的会话**总数**（不受 limit 影响）。

    为什么要单独一个函数：列表查询带了 ``limit``，拿它的长度当总数的话
    ``total`` 永远等于本页条数（最多 50），前端既算不出页数，
    也发现不了"第 51 个会话其实存在"。
    """
    stmt = select(func.count()).select_from(Conversation)
    if user_id is not None:
        stmt = stmt.where(Conversation.user_id == user_id)
    if status is not None:
        stmt = stmt.where(Conversation.status == status)
    return int(session.scalar(stmt) or 0)


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

    ## 怎么解决并发撞唯一约束

    旧实现是 ``MAX(seq)+1``：同一个会话被并发写（比如前端重发、SSE 重连）
    时，两个事务会读到同一个 ``MAX``，算出同一个下一个序号，
    其中一个插进去就被 ``uq_chat_msg_seq`` 顶回来。

    现在锁住这条会话行（``SELECT ... FOR UPDATE``），在事务内读
    ``last_seq`` 并 ``+1`` 写回 —— 同会话的并发请求被这条行锁**串行化**，
    不会再撞。``last_seq`` 列在 `models_chat.py` 里，迁移时按各会话已用的
    最大 seq 回填过了。
    """
    conversation = session.get(Conversation, conversation_id, with_for_update=True)
    if conversation is None:
        # 极端边缘：正在写消息时会话被级联删掉。退化成旧的 MAX 做法——
        # 反正这种边缘撞不撞唯一约束都不重要了。
        current = session.scalar(
            select(func.max(ChatMessage.seq)).where(ChatMessage.conversation_id == conversation_id)
        )
        return int(current or 0) + 1
    new_seq = int(conversation.last_seq or 0) + 1
    conversation.last_seq = new_seq
    session.flush()
    return new_seq


def set_conversation_status(conversation_id: int, new_status: str) -> bool:
    """翻转会话状态（归档 / 取消归档）。返回会话是否存在。"""
    with session_scope() as session:
        row = session.get(Conversation, conversation_id)
        if row is None:
            return False
        row.status = new_status
        return True


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
    conversation.total_tokens = (
        int(conversation.total_tokens or 0) + int(row.prompt_tokens or 0) + int(row.completion_tokens or 0)
    )
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


def history(
    session: Session,
    conversation_id: int,
    *,
    limit: int | None = None,
    before_seq: int | None = None,
) -> list[ChatMessage]:
    """会话历史，按 ``seq`` 升序。

    ## 为什么 ``limit`` 是"取最后 N 条"而不是"取前 N 条"

    调用方（`chat/agent.py` 拼模型上下文）要的是**最近的** N 条 ——
    模型关心的是"刚才说了什么"，不是"这场对话的开场白"。

    ## ``before_seq``：向上滚动加载

    加了这个上界之后，"取最后 N 条"变成"取 seq < before_seq 的最后 N 条"，
    于是可以一页页往回翻。没有它的话，接口只能永远返回最新的那 N 条，
    用户就再也看不到更早的消息了 —— 而消息内容（工具返回的大段 JSON）
    恰恰是最不适合一次性全量拉的东西。

    ## 为什么要用倒序 + LIMIT，而不是全查出来再切片

    这里曾经是"把整场对话读进内存，然后 ``rows[-limit:]``"。功能上没错，
    但**一个聊了 200 句的会话每次请求都要把几百行消息（含工具返回的大段 JSON）
    全量拉出来再扔掉 90%**。对话越长，这个浪费越大 ——
    而这正是"限制 40 条"本来想省下来的成本，等于白省。

    倒序取 N 条再翻转，既拿到同一批数据，又让数据库用上
    ``ix_chat_msg_conv_seq`` 直接定位末尾。
    """
    stmt = (
        select(ChatMessage)
        .where(ChatMessage.conversation_id == conversation_id)
        .order_by(ChatMessage.seq.desc())
        .limit(limit)  # None = 不限，SQLAlchemy 会省略 LIMIT 子句
    )
    if before_seq is not None:
        stmt = stmt.where(ChatMessage.seq < before_seq)
    rows = list(session.scalars(stmt))
    rows.reverse()  # 上面倒序只为"取末尾 N 条"，对外仍按 seq 升序
    return rows


# ===========================================================================
# 消息 → LLM 上下文
# ===========================================================================
#: 末尾这么多条消息里的工具结果保持完整。见 `to_llm_messages` 的说明。
_TOOL_RESULT_FULL_TAIL = 12

#: 被打断的回答在重放时缀上的说明。
#: 明说"这是系统提示"而不是让它光秃秃地接在正文后面 ——
#: 模型很容易把这种补充当成用户的新输入。
_INTERRUPTED_NOTE = "（系统提示：这条回答当时生成到一半就中断了，不是完整回答。）"


def to_llm_messages(
    rows: list[ChatMessage],
    *,
    system_prompt: str,
    old_tool_result_chars: int | None = None,
) -> list[Message]:
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

    ## 另一个必须处理的坑：成不了对的 tool_calls / tool 消息

    OpenAI 兼容接口对 ``tool_calls`` 和 ``tool`` 消息的**配对与相邻**
    要求很硬，两边都会 400：

    1. **没有父请求的 tool 消息**（裁剪时一刀切在 ``assistant(tool_calls)``
       和它的结果之间）：

           messages with role 'tool' must be a response to a preceding
           message with 'tool_calls'

    2. **没有结果的 ``assistant(tool_calls)``**：

           An assistant message with 'tool_calls' must be followed by
           tool messages responding to each tool_call_id

    第 2 种比第 1 种更常见，而且**不是裁剪造成的**，是正常的用户行为：
    写操作挂起等确认（``finish_reason=approval_required``）时，
    用户没点那张卡片，而是直接接着说了句别的话 —— 历史就变成::

        assistant(tool_calls=[mail__send])
        user("算了，先看看库存")

    这一条在**下一次**请求时才会炸，表现是"聊到一半突然报错"，
    而且换成 mock 模型测不出来（mock 不校验配对）。

    所以裁完之后要跑一遍 :func:`_sanitize_tool_pairs`，两个方向都修：
    丢失父请求的 tool 消息丢掉；配不上结果的 tool_calls 从 assistant
    消息上摘掉（正文保留 —— 那句"我准备发一封邮件…"是给用户看过的事实）。

    ## 另外两处"喂给模型的"和"库里存的"**故意不一样**

    都不是为了好看，是为了模型能做出正确判断。

    ### 1. 被打断的回答要显式标注

    ``interrupted=True`` 的消息是"流式生成到一半断了"（用户关了页面 /
    模型超时）。库里留原文是**事实记录**，但原样喂回去模型会以为自己当时
    说完了 —— 它可能据此往下推理，或者认为"这个问题已经答过"而不再回答。

    所以重放时缀一句说明。前端展示走的是 `serialize_message`，
    不受影响：用户看到的仍是原文，只有模型看得到这个标注。

    ### 2. 旧工具结果要截断

    工具返回的 JSON 动辄几千字（一屏 SKU、一屏订单），而它会**每轮重发一次**，
    直到掉出窗口为止。多数内容在生成它的那一轮之后就不再被需要。

    所以离得远的那部分只留开头（``chat_old_tool_result_chars``），
    最近几条保持完整 —— 模型真正在读的是最近的结果。
    截断处写明"已截断"，免得它把半截 JSON 当成完整数据。
    """
    messages: list[Message] = [Message.system(system_prompt)]

    #: 从这条开始（含）工具结果保持完整，之前的按配置截断。
    full_from = max(0, len(rows) - _TOOL_RESULT_FULL_TAIL)
    truncate_at = _old_tool_result_chars(old_tool_result_chars)

    for index, row in enumerate(rows):
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
            content = row.content
            if row.interrupted:
                content = f"{content}\n{_INTERRUPTED_NOTE}"
            messages.append(Message.assistant(content, tool_calls=calls))
        elif row.role == MSG_ROLE_TOOL:
            content = row.content
            if truncate_at > 0 and index < full_from and len(content) > truncate_at:
                content = f"{content[:truncate_at]}\n…（结果过长已截断，原始长度 {len(content)} 字符）"
            messages.append(
                Message.tool(
                    tool_call_id=row.tool_call_id or "",
                    name=to_llm_name(row.tool_name) if row.tool_name else None,
                    content=content,
                )
            )
        # system 消息不入库（每次由代码重新拼），所以这里不处理

    return _sanitize_tool_pairs(messages)


def _old_tool_result_chars(explicit: int | None) -> int:
    """旧工具结果的截断长度。``explicit`` 优先（测试用），否则读配置。

    配置读不到 / 是负数时退回 0（不截断）—— 宁可不省 token，
    也不要在配置异常时把工具结果切坏。
    """
    if explicit is not None:
        return max(0, explicit)
    from fashionops.config import get_settings

    return max(0, int(getattr(get_settings(), "chat_old_tool_result_chars", 1200) or 0))


def _sanitize_tool_pairs(messages: list[Message]) -> list[Message]:
    """把 ``tool_calls`` 和 ``tool`` 结果修成"成对且相邻"。

    ## 判据是"相邻"，不是"存在"

    ``tool_call_id`` 只要求出现在**后面某处**是不够的。接口要求的是
    "紧跟在它的 assistant 请求之后、中间不能插别的消息"。所以这里先扫一遍，
    只承认**紧跟其后那一段连续的 tool 消息**里出现的 id，
    之后（被别的消息隔开）才出现的 tool 消息不算数。

    这个严格版本顺带覆盖了"assistant 声明了两个调用、只回了一个"的情况：
    没回的那个从 tool_calls 里摘掉，**回过的那个留着** ——
    反正它和结果仍然相邻，丢掉它等于白丢一次已经花掉的 token。

    ## 为什么摘掉 tool_calls 而不是丢掉整条 assistant 消息

    因为那条消息的**正文是用户已经看过的内容**（"我准备发一封邮件给运营组，
    请确认"）。整条丢掉会让模型下一轮不知道自己刚才说过什么，
    于是重新说一遍同样的话 —— 用户看到"它怎么又问一遍"。
    摘掉 tool_calls、留下正文，模型就知道"我提过这件事，但没执行"。
    """
    # 第一步：找出**真正配成对**的 id。
    # 注意这里是"逐条累计已答的 id"，不是"整条 assistant 全部答齐才算" ——
    # 见上面"只回了一个"那段。
    paired: set[str] = set()
    for index, message in enumerate(messages):
        if message.role != "assistant" or not message.tool_calls:
            continue
        wanted = {call.id for call in message.tool_calls}
        for follower in messages[index + 1 :]:
            if follower.role != "tool":
                break  # 中间插了别的东西，后面再出现的 tool 也不属于这一批
            if follower.tool_call_id in wanted:
                paired.add(follower.tool_call_id or "")

    # 第二步：按上面的结论重建
    out: list[Message] = []
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            kept = [call for call in message.tool_calls if call.id in paired]
            if kept:
                out.append(message.model_copy(update={"tool_calls": kept}))
            else:
                # 没有结果 —— 摘掉 tool_calls，保住正文。见函数开头的说明。
                out.append(message.model_copy(update={"tool_calls": None}))
            continue
        if message.role == "tool":
            if message.tool_call_id in paired:
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
        row = Approval(
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


def serialize_approval(row: Approval) -> dict:
    return {
        "id": row.id,
        "approval_no": row.approval_no,
        "conversation_id": row.conversation_id,
        #: 任务侧的闸口会把归属写在这里，对话侧为 None。
        #: 带上它，前端一个组件就能同时渲染两边。
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
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def pending_for_conversation(session: Session, conversation_id: int) -> list[Approval]:
    """这场对话里**还作数**的待确认单。

    ## 为什么这里要先作废过期的、再卡一次时间戳

    两个动作解决的是不同问题，缺一不可：

    * ``expire_stale()`` 把过期的落库成 ``expired`` —— 这是给**看板**和事后复盘看的。
      它自己拦不住任何人（一个拿着审批单号的人可以直接调决定接口），
      所以它不是安全边界，真正的边界在执行口（见 `approval/lifecycle.py`）。
    * 查询里的时间戳条件解决"**同一个请求里看不到上面那次 UPDATE**"：
      MySQL 默认 REPEATABLE READ，而调用方（HTTP 路由）的 session
      在鉴权阶段就已经读过数据、快照固定了。只按 ``status`` 过滤的话，
      过期单在**这次**响应里照样会被返回，用户就还能看到幽灵卡片。

    代价是过期判断做了两遍（一次在 SQL 的 UPDATE 里、一次在 SELECT 里）。
    这点重复换来的是"不需要依赖调用方的事务语义" —— 值得。
    """
    from fashionops.approval import lifecycle

    lifecycle.expire_stale()
    limit = lifecycle.cutoff()

    stmt = select(Approval).where(
        Approval.conversation_id == conversation_id,
        Approval.status == APPROVAL_STATUS_PENDING,
    )
    if limit is not None:
        stmt = stmt.where(Approval.created_at >= limit)
    return list(session.scalars(stmt.order_by(Approval.id)))


def get_approval(session: Session, approval_no: str) -> Approval | None:
    return session.scalar(select(Approval).where(Approval.approval_no == approval_no))


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
