"""人工审批：策略的唯一入口。

对话侧和任务侧都从这里拿判据（见 `policy.py` 的模块注释），
不要在别处再写一份 `needs_approval`。
"""

from fashionops.approval.policy import (
    APPROVAL_EXEMPT,
    MAIL_SEND,
    describe_preview,
    needs_approval,
)

__all__ = [
    "APPROVAL_EXEMPT",
    "MAIL_SEND",
    "describe_preview",
    "needs_approval",
]
