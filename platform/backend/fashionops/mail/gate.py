"""邮件发送闸门（Fail-Closed Policy Gate）。

## 为什么需要单独一层闸门

工具网关已经有限流、熔断、审计、权限了，为什么还要再来一层？

因为两者管的**不是同一件事**：

| 层 | 管什么 | 例子 |
| --- | --- | --- |
| 工具网关 | **技术治理** | 这工具一分钟最多调几次、连续失败要不要熔断、调用留痕 |
| 邮件闸门 | **业务策略** | 这封信**该不该**发出去、能发给谁、能带什么附件 |

网关不知道"发给 `ceo@competitor.com` 是不合适的"，那是业务判断。
把它塞进网关会让网关变成一个个特例的堆积；单独一层才能讲清楚规则。

## 核心设计：fail-closed

给 Agent 装上一双能发邮件的手，等于给它一个**对外发声**的通道。
邮件发出去就收不回来 —— 不像数据库写错了可以回滚。
所以这里的每一个默认值都选了"更安全的那个"：

1. **总开关默认关**（``mail_enabled=false``）
2. **白名单为空 = 禁止发送任何邮件**。这是最关键的一条：
   漏配管理员配置的后果是"发不出去"，而不是"能发给任何人"。
3. **黑名单优先于白名单**。命中白名单但也在黑名单里 → 拒绝。
4. **收件人数量上限**，防一次误群发。
5. **附件扩展名 + 大小双限**，防把密钥文件或 200MB 日志发出去。
6. **按收件人限流**，防模型陷入循环把同一封信重复发几十遍。
7. **dry_run 优先于投递但排在策略检查之后** ——
   它也要跑完全部检查，否则开发期永远测不出限流和黑名单。

## 为什么返回决策对象而不是抛异常

这个闸门**必须把被拦下的邮件也记进库**（``email_messages`` 表的
``gate_decision`` / ``gate_reason`` 字段就是为它准备的）。
抛异常的话，异常往上冒的过程中很容易丢掉"到底哪一层拦的、为什么拦的"，
最后库里只剩一行 `status=failed`，复盘时完全看不出原因。

面试时最有说服力的不是"我做了安全策略"，而是能打开库说：
**"这 8 封信是为什么被拦下来的"，逐条讲得出原因。**
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from fashionops.gateway.rate_limit import RateLimiter

GateCode = Literal[
    "allowed",
    "dry_run",
    "blocked_disabled",
    "blocked_no_recipient",
    "blocked_bad_address",
    "blocked_denylist",
    "blocked_whitelist",
    "blocked_recipients",
    "blocked_attachment_type",
    "blocked_size",
    "blocked_ratelimit",
]

#: 这些决策意味着"策略放行了"（dry_run 也算放行，只是不真正投递）
PASSED_CODES: frozenset[str] = frozenset({"allowed", "dry_run"})

#: 宽松但够用的地址格式。刻意不做完整的 RFC 5322 解析 ——
#: 目的是挡住手滑和注入，不是做邮箱校验服务。
_ADDRESS_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class AttachmentInfo:
    """待发附件。只需要元信息，闸门不读文件内容。"""

    filename: str
    size_bytes: int
    asset_no: str | None = None
    storage_key: str | None = None


@dataclass(frozen=True)
class GateDecision:
    """闸门结论。**不论放行还是拒绝都要落库。**

    为什么要把 ``to`` / ``cc`` / ``bcc`` 分组回传（而不是只给一个并集）：
    调用方最终要用这些地址去组装邮件，而**头部的 To/Cc 与投递名单不是一回事** ——
    BCC 必须进投递名单但绝不能进头部。让调用方从并集里自己切是做不到的：
    去重是跨字段做的，切出来的归属是错的。
    """

    allowed: bool
    code: GateCode
    reason: str = ""
    hint: str = ""
    to: tuple[str, ...] = ()
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()

    @property
    def recipients(self) -> tuple[str, ...]:
        """投递名单（含 BCC）。"""
        return (*self.to, *self.cc, *self.bcc)

    @property
    def blocked(self) -> bool:
        return not self.allowed

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "reason": self.reason,
            "hint": self.hint,
            "to": list(self.to),
            "cc": list(self.cc),
            "bcc": list(self.bcc),
            "recipients": list(self.recipients),
        }


class MailGate:
    """发信策略检查器。"""

    def __init__(self, policy, limiter: RateLimiter | None = None) -> None:  # noqa: ANN001
        self._p = policy
        self._limiter = limiter

    # ------------------------------------------------------------------
    def evaluate(
        self,
        *,
        to: list[str],
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        attachments: list[AttachmentInfo] | None = None,
    ) -> GateDecision:
        """按顺序跑完全部检查，返回第一个不通过的结论。"""
        cc = cc or []
        bcc = bcc or []
        attachments = attachments or []
        checks: list[str] = []

        # ---- 先把收件人解析出来，再做任何检查 ----
        #
        # 为什么把解析提到最前面："这封被拦的信本来要发给谁"是复盘时
        # 最有价值的信息之一。如果总开关检查放在前面，被拦记录里就是空的收件人，
        # 事后根本查不出当时想发给谁。解析是纯字符串处理，不做也无所谓性能。
        to_c, cc_c, bcc_c = split_fields(to, cc, bcc)
        recipients = (*to_c, *cc_c, *bcc_c)
        #: 被拦时也要把收件人带上，落库记录才有复盘价值
        who = (to_c, cc_c, bcc_c)

        # ---- 0. 总开关 ----
        if not self._p.mail_enabled:
            return self._block(
                "blocked_disabled",
                "邮件发送总开关未开启（mail_enabled=false）",
                "需要管理员在 .env 里设 FASHIONOPS_MAIL_ENABLED=true。当前邮件无法发出，请走人工流程。",
                *who,
            )
        checks.append("enabled")

        if not recipients:
            return self._block(
                "blocked_no_recipient",
                "没有有效的收件人地址",
                "请提供至少一个收件人，例如 ops@example.com。",
            )

        bad = [a for a in recipients if not _ADDRESS_RE.match(a)]
        if bad:
            return self._block(
                "blocked_bad_address",
                f"地址格式不合法：{', '.join(bad[:3])}",
                "地址需要形如 name@example.com。请修正后重试。",
                *who,
            )
        checks.append("address_format")

        # ---- 2. 黑名单（优先于白名单）----
        denylist = self._p.mail_denylist
        for address in recipients:
            for pattern in denylist:
                if _matches(address.lower(), pattern.lower()):
                    return self._block(
                        "blocked_denylist",
                        f"收件人 {address} 命中黑名单规则 {pattern}",
                        "该地址被管理员禁止。请改用其他收件人，或联系管理员调整配置。",
                        *who,
                    )
        checks.append("denylist")

        # ---- 3. 白名单：空 = 禁发 ----
        allowlist = self._p.mail_allowlist
        if not allowlist:
            return self._block(
                "blocked_whitelist",
                "收件人白名单为空，出于安全考虑禁止发送任何邮件",
                (
                    "需要管理员配置 FASHIONOPS_MAIL_SEND_ALLOWLIST（例如 @example.com）"
                    "后才能发送。可以先把邮件内容交给人工发送。"
                ),
                *who,
            )
        outside = [a for a in recipients if not any(_matches(a.lower(), p.lower()) for p in allowlist)]
        if outside:
            return self._block(
                "blocked_whitelist",
                f"收件人 {', '.join(outside[:3])} 不在白名单内（当前允许：{', '.join(allowlist)}）",
                "如确需发送，请让管理员加入白名单；否则请改为人工转发。",
                *who,
            )
        checks.append("allowlist")

        # ---- 4. 收件人数量 ----
        limit_n = self._p.mail_max_recipients
        if len(recipients) > limit_n:
            return self._block(
                "blocked_recipients",
                f"收件人过多（{len(recipients)} 个），上限 {limit_n} 个",
                "请拆分多次发送，或先与用户确认收件人名单。",
                *who,
            )
        checks.append("recipient_count")

        # ---- 5. 附件 ----
        allowed_exts = self._p.mail_attachment_exts
        max_bytes = self._p.mail_max_attachment_bytes
        for att in attachments:
            if allowed_exts:
                suffix = att.filename[att.filename.rfind(".") :].lower() if "." in att.filename else ""
                if suffix not in allowed_exts:
                    return self._block(
                        "blocked_attachment_type",
                        f"附件 {att.filename} 的类型 {suffix or '(无扩展名)'} 不允许外发",
                        f"允许的类型：{', '.join(allowed_exts)}。请换一种格式，或把内容写进正文。",
                        *who,
                    )
            if att.size_bytes > max_bytes:
                return self._block(
                    "blocked_size",
                    (
                        f"附件 {att.filename} 过大"
                        f"（{att.size_bytes / 1024 / 1024:.1f} MB），"
                        f"上限 {max_bytes // 1024 // 1024} MB"
                    ),
                    "请压缩或拆分该文件，或改为只发下载链接。",
                    *who,
                )
        checks.append("attachment")

        # ---- 6. 按收件人限流 ----
        if self._limiter is not None:
            per_minute = self._p.mail_rate_limit_per_minute
            for address in recipients:
                decision = self._limiter.hit(f"mail:{address.lower()}", per_minute, 60)
                if not decision.allowed:
                    return self._block(
                        "blocked_ratelimit",
                        (f"收件人 {address} 最近一分钟已收 {decision.limit} 封，超过上限 {per_minute} 封"),
                        (
                            f"请等待约 {int(decision.retry_after_s) + 1} 秒后重试。"
                            "如果这是循环发信，说明任务逻辑有问题。"
                        ),
                        *who,
                    )
        checks.append("rate_limit")

        # ---- 7. 到这里策略已放行，最后才看 dry_run ----
        if self._p.mail_dry_run or not self._p.mail_can_really_send:
            return GateDecision(
                allowed=True,
                code="dry_run",
                reason="dry_run 模式：策略已放行，仅记录不投递",
                hint="要真发需同时满足：mail_enabled=true、mail_dry_run=false、配置了 smtp_host。",
                to=to_c,
                cc=cc_c,
                bcc=bcc_c,
                checks=tuple(checks),
            )

        return GateDecision(
            allowed=True,
            code="allowed",
            reason="全部策略检查通过",
            to=to_c,
            cc=cc_c,
            bcc=bcc_c,
            checks=tuple(checks),
        )

    # ------------------------------------------------------------------
    def _block(
        self,
        code: GateCode,
        reason: str,
        hint: str,
        to: tuple[str, ...] = (),
        cc: tuple[str, ...] = (),
        bcc: tuple[str, ...] = (),
    ) -> GateDecision:
        return GateDecision(allowed=False, code=code, reason=reason, hint=hint, to=to, cc=cc, bcc=bcc)

    @property
    def policy(self) -> dict[str, object]:
        """给模型看的策略摘要，不含密码。"""
        return self._p.mail_policy_summary()


# ---------------------------------------------------------------------------
# 匹配与清洗
# ---------------------------------------------------------------------------
def _matches(address: str, pattern: str) -> bool:
    """白/黑名单匹配，支持三种写法。

    * ``*`` —— 匹配全部（谨慎使用）
    * ``@corp.com`` / ``*@corp.com`` —— 后缀匹配
    * ``a@b.com`` —— 精确匹配
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


def _extract_address(raw: str) -> str:
    """从 ``张三尺 <a@b.com>`` 里取出 ``a@b.com``。

    模型经常把显示名和地址一起吐出来，直接拿整串比白名单必然不命中 ——
    而"白名单里明明写了这个地址却不匹配"是极难排查的一类问题。
    """
    text = (raw or "").strip()
    if "<" in text and ">" in text:
        inner = text[text.rfind("<") + 1 : text.rfind(">")].strip()
        if inner:
            text = inner
    return text.strip().strip('",;')


def _dedupe(addresses: list[str]) -> tuple[str, ...]:
    """去空白、取真实地址、按小写去重（保留首次出现的原始大小写）。"""
    seen: set[str] = set()
    out: list[str] = []
    for raw in addresses:
        address = _extract_address(raw)
        if not address:
            continue
        key = address.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(address)
    return tuple(out)


def split_fields(
    to: list[str], cc: list[str], bcc: list[str]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """把收件人清洗归一化，**并做跨字段去重**（先出现的字段优先）。

    为什么跨字段去重：同一个地址既在 To 又在 Cc 里，SMTP 会把它算两个收件人 ——
    不只是浪费一次投递额度，更麻烦的是"收件人数量上限"会被虚高的数字触发，
    运维看到的是"10 个收件人超限了"，实际只有 6 个人。

    为什么不能只返回并集：调用方要用 To/Cc 组装邮件头部，
    而 BCC 必须只进投递名单、不能进头部。并集切不出正确的归属。
    """
    to_c = _dedupe(to)
    seen = {a.lower() for a in to_c}

    cc_out: list[str] = []
    for address in _dedupe(cc):
        if address.lower() not in seen:
            seen.add(address.lower())
            cc_out.append(address)

    bcc_out: list[str] = []
    for address in _dedupe(bcc):
        if address.lower() not in seen:
            seen.add(address.lower())
            bcc_out.append(address)

    return to_c, tuple(cc_out), tuple(bcc_out)


__all__ = [
    "PASSED_CODES",
    "AttachmentInfo",
    "GateCode",
    "GateDecision",
    "MailGate",
    "split_fields",
]
