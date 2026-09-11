"""邮件 MCP 模块。"""

from office_mcp.email.client import MailService
from office_mcp.email.guard import SendGuard
from office_mcp.email.models import (
    AttachmentInfo,
    DraftResult,
    EmailDetail,
    EmailSummary,
    FolderInfo,
    SendRequest,
    SendResult,
)

__all__ = [
    "AttachmentInfo",
    "DraftResult",
    "EmailDetail",
    "EmailSummary",
    "FolderInfo",
    "MailService",
    "SendGuard",
    "SendRequest",
    "SendResult",
]
