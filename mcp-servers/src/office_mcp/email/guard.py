"""邮件发送闸门（Guard）。

**这是整个邮件模块最重要的部分。**

给 Agent 装上一双能发邮件的手，等同于给它一个对外发声的通道。一旦模型判断
失误或被提示词注入攻击，后果是真实且不可撤回的（邮件发出去就收不回来）。
因此这里采用 **fail-closed** 设计：

1. **白名单为空 = 禁止发送**。必须显式配置 ``OFFICE_MCP_MAIL_SEND_ALLOWLIST``
   才能发出第一封邮件。漏配的默认后果是"发不出去"，而不是"能发给任何人"。
2. **黑名单优先**。即使命中白名单，只要命中黑名单也拒绝。
3. **收件人数量上限**，防止一次群发。
4. **附件扩展名 + 大小双限**，防止把密钥文件或超大文件发出去。
5. **滑动窗口限流**，防止模型陷入循环把同一个动作重复执行几十次。
6. **dry_run 优先于一切**，用于调试期零副作用验证。

所有拒绝都返回带 ``hint`` 的结构化错误，让模型知道**为什么被拒**以及**该怎么办**。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from pathlib import Path

from office_mcp.common.config import Settings
from office_mcp.common.errors import MailPolicyViolation
from office_mcp.email.models import AttachmentInfo


class SendGuard:
    """发信策略检查器。无状态部分纯函数，限流部分带进程内状态。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._sent_at: deque[float] = deque()

    # ---- 主入口 ----

    def check(
        self,
        *,
        to: list[str],
        cc: list[str],
        bcc: list[str],
        attachments: list[AttachmentInfo],
    ) -> list[str]:
        """执行全部检查。通过则返回去重后的收件人列表，否则抛异常。"""
        recipients = _dedupe([*to, *cc, *bcc])
        if not recipients:
            raise MailPolicyViolation("没有有效的收件人。", hint="请提供至少一个收件人地址。")

        for address in recipients:
            self._check_address(address)

        max_recipients = self._settings.mail_max_recipients
        if len(recipients) > max_recipients:
            raise MailPolicyViolation(
                f"收件人过多（{len(recipients)} 个），上限 {max_recipients} 个。",
                hint="请拆分多次发送，或先与用户确认收件人名单。",
            )

        for attachment in attachments:
            self._check_attachment(attachment)

        self._check_rate_limit()
        return recipients

    # ---- 单项检查 ----

    def _check_address(self, address: str) -> None:
        lowered = address.lower()

        for blocked in self._settings.mail_send_denylist:
            if _matches(lowered, blocked.lower()):
                raise MailPolicyViolation(
                    f"收件人 {address} 命中黑名单 {blocked}，拒绝发送。",
                    hint="该地址被管理员禁止。请改用其他收件人，或联系管理员调整配置。",
                )

        allowlist = self._settings.mail_send_allowlist
        if not allowlist:
            raise MailPolicyViolation(
                "邮件发送白名单为空，出于安全考虑禁止发送任何邮件。",
                hint=(
                    "需要管理员配置环境变量 OFFICE_MCP_MAIL_SEND_ALLOWLIST"
                    "（例如 @corp.example.com）后才能发送。你可以先把邮件存成草稿。"
                ),
            )

        if not any(_matches(lowered, allowed.lower()) for allowed in allowlist):
            raise MailPolicyViolation(
                f"收件人 {address} 不在允许的白名单内。",
                hint=(
                    f"当前允许：{', '.join(allowlist)}。"
                    "如确需发送，请让管理员加入白名单，或先把邮件存成草稿交给人工发送。"
                ),
            )

    def _check_attachment(self, attachment: AttachmentInfo) -> None:
        suffix = Path(attachment.filename).suffix.lower()
        allowed = {ext.lower() for ext in self._settings.mail_attachment_allowlist}
        if allowed and suffix not in allowed:
            raise MailPolicyViolation(
                f"附件类型 {suffix or '(无扩展名)'} 不允许外发。",
                hint=f"允许的附件类型：{', '.join(sorted(allowed))}。",
            )

        if attachment.size_bytes > self._settings.mail_max_attachment_bytes:
            limit_mb = self._settings.mail_max_attachment_bytes / 1024 / 1024
            actual_mb = attachment.size_bytes / 1024 / 1024
            raise MailPolicyViolation(
                f"附件 {attachment.filename} 过大（{actual_mb:.1f} MB），上限 {limit_mb:.0f} MB。",
                hint="请压缩该文件，或改为把文件路径告诉用户让其自行发送。",
            )

    def _check_rate_limit(self) -> None:
        limit = self._settings.mail_rate_limit_per_minute
        now = time.monotonic()
        with self._lock:
            while self._sent_at and now - self._sent_at[0] > 60:
                self._sent_at.popleft()
            if len(self._sent_at) >= limit:
                wait = int(60 - (now - self._sent_at[0])) + 1
                raise MailPolicyViolation(
                    f"发送过于频繁：最近一分钟已发 {len(self._sent_at)} 封，上限 {limit} 封。",
                    hint=f"请等待约 {wait} 秒后重试。如果这是循环发信，说明任务逻辑有问题。",
                )

    # ---- 记账 ----

    def record_attempt(self) -> None:
        """记账一次**策略已放行的发送尝试**。

        注意是「尝试」而不是「投递成功」：``dry_run`` 也会计数。
        为什么？因为限流是**策略**的一部分，而 dry_run 的价值就在于能离线把整条策略
        链路跑一遍。如果 dry_run 跳过限流，开发期永远测不出限流，等上线才第一次触发。
        滑动窗口是一分钟，多计数几十次也不会造成实际影响。
        """
        with self._lock:
            self._sent_at.append(time.monotonic())

    def reset(self) -> None:
        """清空限流状态（测试用）。"""
        with self._lock:
            self._sent_at.clear()

    @property
    def status(self) -> dict[str, object]:
        """给模型看的策略状态，不含敏感信息。"""
        return {
            "send_enabled": self._settings.mail_send_enabled,
            "allowlist": list(self._settings.mail_send_allowlist),
            "denylist": list(self._settings.mail_send_denylist),
            "max_recipients": self._settings.mail_max_recipients,
            "rate_limit_per_minute": self._settings.mail_rate_limit_per_minute,
            "max_attachment_bytes": self._settings.mail_max_attachment_bytes,
            "attachment_allowlist": list(self._settings.mail_attachment_allowlist),
            "dry_run": self._settings.dry_run,
        }


# --------------------------------------------------------------------------- #
# 匹配规则
# --------------------------------------------------------------------------- #


def _matches(address: str, pattern: str) -> bool:
    """白/黑名单匹配。

    支持三种写法：

    - ``*``            —— 匹配全部（谨慎使用）
    - ``@corp.com``    —— 后缀匹配，命中该域下任意邮箱
    - ``a@corp.com``   —— 完整地址精确匹配
    """
    pattern = pattern.strip().lower()
    if not pattern:
        return False
    if pattern == "*":
        return True
    if pattern.startswith("@"):
        return address.endswith(pattern)
    if pattern.startswith("*@"):
        return address.endswith(pattern[1:])
    return address == pattern


def _dedupe(addresses: list[str]) -> list[str]:
    """去重并保守地清洗地址：去掉空白、去掉 ``Name <addr>`` 包裹、忽略空项。"""
    seen: set[str] = set()
    result: list[str] = []
    for raw in addresses:
        address = _extract_address(raw)
        if not address:
            continue
        key = address.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(address)
    return result


def _extract_address(raw: str) -> str:
    """从 ``张三 <a@b.com>`` 中取出 ``a@b.com``；已是裸地址则原样返回。"""
    text = (raw or "").strip()
    if not text:
        return ""
    if "<" in text and ">" in text:
        start = text.rindex("<") + 1
        end = text.index(">", start)
        text = text[start:end].strip()
    return text
