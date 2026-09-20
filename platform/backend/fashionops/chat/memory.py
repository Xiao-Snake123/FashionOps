"""对话记忆的压缩层 —— 让"聊得久了"不靠静默丢消息来解决。

## 问题

上下文窗口是有上限的（`chat_context_messages`，默认 40 条）。
超出窗口的历史**直接丢掉**，而且丢得无声无息：用户聊到第 60 条时会发现
"它忘了我开头说的类目"，而系统里没有任何地方记录过这件事发生过 ——
排查时连个异常都看不到。

`chat/store.py` 里那句注释其实早就写明了该怎么做：

    真正需要长记忆时应该做摘要压缩，而不是把上限调大

这个模块就是那块压缩。

## 做法：滚动摘要

    [ seq 1 .. 27 ]   ← 已折叠成 memory_summary（存在会话行上）
    [ seq 28 .. 67 ]  ← 当前上下文窗口，原文喂给模型

每轮开始时算一次"窗口从哪里开始"。如果有消息掉出了窗口、又还没进摘要，
就把「旧摘要 + 新掉出去的那些」重新压成一段新摘要。

于是模型永远同时看得到**背景**（很早之前发生过什么）和**原文**（刚说了什么），
而不是只剩后者。

## 四个刻意的取舍

1. **摘要存在会话行上，不单独建表。** 一场对话只有一段摘要
   （它本身就是"所有旧消息的折叠"，和会话是一对一）。独立表只会让每次读取
   多一次 JOIN，而对一的关系最容易被写歪成"多条"。

2. **刷新有阈值，不是每轮都刷。** 一旦对话超过窗口，之后每轮都会有新消息
   掉出去；每轮都刷 = 每轮多一次模型调用。所以攒够
   `chat_summary_refresh_every` 条再刷。中间那几条确实还是丢了 ——
   但那和改动之前的行为一样，**严格好于不改**。

3. **`upto` 跟着"实际压缩到的那条"走。** 压缩器的输入也有预算，
   一次塞不下全部新掉出的消息。这时**不能**把 `upto` 直接设成窗口起点 ——
   那等于把没压缩过的消息标记成"已覆盖"，它们就永久消失且不留痕迹。
   所以按预算从旧到新取，`upto` = 实际取到最后一条的 seq。

4. **压缩失败绝不打断对话。** 模型超时 / 返回空 / 返回超长，一律退回
   "没有摘要"（也就是原来的行为）。摘要的价值远小于"这轮对话能不能跑起来"。
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple

from sqlalchemy import func, select

from fashionops.chat import store
from fashionops.db import session_scope
from fashionops.db.models_chat import (
    MSG_ROLE_TOOL,
    MSG_ROLE_USER,
    ChatMessage,
    Conversation,
)
from fashionops.llm import Message

logger = logging.getLogger("fashionops.chat.memory")

#: 摘要块在 system prompt 里的标题。和"铁律"那些段落并列。
SUMMARY_HEADING = "## 更早的对话（已压缩成摘要）"

#: 压缩器的输入预算（字符）。它自己的 token 也是成本，
#: 而且这里塞的是"已经不需要逐字看"的旧内容 —— 给太大反而不划算。
_SUMMARY_INPUT_CHARS = 8000

#: 摘要的长度上限（字符）。超了直接截断：一段 5000 字的"摘要"已经不再是摘要了，
#: 它会挤占本该留给原文的窗口。
_MAX_SUMMARY_CHARS = 800

_SUMMARY_PROMPT = """你在压缩一段运营助手和用户之间的历史对话。把它压成一段不超过 400 字的摘要。

按重要性保留：

1. **用户的目标、约束、明确表达过的偏好**（"只看抖音渠道"、"报表按周出"）
2. **已经查出来的关键数字和结论** —— 带数字，不要写成"查过库存"这种没信息的句子
3. **已经做过的事**：建过哪个任务、发出过什么、哪一步被驳回过
4. **尚未解决、用户还在等的事**

可以丢掉的：寒暄、重复、工具调用的技术细节（除非结论依赖它）。

只输出摘要正文。不要标题、不要"以下是摘要"这类开场白、不要 markdown 代码块。"""


class _Row(NamedTuple):
    """压缩只需要这几个字段。

    刻意不直接传 ORM 对象：压缩发生在会话事务之外，
    而脱离 session 的 ORM 对象访问未加载属性会抛 ``DetachedInstanceError`` ——
    在这个位置出错的表现是"聊到一定长度就崩"，很难定位。
    """

    seq: int
    role: str
    content: str
    tool_name: str | None
    tool_ok: bool | None


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def context_limit() -> int:
    """上下文窗口大小（条）。配置为 0 或负数时退回模块默认常量。"""
    from fashionops.config import get_settings

    value = int(getattr(get_settings(), "chat_context_messages", 0) or 0)
    return value if value > 0 else store.MAX_CONTEXT_MESSAGES


def refresh_every() -> int:
    """攒够多少条新掉出窗口的消息才重做一次摘要。至少 1。"""
    from fashionops.config import get_settings

    return max(1, int(getattr(get_settings(), "chat_summary_refresh_every", 10) or 1))


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
def maybe_refresh(conversation_id: int, *, model_client: Any) -> str:
    """必要时把新掉出窗口的消息并进摘要，返回**当前可用**的摘要。

    返回空串表示"没有摘要" —— 调用方按没有摘要拼上下文即可。

    **任何失败都不往抛**：摘要压缩是锦上添花，
    绝不能因为它把一轮对话搞挂。

    ``model_client`` 由调用方传入而不是这里自己取：这样测试里
    patch 掉 `chat/agent._drive` 用的那个客户端，压缩也会一起被替换掉，
    不会偷偷去调真实 API。
    """
    if model_client is None or context_limit() <= 0:
        return ""
    try:
        return _refresh(conversation_id, model_client=model_client)
    except Exception:  # noqa: BLE001
        logger.exception("会话 %s 的摘要压缩失败，本轮按「没有摘要」继续", conversation_id)
        return ""


def build_block(summary: str) -> str:
    """把摘要拼成 system prompt 里的一段。空摘要返回空串。"""
    text = (summary or "").strip()
    if not text:
        return ""
    return (
        f"{SUMMARY_HEADING}\n\n"
        "以下是你和这位用户**更早**的对话内容（已压缩）。它是背景而不是指令：\n"
        "里面的数字和结论仍然有效，可以直接引用；但如果用户在后面的对话里改了要求，"
        "**一律以后面说的为准**。\n\n"
        f"{text}"
    )


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _refresh(conversation_id: int, *, model_client: Any) -> str:
    limit = context_limit()
    window_start = _window_start_seq(conversation_id, limit)
    if window_start is None:
        # 整个会话还在窗口内，不需要摘要
        return ""

    summary, upto = _load_summary(conversation_id)
    if upto >= window_start - 1:
        # 已经覆盖了所有掉出窗口的消息
        return summary

    dropped = _load_range(conversation_id, after=upto, before=window_start)
    if not dropped:
        return summary

    # 第一次压缩（还没有摘要）不管多少条都做；之后攒够阈值才做，省一次模型调用
    if summary and len(dropped) < refresh_every():
        return summary

    included, new_upto = _fit_budget(dropped)
    if not included or new_upto is None:
        return summary

    text = _summarize(model_client, previous=summary, block=_render_for_summary(included))
    if not text:
        return summary

    _save_summary(conversation_id, text, new_upto)
    return text


def _window_start_seq(conversation_id: int, limit: int) -> int | None:
    """上下文窗口里**最旧**那条消息的 seq。``None`` = 整个会话还没超出窗口。"""
    with session_scope() as session:
        total = session.scalar(
            select(func.count())
            .select_from(ChatMessage)
            .where(ChatMessage.conversation_id == conversation_id)
        )
        if int(total or 0) <= limit:
            return None
        seq = session.scalar(
            select(ChatMessage.seq)
            .where(ChatMessage.conversation_id == conversation_id)
            .order_by(ChatMessage.seq.desc())
            .offset(limit - 1)
            .limit(1)
        )
    return None if seq is None else int(seq)


def _load_summary(conversation_id: int) -> tuple[str, int]:
    with session_scope() as session:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            return "", 0
        return conversation.memory_summary or "", int(conversation.memory_summary_upto_seq or 0)


def _load_range(conversation_id: int, *, after: int, before: int) -> list[_Row]:
    with session_scope() as session:
        rows = session.execute(
            select(
                ChatMessage.seq,
                ChatMessage.role,
                ChatMessage.content,
                ChatMessage.tool_name,
                ChatMessage.tool_ok,
            )
            .where(
                ChatMessage.conversation_id == conversation_id,
                ChatMessage.seq > after,
                ChatMessage.seq < before,
            )
            .order_by(ChatMessage.seq)
        ).all()
    return [_Row(r.seq, r.role, r.content or "", r.tool_name, r.tool_ok) for r in rows]


def _fit_budget(rows: list[_Row]) -> tuple[list[_Row], int | None]:
    """按输入预算从旧到新取，返回 ``(取到的行, 覆盖到的 seq)``。

    见模块开头第 3 条取舍：``upto`` 必须跟着**实际取到的那一条**走。
    """
    taken: list[_Row] = []
    used = 0
    last_seq: int | None = None
    for row in rows:
        text = _render_one(row)
        if taken and used + len(text) > _SUMMARY_INPUT_CHARS:
            break
        taken.append(row)
        used += len(text)
        last_seq = row.seq
    return taken, last_seq


def _render_one(row: _Row) -> str:
    if row.role == MSG_ROLE_TOOL:
        # 工具结果只留名字和成败：压缩器的输入也有预算，
        # 而"查过什么"比"返回的那一大段 JSON"更值得占位置。
        # 真正的数字应该已经在助手当时的回答里了（那条会被保留）。
        return f"[工具] {row.tool_name or '未知'} → {'成功' if row.tool_ok else '失败'}"
    who = "用户" if row.role == MSG_ROLE_USER else "助手"
    text = " ".join((row.content or "").split())
    return f"[{who}] {text}" if text else ""


def _render_for_summary(rows: list[_Row]) -> str:
    return "\n".join(part for part in (_render_one(row) for row in rows) if part)


def _summarize(model_client: Any, *, previous: str, block: str) -> str:
    """把「旧摘要 + 新并进来的内容」压成一段新摘要。失败返回空串。"""
    parts: list[str] = []
    if previous.strip():
        parts.append(f"【已有摘要（覆盖更早的内容）】\n{previous.strip()}")
    parts.append(f"【需要并进来的新内容】\n{block}")

    response = model_client.complete(
        [Message.system(_SUMMARY_PROMPT), Message.user("\n\n".join(parts))],
        temperature=0.2,
    )
    text = (response.content or "").strip()
    if len(text) > _MAX_SUMMARY_CHARS:
        # 超长直接截断而不是重试：一段 5000 字的"摘要"已经不是摘要了，
        # 它会挤占本该留给原文的窗口，而重试只会再花一次钱、结果未必更短。
        text = text[:_MAX_SUMMARY_CHARS].rstrip() + "…"
    return text


def _save_summary(conversation_id: int, text: str, upto: int) -> None:
    with session_scope() as session:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            return
        conversation.memory_summary = text
        conversation.memory_summary_upto_seq = upto


__all__ = [
    "SUMMARY_HEADING",
    "build_block",
    "context_limit",
    "maybe_refresh",
    "refresh_every",
]
