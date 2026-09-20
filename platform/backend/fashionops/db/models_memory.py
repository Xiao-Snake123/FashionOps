"""Agent 长期记忆 —— 跨会话记住用户的偏好和约定。

## 它和 `chat_conversations.memory_summary` 是两个层次

    chat_conversations.memory_summary   一场对话里"更早的内容"压成摘要（**短期**）
    agent_memories                      跨对话持久记住的偏好/约定（**长期**）

短期那个解决"聊到第 60 条时忘了开头说了什么"，但它**只在一场对话里生效** ——
新开一个对话仍然从零开始。用户上周说过"只看抖音渠道"，
这周新开对话时助手不该再问一遍。这就是这张表存在的理由。

（规划 §3 #10「Agent 记忆分层：短期（会话）+ 长期（历史偏好）」里缺的就是后半截。）

## 为什么用 ``topic`` 参与唯一键，而不是"存一段自由文本"

只存自由文本的话，用户改主意时只能**再追加一条**，于是库里会同时有：

    "只看抖音渠道"
    "现在淘宝也要看了"

两条都"是当时的事实"，但**任一条单独看都是错的**，而注入时两条都会进去 ——
模型面对两条自相矛盾的记忆只能瞎猜，而它猜错的代价是"按已作废的要求干活"。

用 ``topic``（"渠道偏好"）做 ``(user_id, topic)`` 唯一键之后，
**再记一次同一个 topic 就是更新**，库里永远只有一条当前有效的说法。
这也顺带把增长**限制在 topic 的数量上**，而不是调用次数上 ——
一个用户想不出 50 条互不重叠的长期偏好。

## 为什么记 ``kind``

因为"会过期"和"不会过期"的记忆注入方式不同。
``fact``（"9 月退货率 3.2%"）是**有时效**的，模型必须知道它的时间背景；
``preference``（"报表按周出"）不会过期。混在一起会让模型把
一个半月前的数字当成今天的。

## 为什么冗余 ``username``

和 ``tasks.created_by`` / ``chat_conversations.username`` 同一个理由：
用户被删/改名后，"这条偏好当时是谁说的"是个事实，不该跟着变。

## 为什么 ``ondelete="CASCADE"``

记忆是**个人数据**。用户被删时它必须跟着消失 ——
留着的后果是"那个人已经离职了，助手还在按他的习惯办事"，
而且没人能解释为什么。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from fashionops.db.base import Base, utcnow

#: 用户偏好 / 约定。**不会过期**，最常记的就是它。
MEMORY_KIND_PREFERENCE = "preference"
#: 业务事实（数字、结论）。**有时效** —— 注入时会带上记录时间。
MEMORY_KIND_FACT = "fact"
#: 其它备忘（"这个客户很在意时效"这类）。不承诺可执行，只作背景。
MEMORY_KIND_NOTE = "note"

MEMORY_KINDS: frozenset[str] = frozenset(
    {MEMORY_KIND_PREFERENCE, MEMORY_KIND_FACT, MEMORY_KIND_NOTE}
)

#: ``topic`` 的长度上限。
#: 它的作用是让模型**能稳定地复述同一个键**（"渠道偏好"每次都得写一样），
#: 太长就必然每次写法都不一样，唯一键也就失去意义了。
MAX_TOPIC_CHARS = 64

#: ``content`` 的长度上限。记忆是一句话，不是一段话 ——
#: 太长的东西应该留在对话摘要里，而不是每轮都注入。
MAX_CONTENT_CHARS = 500


class AgentMemory(Base):
    """一条长期记忆。"""

    __tablename__ = "agent_memories"
    __table_args__ = (
        # 唯一键：同一个人、同一个 topic 只有一条 —— 再记就是更新。
        # 见模块开头"为什么用 topic 参与唯一键"。
        UniqueConstraint("user_id", "topic", name="uq_agent_memory_user_topic"),
        # 注入时的查询就靠它：按人取、按更新时间倒序
        Index("ix_agent_memory_user_updated", "user_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: 用户名快照，理由见模块顶部
    username: Mapped[str] = mapped_column(String(64), default="")

    #: 这条记忆的标题，也是它的键（"渠道偏好" / "报表口径"）。
    topic: Mapped[str] = mapped_column(String(MAX_TOPIC_CHARS))
    #: 一句话说清记住什么。
    content: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(32), default=MEMORY_KIND_PREFERENCE)

    #: 在哪场对话里记下的。**可空**：将来可能有别的入口（CLI 预置、管理界面）。
    #: 留着它才能在用户问"你凭什么记得这个"时指回原话。
    source_conversation_no: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


__all__ = [
    "MAX_CONTENT_CHARS",
    "MAX_TOPIC_CHARS",
    "MEMORY_KINDS",
    "MEMORY_KIND_FACT",
    "MEMORY_KIND_NOTE",
    "MEMORY_KIND_PREFERENCE",
    "AgentMemory",
]
