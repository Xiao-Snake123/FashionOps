"""邮件投递后端。

把"投递"抽成接口，理由和 ``StorageBackend``、``VectorStore`` 一样：
**开发期不该被真实外部依赖卡住**。

* ``DryRunSender`` —— 一个字都不发，只返回"我本来会发什么"。
  开发期默认走这条，零配置、零副作用、零误发风险。
* ``RecordingSender`` —— 测试用，把每封信存在内存里，断言用。
* ``SmtpSender`` —— 真的发。只在闸门放行 + 配好 SMTP 时才会被调用。

## 为什么用标准库 smtplib 而不是第三方库

这个场景只需要"发一封带附件的邮件"，标准库完全够：
``smtplib.SMTP_SSL`` + ``email.message.EmailMessage``。

引第三方库（比如 ``yagmail``）会多一个依赖、多一层要审计的代码，
而且很多库为了"好用"会把认证信息散在构造参数里 —— 那反而更难做凭证隔离。

**附件一律从对象存储读字节**，不让调用方传本地路径。
传路径意味着 Agent（或注入进来的提示词）可以指定服务器上任意文件，
``/etc/shadow`` 或者 ``.env`` 都可能被打包发出去。
"""

from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage as StdEmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Protocol


@dataclass
class OutboundMessage:
    """一封待发邮件。``attachments`` 是 ``(文件名, 字节)``，不是路径。"""

    from_addr: str
    to: tuple[str, ...]
    subject: str
    body_text: str
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    body_html: str | None = None
    attachments: tuple[tuple[str, bytes], ...] = ()

    @property
    def all_recipients(self) -> tuple[str, ...]:
        """SMTP 投递用（含 BCC）。"""
        return (*self.to, *self.cc, *self.bcc)

    @property
    def header_recipients(self) -> tuple[str, ...]:
        """会出现在邮件头部里的收件人（To + Cc，**不含 Bcc**）。"""
        return (*self.to, *self.cc)


@dataclass
class SendResult:
    ok: bool
    backend: str
    detail: str = ""
    error: str = ""
    message_id: str = ""


class EmailSender(Protocol):
    backend_name: str

    def send(self, message: OutboundMessage) -> SendResult: ...


class DryRunSender:
    """不投递，只报告"本来会发什么"。"""

    backend_name = "dry_run"

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    def send(self, message: OutboundMessage) -> SendResult:
        self.sent.append(message)
        return SendResult(
            ok=True,
            backend=self.backend_name,
            detail=(
                f"dry_run：未投递。本来会发给 {len(message.all_recipients)} 个收件人，"
                f"附件 {len(message.attachments)} 个"
            ),
            message_id=make_msgid(domain=message.from_addr.split("@")[-1] or "fashionops.local"),
        )


class RecordingSender(DryRunSender):
    """测试专用。行为同 DryRunSender，名字更直白。"""

    backend_name = "recording"


class SmtpSender:
    """真实 SMTP 投递。"""

    backend_name = "smtp"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        from_addr: str,
        use_ssl: bool = True,
        timeout: float = 20.0,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.from_addr = from_addr
        self.use_ssl = use_ssl
        self.timeout = timeout

    # ------------------------------------------------------------------
    def build(self, message: OutboundMessage) -> StdEmailMessage:
        """组装 MIME 邮件。抽出来是为了能在不连 SMTP 的情况下单测头部。"""
        msg = StdEmailMessage()
        msg["From"] = formataddr(("运营 Agent", message.from_addr))
        # To 和 Cc 必须分开设。不能把两者拼进 To ——
        # 那样收件人看到的头部就分不出谁是直接收件人谁是抄送。
        if message.to:
            msg["To"] = ", ".join(message.to)
        if message.cc:
            msg["Cc"] = ", ".join(message.cc)
        # Bcc **不设头部**：它只进投递名单。设了头部密送就失效了，
        # 而且这个错误很难发现 —— 功能看起来完全正常，
        # 只是所有人都看到了本该隐藏的地址。
        msg["Subject"] = message.subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=message.from_addr.split("@")[-1] or "fashionops.local")

        msg.set_content(message.body_text or "")
        if message.body_html:
            msg.add_alternative(message.body_html, subtype="html")

        for filename, payload in message.attachments:
            maintype, subtype = _guess_type(filename)
            msg.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
        return msg

    # ------------------------------------------------------------------
    def send(self, message: OutboundMessage) -> SendResult:
        """真实投递。

        收件人**显式传** ``all_recipients``（含 BCC），而不是让 smtplib
        从头部推导。虽然标准库的 ``send_message`` 确实会把 ``Bcc`` 算进去并在
        投递副本里删掉它，但依赖这个隐式行为太脆 —— 哪天有人改了头部组装方式，
        密送就会静默失效。显式传参就不会有这种惊喜。
        """
        recipients = list(message.all_recipients)
        if not recipients:
            return SendResult(ok=False, backend=self.backend_name, error="没有收件人")

        msg = self.build(message)
        try:
            if self.use_ssl:
                with smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout) as server:
                    self._login(server)
                    server.send_message(msg, from_addr=self.from_addr, to_addrs=recipients)
            else:
                with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as server:
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
                    self._login(server)
                    server.send_message(msg, from_addr=self.from_addr, to_addrs=recipients)
        except (smtplib.SMTPException, OSError) as exc:
            # 只报异常类型和文本，不把连接串/凭证带进错误信息
            return SendResult(
                ok=False,
                backend=self.backend_name,
                error=f"{type(exc).__name__}: {exc}",
            )
        return SendResult(
            ok=True,
            backend=self.backend_name,
            detail=f"已投递给 {len(recipients)} 个收件人",
            message_id=str(msg["Message-ID"]),
        )

    def _login(self, server: smtplib.SMTP) -> None:
        """登录。用户名为空时跳过 —— 有些内网中继不需要认证。"""
        if self.user:
            server.login(self.user, self.password)


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
def get_sender(settings) -> EmailSender:  # noqa: ANN001
    """按配置返回投递后端。

    **不满足真发条件时返回 DryRunSender，而不是报错** ——
    报错会让"还没配 SMTP"的开发环境整条链路都跑不起来。
    降级 + 在结果里说明原因，比直接失败有用。
    """
    if not settings.mail_can_really_send:
        return DryRunSender()
    return SmtpSender(
        host=settings.smtp_host,
        port=settings.smtp_port,
        user=settings.smtp_user,
        password=settings.smtp_password or "",
        from_addr=settings.mail_from,
        use_ssl=settings.smtp_use_ssl,
        timeout=settings.smtp_timeout,
    )


_TYPE_MAP = {
    ".xlsx": ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ".pdf": ("application", "pdf"),
    ".csv": ("text", "csv"),
    ".txt": ("text", "plain"),
    ".png": ("image", "png"),
    ".jpg": ("image", "jpeg"),
    ".jpeg": ("image", "jpeg"),
}


def _guess_type(filename: str) -> tuple[str, str]:
    lower = filename.lower()
    for suffix, (main, sub) in _TYPE_MAP.items():
        if lower.endswith(suffix):
            return main, sub
    return "application", "octet-stream"


__all__ = [
    "DryRunSender",
    "EmailSender",
    "OutboundMessage",
    "RecordingSender",
    "SendResult",
    "SmtpSender",
    "get_sender",
]
