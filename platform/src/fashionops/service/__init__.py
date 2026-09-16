"""客服域 —— 工单的两层处理（代码快车道 + Agent 兜底）。

对外入口：

    from fashionops.service import handle_ticket, triage
    from fashionops.service.rules import review_escalation

和 `chat/` 一样，这个包不依赖 HTTP 层：CLI、对话 Agent、定时任务都能用同一套逻辑。
"""

from __future__ import annotations

__all__ = ["rules"]
