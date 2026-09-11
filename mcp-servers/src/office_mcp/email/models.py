"""邮件模块的数据模型。

统一用 Pydantic 描述，MCP 层可以据此自动生成 JSON Schema。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

MAX_SUBJECT_CHARS = 300


class AttachmentInfo(BaseModel):
    """附件元信息（不含内容）。"""

    filename: str = Field(description="附件文件名")
    content_type: str = Field(default="application/octet-stream", description="MIME 类型")
    size_bytes: int = Field(default=0, ge=0, description="字节数")


class EmailSummary(BaseModel):
    """邮件列表项，用于 ``search_emails`` 返回，体积小、可批量。"""

    uid: str = Field(description="邮件在所在文件夹内的 UID，后续操作用它定位")
    folder: str = Field(description="所在文件夹，如 INBOX")
    subject: str = Field(description="主题")
    sender: str = Field(description="发件人，形如 ``张三 <zhangsan@x.com>``")
    recipients: list[str] = Field(default_factory=list, description="收件人")
    date: datetime | None = Field(default=None, description="发送时间")
    snippet: str = Field(default="", description="正文摘要（前若干字符）")
    unread: bool = Field(default=False, description="是否未读")
    has_attachments: bool = Field(default=False, description="是否带附件")


class EmailDetail(EmailSummary):
    """单封邮件详情，用于 ``read_email``。"""

    body_text: str = Field(default="", description="纯文本正文（已截断）")
    body_truncated: bool = Field(default=False, description="正文是否因过长被截断")
    attachments: list[AttachmentInfo] = Field(default_factory=list, description="附件列表")
    message_id: str | None = Field(default=None, description="Message-ID，回复时用于串联会话")


class SendRequest(BaseModel):
    """发信请求。"""

    to: list[str] = Field(min_length=1, description="收件人，至少一个")
    subject: str = Field(description="邮件主题")
    body_text: str = Field(default="", description="纯文本正文")
    body_html: str | None = Field(default=None, description="HTML 正文（可选）")
    cc: list[str] = Field(default_factory=list, description="抄送")
    bcc: list[str] = Field(default_factory=list, description="密送")
    attachments: list[str] = Field(
        default_factory=list,
        description="附件在工作区内的相对路径列表，如 ``outputs/周报.docx``",
    )
    reply_to_uid: str | None = Field(
        default=None, description="若为回复邮件，填原邮件 UID，会自动带上 Re: 与引用头"
    )

    @field_validator("subject")
    @classmethod
    def _trim_subject(cls, value: str) -> str:
        cleaned = value.replace("\r", " ").replace("\n", " ").strip()
        if not cleaned:
            raise ValueError("邮件主题不能为空")
        return cleaned[:MAX_SUBJECT_CHARS]


class FolderInfo(BaseModel):
    """文件夹信息。"""

    name: str = Field(description="文件夹名")
    delimiter: str = Field(default="/", description="层级分隔符")
    flags: list[str] = Field(default_factory=list, description="IMAP 标志")


class SendResult(BaseModel):
    """发信结果。``dry_run`` 为真时没有真实投递。"""

    dry_run: bool = Field(default=False, description="是否只是演练（未真实发送）")
    recipients: list[str] = Field(default_factory=list, description="实际收件人（含 cc/bcc）")
    subject: str = ""
    attachments: list[AttachmentInfo] = Field(default_factory=list)
    message_id: str | None = None
    saved_to: str | None = Field(default=None, description="保存到哪个文件夹（如已发送）")


class DraftResult(BaseModel):
    """存草稿的结果。"""

    path: str = Field(description="草稿文件在工作区内的路径（.eml）")
    subject: str = ""
    recipients: list[str] = Field(default_factory=list)


MailAction = Literal["mark_read", "mark_unread", "move", "delete"]
