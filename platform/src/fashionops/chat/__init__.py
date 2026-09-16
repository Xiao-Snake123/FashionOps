"""对话域 —— 会话、消息、对话内审批，以及对话 Agent 本身。

对外只需要这三个入口：

    from fashionops.chat import agent, store
    agent.stream_turn(conversation_no="CH...", principal=p, user_text="...")
"""

from __future__ import annotations

from fashionops.chat import agent, store

__all__ = ["agent", "store"]
