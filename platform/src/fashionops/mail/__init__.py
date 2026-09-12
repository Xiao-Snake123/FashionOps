"""邮件模块：策略闸门 + 投递后端。

分两层是因为它们要分开测：

* ``gate`` —— 纯策略判断，**不碰网络**，所以能密集单测（白名单、黑名单、
  附件限制、限流、dry_run 优先级……每个分支一条用例）
* ``sender`` —— 真的碰网络，用假后端替换就能在不发信的前提下测完整链路
"""

from fashionops.mail.gate import (
    PASSED_CODES,
    AttachmentInfo,
    GateDecision,
    MailGate,
    split_fields,
)
from fashionops.mail.sender import (
    DryRunSender,
    EmailSender,
    OutboundMessage,
    RecordingSender,
    SendResult,
    SmtpSender,
    get_sender,
)

__all__ = [
    "PASSED_CODES",
    "AttachmentInfo",
    "DryRunSender",
    "EmailSender",
    "GateDecision",
    "MailGate",
    "OutboundMessage",
    "RecordingSender",
    "SendResult",
    "SmtpSender",
    "get_sender",
    "split_fields",
]
