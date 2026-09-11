"""邮件客户端：IMAP 读收件箱 + SMTP 发信。

刻意只用 Python 标准库（``imaplib`` / ``smtplib`` / ``email``），不引入第三方依赖：
邮件协议本身很稳定，而第三方库带来的版本与安全补丁负担反而更重。

可测试性设计：:class:`MailService` 通过构造函数注入 ``mailbox`` 与 ``sender``，
单元测试可以传假实现，无需真实邮箱服务器。
"""

from __future__ import annotations

import contextlib
import email
import email.policy
import imaplib
import logging
import mimetypes
import re
import smtplib
import ssl
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol

from office_mcp.common.config import Settings
from office_mcp.common.errors import (
    MailOperationError,
    UpstreamUnavailableError,
)
from office_mcp.common.paths import safe_filename
from office_mcp.email.guard import SendGuard, _extract_address
from office_mcp.email.models import (
    AttachmentInfo,
    DraftResult,
    EmailDetail,
    EmailSummary,
    FolderInfo,
    SendRequest,
    SendResult,
)

logger = logging.getLogger("office_mcp.email")

_IMAP_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _imap_date(value: date) -> str:
    """格式化为 IMAP 要求的 ``dd-Mon-yyyy``。

    不能用 ``strftime('%b')`` —— 它依赖进程 locale，中文 Windows 下会输出「9月」。
    """
    return f"{value.day:02d}-{_IMAP_MONTHS[value.month - 1]}-{value.year}"


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #


class Mailbox(Protocol):
    """收件箱操作接口。"""

    def list_folders(self) -> list[FolderInfo]: ...

    def search(self, folder: str, criteria: Sequence[str], limit: int) -> list[EmailSummary]: ...

    def fetch(self, folder: str, uid: str) -> EmailDetail: ...

    def set_seen(self, folder: str, uid: str, seen: bool) -> None: ...

    def move(self, folder: str, uid: str, target: str) -> None: ...

    def delete(self, folder: str, uid: str) -> None: ...

    def append(self, folder: str, raw: bytes, flags: Iterable[str] = ()) -> None: ...


class Sender(Protocol):
    """发信接口。"""

    def send(self, message: EmailMessage) -> str: ...


# --------------------------------------------------------------------------- #
# IMAP 实现
# --------------------------------------------------------------------------- #


@dataclass
class _RawMessage:
    uid: str
    raw: bytes
    flags: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedAttachment:
    """已通过路径沙箱校验的附件。

    ``info`` 给策略检查与返回值用，``path`` 给实际读文件用。
    两者绑在一起，避免“检查的是 A、发出去的是 B”这类漏洞。
    """

    info: AttachmentInfo
    path: Path


class ImapMailbox:
    """基于标准库 ``imaplib`` 的实现。每次操作建立短连接。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # ---- 连接 ----

    def _connect(self) -> imaplib.IMAP4:
        s = self._settings
        if not s.imap_configured:
            raise UpstreamUnavailableError(
                "IMAP 未配置，无法读取邮件。",
                hint=(
                    "请设置 OFFICE_MCP_IMAP_HOST / OFFICE_MCP_MAIL_ADDRESS / "
                    "OFFICE_MCP_MAIL_PASSWORD。"
                ),
            )
        try:
            if s.imap_use_ssl:
                client: imaplib.IMAP4 = imaplib.IMAP4_SSL(
                    s.imap_host, s.imap_port, ssl_context=ssl.create_default_context()
                )
            else:
                client = imaplib.IMAP4(s.imap_host, s.imap_port)
                client.starttls(ssl_context=ssl.create_default_context())
            client.login(s.mail_address, s.mail_password or "")
            return client
        except imaplib.IMAP4.error as exc:
            raise UpstreamUnavailableError(
                f"IMAP 登录失败：{_scrub(str(exc), s.mail_password)}",
                hint="请检查邮箱地址、密码（多数邮箱需使用「授权码」而非登录密码）与服务器地址。",
            ) from exc
        except (OSError, ssl.SSLError) as exc:
            raise UpstreamUnavailableError(
                f"无法连接 IMAP 服务器 {s.imap_host}:{s.imap_port} —— {exc}",
                hint="请检查网络、端口与防火墙；内网环境可能需要配置代理。",
            ) from exc

    # ---- 读操作 ----

    def list_folders(self) -> list[FolderInfo]:
        client = self._connect()
        try:
            status, data = client.list()
            if status != "OK":
                raise MailOperationError("无法列出邮件文件夹。")
            return [_parse_folder(item) for item in data if item]
        finally:
            _close(client)

    def search(self, folder: str, criteria: Sequence[str], limit: int) -> list[EmailSummary]:
        client = self._connect()
        try:
            _select(client, folder)
            uids = _search_uids(client, criteria)
            if not uids:
                return []
            # 取最新的 limit 封
            selected = uids[-limit:]
            raws = _fetch_headers(client, selected)
            return [_summary_from_raw(item, folder) for item in raws]
        finally:
            _close(client)

    def fetch(self, folder: str, uid: str) -> EmailDetail:
        client = self._connect()
        try:
            _select(client, folder)
            status, data = client.uid("FETCH", uid, "(RFC822 FLAGS)")
            if status != "OK":
                raise MailOperationError(
                    f"读取邮件 UID={uid} 失败。",
                    hint=f"请先用 search_emails 确认该 UID 在 {folder} 中存在。",
                )
            raw, flags = _extract_fetch(data)
            if raw is None:
                raise MailOperationError(
                    f"邮件 UID={uid} 不存在或已被删除。",
                    hint=f"请重新用 search_emails 搜索 {folder} 获取最新 UID。",
                )
            return _detail_from_raw(raw, folder, uid, flags, self._settings.imap_max_body_chars)
        finally:
            _close(client)

    # ---- 写操作 ----

    def set_seen(self, folder: str, uid: str, seen: bool) -> None:
        client = self._connect()
        try:
            _select(client, folder, readonly=False)
            command = "+FLAGS" if seen else "-FLAGS"
            status, _ = client.uid("STORE", uid, command, "(\\Seen)")
            if status != "OK":
                raise MailOperationError(f"更新邮件 {uid} 的已读状态失败。")
        finally:
            _close(client)

    def move(self, folder: str, uid: str, target: str) -> None:
        client = self._connect()
        try:
            _select(client, folder, readonly=False)
            status, _ = client.uid("MOVE", uid, target)
            if status != "OK":
                # 部分服务器不支持 MOVE，回退到 COPY + DELETE
                status, _ = client.uid("COPY", uid, target)
                if status != "OK":
                    raise MailOperationError(
                        f"无法把邮件移动到 {target}。",
                        hint="请先用 list_folders 确认目标文件夹存在、名称拼写正确。",
                    )
                client.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
                client.expunge()
        finally:
            _close(client)

    def delete(self, folder: str, uid: str) -> None:
        client = self._connect()
        try:
            _select(client, folder, readonly=False)
            status, _ = client.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
            if status != "OK":
                raise MailOperationError(f"删除邮件 {uid} 失败。")
            client.expunge()
        finally:
            _close(client)

    def append(self, folder: str, raw: bytes, flags: Iterable[str] = ()) -> None:
        client = self._connect()
        try:
            flag_str = f"({' '.join(flags)})" if flags else None
            status, _ = client.append(folder, flag_str, None, raw)
            if status != "OK":
                raise MailOperationError(f"无法把邮件写入 {folder} 文件夹。")
        finally:
            _close(client)


# --------------------------------------------------------------------------- #
# SMTP 实现
# --------------------------------------------------------------------------- #


class SmtpSender:
    """基于标准库 ``smtplib`` 的实现。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def send(self, message: EmailMessage) -> str:
        s = self._settings
        if not s.mail_configured:
            raise UpstreamUnavailableError(
                "SMTP 未配置，无法发送邮件。",
                hint=(
                    "请设置 OFFICE_MCP_SMTP_HOST / OFFICE_MCP_MAIL_ADDRESS / "
                    "OFFICE_MCP_MAIL_PASSWORD。"
                ),
            )
        try:
            if s.smtp_use_ssl:
                server: smtplib.SMTP = smtplib.SMTP_SSL(
                    s.smtp_host, s.smtp_port, context=ssl.create_default_context()
                )
            else:
                server = smtplib.SMTP(s.smtp_host, s.smtp_port)
                server.starttls(context=ssl.create_default_context())
            with server:
                server.login(s.mail_address, s.mail_password or "")
                server.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            raise UpstreamUnavailableError(
                f"SMTP 认证失败：{_scrub(str(exc), s.mail_password)}",
                hint="请检查邮箱地址与授权码；部分邮箱需要在设置中单独开启 SMTP 服务。",
            ) from exc
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            raise UpstreamUnavailableError(
                f"发送失败：{exc}",
                hint="请检查网络与 SMTP 服务器配置。若不确定，可先用 dry_run 模式验证内容。",
            ) from exc
        return str(message.get("Message-ID", ""))


# --------------------------------------------------------------------------- #
# 服务门面
# --------------------------------------------------------------------------- #


class MailService:
    """把收件箱、发信器与安全闸门组合起来。"""

    def __init__(
        self,
        settings: Settings,
        *,
        mailbox: Mailbox | None = None,
        sender: Sender | None = None,
        guard: SendGuard | None = None,
        resolve_attachment: Callable[[str], Path] | None = None,
    ) -> None:
        self._settings = settings
        self._mailbox = mailbox or ImapMailbox(settings)
        self._sender = sender or SmtpSender(settings)
        self._guard = guard or SendGuard(settings)
        # 附件路径解析器：server 层注入路径沙箱校验，测试可注入桩函数
        self._resolve: Callable[[str], Path] = resolve_attachment or Path

    @property
    def guard(self) -> SendGuard:
        return self._guard

    # ---- 读 ----

    def list_folders(self) -> list[FolderInfo]:
        return self._mailbox.list_folders()

    def search(
        self,
        *,
        folder: str = "INBOX",
        unread_only: bool = False,
        from_address: str | None = None,
        subject_contains: str | None = None,
        body_contains: str | None = None,
        since: date | None = None,
        before: date | None = None,
        recent_days: int | None = None,
        limit: int = 20,
    ) -> list[EmailSummary]:
        criteria: list[str] = []
        if unread_only:
            criteria.append("UNSEEN")
        if from_address:
            criteria.append(f'FROM "{_escape_imap(from_address)}"')
        if subject_contains:
            criteria.append(f'SUBJECT "{_escape_imap(subject_contains)}"')
        if body_contains:
            criteria.append(f'TEXT "{_escape_imap(body_contains)}"')

        if recent_days is not None:
            since = date.today() - timedelta(days=max(0, recent_days))
        if since:
            criteria.append(f"SINCE {_imap_date(since)}")
        if before:
            criteria.append(f"BEFORE {_imap_date(before)}")

        if not criteria:
            criteria.append("ALL")

        return self._mailbox.search(folder, criteria, limit)

    def read(self, folder: str, uid: str) -> EmailDetail:
        return self._mailbox.fetch(folder, uid)

    # ---- 写 ----

    def set_seen(self, folder: str, uid: str, seen: bool) -> None:
        self._mailbox.set_seen(folder, uid, seen)

    def move(self, folder: str, uid: str, target: str) -> None:
        self._mailbox.move(folder, uid, target)

    def delete(self, folder: str, uid: str) -> None:
        self._mailbox.delete(folder, uid)

    def send(self, request: SendRequest) -> SendResult:
        """校验并发送邮件。路径解析与安全闸门都在这里收口。"""
        attachments = self._collect_attachments(request.attachments)
        recipients = self._guard.check(
            to=request.to,
            cc=request.cc,
            bcc=request.bcc,
            attachments=[a.info for a in attachments],
        )

        message = self._build_message(request, attachments)

        # 先记账再分叉：dry_run 也要计入限流，否则离线永远验证不到限流策略。
        self._guard.record_attempt()

        if self._settings.dry_run:
            logger.info("dry_run：跳过真实发送，收件人 %s", recipients)
            return SendResult(
                dry_run=True,
                recipients=recipients,
                subject=request.subject,
                attachments=[a.info for a in attachments],
            )

        message_id = self._sender.send(message)

        saved_to = None
        if self._settings.mail_save_sent_copy:
            try:
                self._mailbox.append(self._settings.mail_sent_folder, message.as_bytes())
                saved_to = self._settings.mail_sent_folder
            except Exception as exc:  # noqa: BLE001 - 存档失败不应让发送失败
                logger.warning("已发送邮件存档失败（不影响投递）：%s", exc)

        return SendResult(
            dry_run=False,
            recipients=recipients,
            subject=request.subject,
            attachments=[a.info for a in attachments],
            message_id=message_id or None,
            saved_to=saved_to,
        )

    def save_draft(self, request: SendRequest) -> DraftResult:
        """把邮件存成工作区里的 ``.eml`` 草稿。

        这是白名单被拒时的**推荐替代路径** —— 让 Agent 有办法"把事做完"，
        而不是卡在"发不出去"上。
        """
        attachments = self._collect_attachments(request.attachments)
        message = self._build_message(request, attachments)
        drafts_dir = self._settings.workspace_root / "drafts"
        drafts_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = _safe_draft_name(request.subject)
        path = drafts_dir / f"{stamp}-{name}.eml"
        path.write_bytes(message.as_bytes())

        return DraftResult(
            path=str(path.relative_to(self._settings.workspace_root)).replace("\\", "/"),
            subject=request.subject,
            recipients=[_extract_address(a) for a in request.to],
        )

    # ---- 内部 ----

    def _collect_attachments(self, paths: list[str]) -> list[ResolvedAttachment]:
        resolved: list[ResolvedAttachment] = []
        for raw in paths:
            path = self._resolve(raw)
            if not path.exists():
                raise MailOperationError(
                    f"附件不存在：{raw}",
                    hint="请先用 list_files 确认文件路径，或先用文档工具生成该文件。",
                )
            resolved.append(
                ResolvedAttachment(
                    info=AttachmentInfo(
                        filename=path.name,
                        content_type=mimetypes.guess_type(path.name)[0]
                        or "application/octet-stream",
                        size_bytes=path.stat().st_size,
                    ),
                    path=path,
                )
            )
        return resolved

    def _build_message(
        self, request: SendRequest, attachments: Sequence[ResolvedAttachment]
    ) -> EmailMessage:
        message = EmailMessage()
        message["From"] = self._settings.mail_address or ""
        message["To"] = ", ".join(request.to)
        if request.cc:
            message["Cc"] = ", ".join(request.cc)
        if request.bcc:
            # 必须写进 Bcc 头：smtplib.send_message 从 To/Cc/Bcc 汇总收件人，
            # 并在传输副本里删掉 Bcc 头。漏了这行密送收件人根本收不到邮件。
            message["Bcc"] = ", ".join(request.bcc)
        message["Subject"] = request.subject
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid()

        if request.body_text:
            message.set_content(request.body_text)
        if request.body_html:
            if request.body_text:
                message.add_alternative(request.body_html, subtype="html")
            else:
                message.set_content(request.body_html, subtype="html")

        # 回复场景：带上 In-Reply-To / References 让邮件客户端正确串联会话
        if request.reply_to_uid:
            try:
                original = self._mailbox.fetch("INBOX", request.reply_to_uid)
            except Exception:  # noqa: BLE001 - 拿不到原邮件就不串会话，不该阻断发送
                logger.info("未能读取原邮件 %s，跳过会话串联", request.reply_to_uid)
            else:
                if original.message_id:
                    message["In-Reply-To"] = original.message_id
                    message["References"] = original.message_id
                if request.subject and not request.subject.lower().startswith("re:"):
                    message.replace_header("Subject", f"Re: {request.subject}")

        for resolved in attachments:
            content_type = resolved.info.content_type
            maintype, _, subtype = content_type.partition("/")
            message.add_attachment(
                resolved.path.read_bytes(),
                maintype=maintype,
                subtype=subtype or "octet-stream",
                filename=resolved.info.filename,
            )
        return message


# --------------------------------------------------------------------------- #
# 解析辅助
# --------------------------------------------------------------------------- #


def _scrub(text: str, secret: str | None) -> str:
    """把错误信息里可能出现的密码抹掉。"""
    if not secret:
        return text
    return text.replace(secret, "***")


def _escape_imap(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _select(client: imaplib.IMAP4, folder: str, *, readonly: bool = True) -> None:
    status, _ = client.select(f'"{_escape_imap(folder)}"', readonly=readonly)
    if status != "OK":
        raise MailOperationError(
            f"无法打开文件夹 {folder}。",
            hint="请先用 list_folders 查看可用文件夹名称（注意大小写与层级分隔符）。",
        )


def _search_uids(client: imaplib.IMAP4, criteria: Sequence[str]) -> list[str]:
    """执行 UID SEARCH，返回按升序排列的 UID 列表。"""
    args: list[Any] = []
    needs_charset = any(not c.isascii() for c in criteria)
    if needs_charset:
        args.extend([b"CHARSET", b"UTF-8"])
    for item in criteria:
        args.append(item.encode("utf-8") if not item.isascii() else item)

    status, data = client.uid("SEARCH", *args)
    if status != "OK":
        raise MailOperationError(
            "邮件搜索失败。",
            hint="请简化搜索条件（去掉中文关键词或日期范围）后重试。",
        )
    if not data or not data[0]:
        return []
    return [uid.decode() for uid in data[0].split()]


def _fetch_headers(client: imaplib.IMAP4, uids: Sequence[str]) -> list[_RawMessage]:
    """批量取邮件头与标志位，用于构造列表项。

    只取 ``BODY.PEEK[HEADER]``（不碰正文），因此即使列表里 50 封邮件也很快，
    而且 ``PEEK`` 不会把未读邮件意外标成已读。
    """
    uid_set = ",".join(uids)
    status, data = client.uid("FETCH", uid_set, "(FLAGS BODY.PEEK[HEADER])")
    if status != "OK":
        raise MailOperationError("读取邮件列表失败。")

    results: list[_RawMessage] = []
    pending_uid = ""
    pending_flags: tuple[str, ...] = ()
    for item in data:
        if isinstance(item, tuple):
            prefix = item[0]
            if isinstance(prefix, bytes):
                pending_uid = _uid_from_prefix(prefix) or pending_uid
                pending_flags = _flags_from_prefix(prefix) or ()
            payload = item[1]
            if isinstance(payload, bytes):
                results.append(_RawMessage(uid=pending_uid, raw=payload, flags=pending_flags))
            pending_uid = ""
            pending_flags = ()
    return results


_UID_RE = re.compile(rb"UID (\d+)")
_FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")


def _uid_from_prefix(prefix: bytes) -> str | None:
    match = _UID_RE.search(prefix)
    return match.group(1).decode() if match else None


def _flags_from_prefix(prefix: bytes) -> tuple[str, ...] | None:
    match = _FLAGS_RE.search(prefix)
    if match is None:
        return None
    return tuple(part.decode(errors="ignore") for part in match.group(1).split())


def _bodystructure_has_attachment(raw: bytes) -> bool:
    """（已废弃）保留仅供迁移参考。

    早期版本试图从 BODYSTRUCTURE 响应里判断附件，但 imaplib 对不同服务器返回的
    响应切片形状不一致，状态机容易错位。现在列表页改用头部启发式（见
    ``_header_suggests_attachment``），精确信息由 ``read_email`` 提供。
    """
    return b"ATTACHMENT" in raw.upper()


def _extract_fetch(data: list[Any]) -> tuple[bytes | None, tuple[str, ...]]:
    """从 FETCH 响应里取出完整邮件原文与标志位。"""
    raw: bytes | None = None
    flags: tuple[str, ...] = ()
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2:
            prefix, payload = item[0], item[1]
            if isinstance(prefix, bytes):
                parsed = _flags_from_prefix(prefix)
                if parsed:
                    flags = parsed
            if isinstance(payload, bytes):
                raw = payload
        elif isinstance(item, bytes) and b"FLAGS" in item:
            parsed = _flags_from_prefix(item)
            if parsed:
                flags = parsed
    return raw, flags


def _header_suggests_attachment(message: Any) -> bool:
    """仅凭邮件头部猜测是否含附件。

    判据：顶层 Content-Type 为 ``multipart/mixed``，或出现 ``Content-Disposition: attachment``。
    这是**启发式**：能覆盖绝大多数带附件的邮件，但可能把内联图片的邮件误报为有附件。
    这也正是 ``read_document`` 之外还要有 ``read_email`` 的原因 —— 列表页要快，详情页要准。
    """
    content_type = str(message.get("Content-Type") or "").lower()
    if "multipart/mixed" in content_type:
        return True
    disposition = str(message.get("Content-Disposition") or "").lower()
    return "attachment" in disposition


def _summary_from_raw(item: _RawMessage, folder: str) -> EmailSummary:
    message = email.message_from_bytes(item.raw, policy=email.policy.default)
    return EmailSummary(
        uid=item.uid,
        folder=folder,
        subject=_header(message, "Subject"),
        sender=_header(message, "From"),
        recipients=_address_list(message, "To"),
        date=_parse_date(message.get("Date")),
        snippet="",
        unread="\\Seen" not in item.flags,
        has_attachments=_header_suggests_attachment(message),
    )


def _detail_from_raw(
    raw: bytes, folder: str, uid: str, flags: tuple[str, ...], max_chars: int
) -> EmailDetail:
    message = email.message_from_bytes(raw, policy=email.policy.default)

    body_text = ""
    body_html = ""
    attachments: list[AttachmentInfo] = []

    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart():
                continue
            disposition = (part.get_content_disposition() or "").lower()
            filename = part.get_filename()
            if disposition == "attachment" or (filename and disposition != "inline"):
                payload = part.get_payload(decode=True) or b""
                attachments.append(
                    AttachmentInfo(
                        filename=_decode_mime_words(filename or "unnamed"),
                        content_type=part.get_content_type(),
                        size_bytes=len(payload),
                    )
                )
                continue
            content_type = part.get_content_type()
            try:
                text = part.get_content()
            except (LookupError, ValueError):
                payload = part.get_payload(decode=True) or b""
                text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            if content_type == "text/plain" and not body_text:
                body_text = text
            elif content_type == "text/html" and not body_html:
                body_html = text
    else:
        try:
            content = message.get_content()
        except (LookupError, ValueError):
            payload = message.get_payload(decode=True) or b""
            content = payload.decode(message.get_content_charset() or "utf-8", errors="replace")
        if message.get_content_type() == "text/html":
            body_html = content
        else:
            body_text = content

    if not body_text:
        body_text = _html_to_text(body_html)

    truncated = len(body_text) > max_chars
    if truncated:
        body_text = body_text[:max_chars]

    return EmailDetail(
        uid=uid,
        folder=folder,
        subject=_header(message, "Subject"),
        sender=_header(message, "From"),
        recipients=_address_list(message, "To"),
        date=_parse_date(message.get("Date")),
        snippet=body_text[:160],
        unread="\\Seen" not in flags,
        has_attachments=bool(attachments),
        body_text=body_text,
        body_truncated=truncated,
        attachments=attachments,
        message_id=str(message.get("Message-ID") or "") or None,
    )


def _header(message: Any, name: str) -> str:
    value = message.get(name)
    return _decode_mime_words(str(value)) if value else ""


def _address_list(message: Any, name: str) -> list[str]:
    value = message.get(name)
    if not value:
        return []
    return [str(addr) for addr in value.addresses] if hasattr(value, "addresses") else [str(value)]


def _decode_mime_words(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").strip()


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None


_TAG_RE = re.compile(r"<[^>]+>")
_STYLE_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")


def _html_to_text(html: str) -> str:
    """极简 HTML 转文本，仅在邮件没有 text/plain 部分时兜底。"""
    if not html:
        return ""
    text = _STYLE_RE.sub(" ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = _TAG_RE.sub("", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _parse_folder(raw: Any) -> FolderInfo:
    text = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
    # 形如: (\HasNoChildren) "/" "INBOX"
    match = re.match(r'\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+(?P<name>.+)', text)
    if not match:
        return FolderInfo(name=text.strip().strip('"'))
    name = match.group("name").strip()
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1]
    return FolderInfo(
        name=name,
        delimiter=match.group("delim") or "/",
        flags=[f for f in match.group("flags").split() if f],
    )


def _safe_draft_name(subject: str) -> str:
    """复用路径沙箱的文件名清洗，避免“主题里带 ``/`` 就写到外面去”。"""
    return safe_filename(subject, default_ext="")[:60] or "draft"


def _close(client: imaplib.IMAP4) -> None:
    with contextlib.suppress(Exception):
        client.logout()
