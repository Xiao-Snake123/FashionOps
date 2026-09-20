"""跨会话的长期记忆 —— 记住用户的**偏好和约定**，而不是记住每一句话。

分层说明（它和会话内摘要的区别）写在 `db/models_memory.py` 里，这里不重复。

## 写入：让模型显式调 ``remember``，不做后台抽取

两条路都走得通，选了前者：

* **后台每轮抽一次**（跑一遍模型问"这轮有值得记的吗"）—— 不漏，
  但每轮多一次调用，而且它会在用户不知情的时候**悄悄攒一堆东西**。
* **模型主动调 ``remember``**（本方案）—— 零额外调用，
  由模型判断"这句话是不是一条会长期有效的偏好"。

选后者的关键理由不是省钱，是**可追溯**：记忆里出现一条莫名其妙的内容时，
前者只能去翻"哪一轮的抽取把它带出来的"；后者有 `source_conversation_no`
指回原话，而且**是模型自己在那轮判断该记的**。

代价是模型可能忘了记。这个代价可以接受：漏记的表现是"下次又问一遍"，
用户当场就会说出来，那时再记也来得及。**多记才是麻烦** ——
一条"用户随口提过的 SKU"混进长期记忆，之后每一轮都会被注入，
变成一种没人能解释的偏差。所以工具描述里对"什么时候该记"写得很紧。

## 纪律写在**工具描述**里，不写在 system prompt 里

"什么时候该记"是这个工具的用法，所以它写在 `remember` 的 description 里
（`chat/tools.py`），和其他工具一致。system prompt 里只留"你记得这个用户什么"。

理由：模型的注意力在"挑哪个工具"时读的就是那段描述；
再往 system prompt 里抄一份，只会让它和另外十条纪律抢位置。

## 每轮注入的是**全量（取最近 N 条）**，不做向量检索

一个用户想不出几十条互不重叠的长期偏好（`topic` 唯一约束限制了这一点），
所以注入上限（默认 20）就够覆盖。为这点数据上一套向量检索，
换来的是"每次对话多一次 embedding 调用 + 一个可能召回错的环节"，
收益和代价不成比例。真到记不下的时候，该做的是让用户清理，
而不是让检索来掩盖"记得太多"。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from fashionops.db import session_scope
from fashionops.db.models_memory import (
    MAX_CONTENT_CHARS,
    MAX_TOPIC_CHARS,
    MEMORY_KIND_FACT,
    MEMORY_KIND_PREFERENCE,
    MEMORY_KINDS,
    AgentMemory,
)

logger = logging.getLogger("fashionops.chat.longterm")

#: 注入块在 system prompt 里的标题。
MEMORY_HEADING = "## 你记得的关于这位用户的事"

#: kind → 注入时给人看的中文标签。只影响可读性，不影响行为。
_KIND_LABELS: dict[str, str] = {
    "preference": "偏好",
    "fact": "事实",
    "note": "备注",
}


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def enabled() -> bool:
    from fashionops.config import get_settings

    return bool(getattr(get_settings(), "chat_memory_enabled", True))


def inject_limit() -> int:
    from fashionops.config import get_settings

    return max(0, int(getattr(get_settings(), "chat_memory_inject_limit", 20) or 0))


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def serialize(row: AgentMemory) -> dict[str, Any]:
    return {
        "id": row.id,
        "topic": row.topic,
        "content": row.content,
        "kind": row.kind,
        "username": row.username,
        "source_conversation_no": row.source_conversation_no,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def upsert(
    *,
    user_id: int,
    username: str,
    topic: str,
    content: str,
    kind: str = MEMORY_KIND_PREFERENCE,
    source_conversation_no: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """记一条记忆，返回 ``(行, 是否新建)``。

    同一个 ``topic`` 再记一次就是**更新**而不是追加 —— 理由见
    `db/models_memory.py` 里"为什么用 topic 参与唯一键"那一段
    （两条互相矛盾的记忆同时被注入，比只有一条旧的更糟）。
    """
    with session_scope() as session:
        row = session.scalar(
            select(AgentMemory).where(
                AgentMemory.user_id == user_id, AgentMemory.topic == topic
            )
        )
        created = row is None
        if row is None:
            row = AgentMemory(
                user_id=user_id,
                username=username,
                topic=topic,
                content=content,
                kind=kind,
                source_conversation_no=source_conversation_no,
            )
            session.add(row)
        else:
            row.content = content
            row.kind = kind
            # 用户名也刷成最新的：这条记忆现在归他管，快照要跟着他的当前名字，
            # 否则会出现"记忆里写着旧名字、界面上是新人"的错位。
            row.username = username
            if source_conversation_no:
                row.source_conversation_no = source_conversation_no
        session.flush()
        payload = serialize(row)

    logger.info(
        "长期记忆%s：user=%s topic=%r", "新增" if created else "更新", username, topic
    )
    return payload, created


def forget(*, user_id: int, topic: str) -> bool:
    """删掉一条记忆。返回是否真的删到了。

    只按 ``topic`` **精确**匹配，不做模糊搜索：模糊匹配会在
    "删除"这个动作上引入"删错了一条"的可能，而那个错误没有撤销入口。
    模型在注入块里能看到确切的 topic 原文，用它就行。
    """
    with session_scope() as session:
        result = session.execute(
            sa_delete(AgentMemory).where(
                AgentMemory.user_id == user_id, AgentMemory.topic == topic
            )
        )
        removed = int(result.rowcount or 0) > 0
    if removed:
        logger.info("长期记忆删除：user_id=%s topic=%r", user_id, topic)
    return removed


def list_for_user(user_id: int, *, limit: int | None = None) -> list[dict[str, Any]]:
    """按**最近更新**倒序取这个人的记忆。``limit=None`` 表示全部。"""
    stmt = (
        select(AgentMemory)
        .where(AgentMemory.user_id == user_id)
        .order_by(AgentMemory.updated_at.desc(), AgentMemory.id.desc())
    )
    if limit is not None and limit > 0:
        stmt = stmt.limit(limit)
    with session_scope() as session:
        return [serialize(row) for row in session.scalars(stmt)]


# ---------------------------------------------------------------------------
# 注入
# ---------------------------------------------------------------------------
def build_block(rows: list[dict[str, Any]]) -> str:
    """把记忆拼成 system prompt 里的一段。没有记忆时返回空串。"""
    if not rows:
        return ""

    lines: list[str] = []
    for row in rows:
        label = _KIND_LABELS.get(str(row.get("kind")), str(row.get("kind") or ""))
        line = f"* [{row.get('topic')}·{label}] {row.get('content')}"
        # `fact` 是**有时效**的（"9 月退货率 3.2%"）。
        # 不标时间的话，模型会把一个月前的数字当成今天的 ——
        # 这正是这个系统最不能出的错（数字必须可信）。
        if row.get("kind") == MEMORY_KIND_FACT and row.get("updated_at"):
            line += f"（记于 {str(row['updated_at'])[:10]}，可能已过期，引用前请重新查证）"
        lines.append(line)

    return (
        f"{MEMORY_HEADING}\n\n"
        "这是你在**以前的对话**里为这位用户记下的，仍然有效 —— "
        "可以直接照做，不必再问一遍。\n"
        "但如果用户这次的说法不一样，**一律以这次为准**，"
        "并用 `remember` 更新同一个 topic（不要新开一个 topic，那会留下两条互相矛盾的说法）。\n\n"
        + "\n".join(lines)
    )


def load_block(user_id: int) -> str:
    """取这个人的记忆并拼成注入块。**任何失败都返回空串**。

    记忆是锦上添花：读不出来时，这轮对话应该照常跑，
    而不是因为一张附属表出了问题就整个聊不了。
    """
    if not enabled():
        return ""
    try:
        return build_block(list_for_user(user_id, limit=inject_limit()))
    except Exception:  # noqa: BLE001
        logger.exception("长期记忆读取失败，本轮按「没有记忆」继续")
        return ""


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------
def remember_tool(
    *,
    user_id: int,
    username: str,
    arguments: dict[str, Any],
    conversation_no: str | None = None,
) -> dict[str, Any]:
    """``remember`` 工具。返回的是**给模型看的** payload。

    参数不合法时返回 ``ok=False`` + ``hint``，而不是抛异常：
    模型能照着 hint 自己改一次。抛异常的话这一轮直接断了，
    而"topic 忘填了"根本不值得断一轮。
    """
    if not enabled():
        return {
            "ok": False,
            "error": {
                "code": "MEMORY_DISABLED",
                "message": "长期记忆功能已被关闭",
                "hint": "告诉用户这个功能被管理员关掉了，**不要重试**。",
            },
        }

    topic = str(arguments.get("topic") or "").strip()
    content = str(arguments.get("content") or "").strip()
    kind = str(arguments.get("kind") or MEMORY_KIND_PREFERENCE).strip().lower()

    if not topic or not content:
        return {
            "ok": False,
            "error": {
                "code": "INVALID_ARGUMENTS",
                "message": "topic 和 content 都不能为空",
                "hint": (
                    "topic 是这条记忆的标题（2~8 个字，如「渠道偏好」「报表口径」），"
                    "content 是要记住的那一句话。"
                ),
            },
        }

    if kind not in MEMORY_KINDS:
        # 认不出来的 kind 退回最保守的那种：宁可当成普通偏好，
        # 也不要因为一个拼错的枚举值把整次记住操作拒掉。
        kind = MEMORY_KIND_PREFERENCE

    payload, created = upsert(
        user_id=user_id,
        username=username,
        topic=topic[:MAX_TOPIC_CHARS],
        content=content[:MAX_CONTENT_CHARS],
        kind=kind,
        source_conversation_no=conversation_no,
    )
    return {
        "ok": True,
        "data": {
            "topic": payload["topic"],
            "content": payload["content"],
            "action": "created" if created else "updated",
            "note": (
                "已经记住了。**不要在回答里郑重宣布「我记住了」** —— "
                "自然地把这件事带过去就好，用户要的是被记住，不是被告知。"
            ),
        },
    }


def forget_tool(*, user_id: int, arguments: dict[str, Any]) -> dict[str, Any]:
    """``forget`` 工具。"""
    if not enabled():
        return {
            "ok": False,
            "error": {
                "code": "MEMORY_DISABLED",
                "message": "长期记忆功能已被关闭",
                "hint": "告诉用户即可，不要重试。",
            },
        }

    topic = str(arguments.get("topic") or "").strip()
    if not topic:
        return {
            "ok": False,
            "error": {
                "code": "INVALID_ARGUMENTS",
                "message": "topic 不能为空",
                "hint": "用注入块里那个 topic 的原文（方括号里、点号前面那部分）。",
            },
        }

    if not forget(user_id=user_id, topic=topic):
        # 删不到也要说清"没有这条"，而不是假装成功 ——
        # 假装成功会让模型告诉用户"已经忘了"，而实际还在每轮注入。
        return {
            "ok": False,
            "error": {
                "code": "NOT_FOUND",
                "message": f"没有 topic 为「{topic}」的记忆",
                "hint": (
                    "检查 topic 是否和注入块里的原文一致。"
                    "确实没有的话就当没记过，**不要换个写法反复删**。"
                ),
            },
        }
    return {"ok": True, "data": {"topic": topic, "action": "deleted"}}


__all__ = [
    "MEMORY_HEADING",
    "build_block",
    "enabled",
    "forget",
    "forget_tool",
    "inject_limit",
    "list_for_user",
    "load_block",
    "remember_tool",
    "serialize",
    "upsert",
]
