"""邮件闸门 + 投递测试。

分三层：

1. **闸门是纯函数** —— 不碰网络，所以能把每个分支密集测一遍
2. **投递后端** —— 用假后端替换，在不发信的前提下走完整链路
3. **工具层端到端** —— 真实 SQLite 临时库 + 真实工具网关

重点断言的是**被拦下的邮件也要落库** —— 这是这个模块最容易被做错、
也最难在事后发现的地方。

（原来的第 4 层"通知节点端到端"随 `notification.py` 一起删了：
新架构里"起草邮件"由 Agent 自己写、"发送"由 `mail.send` 工具做，
两者分别在 `tests/test_loop.py`（发信前挂闸口）和本文件第 3 层被覆盖。）
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sqlalchemy.orm import Session

from fashionops.agents.context import RunContext
from fashionops.db.base import Base, build_engine, get_engine, reset_engine, utcnow
from fashionops.db.models import Asset, EmailMessage
from fashionops.gateway import (
    CallContext,
    IdempotencyStore,
    MemoryAuditSink,
    MemoryCircuitBreaker,
    MemoryRateLimiter,
    ToolGateway,
    ToolRegistry,
)
from fashionops.llm import MockModelClient
from fashionops.mail import (
    AttachmentInfo,
    DryRunSender,
    MailGate,
    OutboundMessage,
    SmtpSender,
    split_fields,
)
from fashionops.tools import AGENT_SCOPES, register_all_tools


# ===========================================================================
# 策略替身（闸门只依赖这些属性，不需要真的 Settings）
# ===========================================================================
@dataclass
class FakePolicy:
    mail_enabled: bool = True
    mail_dry_run: bool = True
    mail_allowlist: tuple[str, ...] = ("@example.com",)
    mail_denylist: tuple[str, ...] = ()
    mail_max_recipients: int = 10
    mail_rate_limit_per_minute: int = 6
    mail_max_attachment_bytes: int = 20 * 1024 * 1024
    mail_attachment_exts: tuple[str, ...] = (".xlsx", ".pdf", ".csv", ".png", ".jpg")
    mail_from: str = "agent@fashionops.local"
    mail_can_really_send: bool = False
    extra: dict = field(default_factory=dict)

    def mail_policy_summary(self) -> dict:
        return {"enabled": self.mail_enabled, "dry_run": self.mail_dry_run, **self.extra}


def _gate(**kw) -> MailGate:  # noqa: ANN003
    return MailGate(FakePolicy(**kw))


# ===========================================================================
# 1. 闸门：每个拒绝分支
# ===========================================================================
class TestGateBlocks:
    def test_disabled_switch_blocks(self):
        d = _gate(mail_enabled=False).evaluate(to=["ops@example.com"])
        assert d.code == "blocked_disabled"
        assert not d.allowed
        # 被拦记录也要带上有意投递的地址 —— "本来要发给谁"是复盘时最需要的信息
        assert d.to == ("ops@example.com",)

    def test_no_recipient(self):
        assert _gate().evaluate(to=[]).code == "blocked_no_recipient"

    def test_blank_address_is_dropped_not_blocked(self):
        """空白项是"没填"，不是"填错了" —— 直接忽略比报错合理。"""
        d = _gate().evaluate(to=["", "  ", "ops@example.com"])
        assert d.code == "dry_run"
        assert d.to == ("ops@example.com",)

    def test_malformed_address(self):
        d = _gate().evaluate(to=["not-an-email"])
        assert d.code == "blocked_bad_address"

    def test_empty_allowlist_is_fail_closed(self):
        """**最关键的一条**：白名单没配 = 谁都发不出去。

        漏配管理员配置的后果必须是"发不出去"，而不是"能发给任何人"。
        """
        d = _gate(mail_allowlist=()).evaluate(to=["ops@example.com"])
        assert d.code == "blocked_whitelist"
        assert "白名单为空" in d.reason
        assert "FASHIONOPS_MAIL_SEND_ALLOWLIST" in d.hint

    def test_outside_allowlist(self):
        d = _gate().evaluate(to=["someone@gmail.com"])
        assert d.code == "blocked_whitelist"
        assert "someone@gmail.com" in d.reason

    def test_denylist_beats_allowlist(self):
        """黑名单优先级高于白名单 —— 两个都命中时必须是拒绝。"""
        d = _gate(mail_denylist=("bad@example.com",)).evaluate(to=["bad@example.com"])
        assert d.code == "blocked_denylist"

    def test_too_many_recipients(self):
        d = _gate(mail_max_recipients=2).evaluate(to=["a@example.com", "b@example.com", "c@example.com"])
        assert d.code == "blocked_recipients"

    def test_attachment_type_not_allowed(self):
        d = _gate().evaluate(to=["ops@example.com"], attachments=[AttachmentInfo(".env", 100)])
        assert d.code == "blocked_attachment_type"
        assert ".env" in d.reason

    def test_attachment_no_extension_blocked(self):
        d = _gate().evaluate(to=["ops@example.com"], attachments=[AttachmentInfo("secrets", 100)])
        assert d.code == "blocked_attachment_type"

    def test_attachment_too_large(self):
        d = _gate().evaluate(
            to=["ops@example.com"],
            attachments=[AttachmentInfo("report.xlsx", 30 * 1024 * 1024)],
        )
        assert d.code == "blocked_size"

    def test_rate_limit_per_recipient(self):
        gate = _gate(mail_rate_limit_per_minute=2, mail_dry_run=True)
        limiter = MemoryRateLimiter()
        gate = MailGate(FakePolicy(mail_rate_limit_per_minute=2), limiter=limiter)
        assert gate.evaluate(to=["ops@example.com"]).code == "dry_run"
        assert gate.evaluate(to=["ops@example.com"]).code == "dry_run"
        third = gate.evaluate(to=["ops@example.com"])
        assert third.code == "blocked_ratelimit"
        # 别人不受影响 —— 限流按收件人分开计数
        assert gate.evaluate(to=["other@example.com"]).code == "dry_run"
        del limiter


class TestGateAllows:
    def test_dry_run_passes_policy_then_stops(self):
        gate = _gate(mail_dry_run=True, mail_can_really_send=False)
        d = gate.evaluate(to=["ops@example.com"], cc=["boss@example.com"])
        assert d.code == "dry_run"
        assert d.allowed
        # 策略检查必须**跑完**才判 dry_run，否则开发期测不出限流和黑名单
        assert "allowlist" in d.checks
        assert "attachment" in d.checks
        assert "rate_limit" in d.checks

    def test_dry_run_does_not_bypass_blocking(self):
        """dry_run 不是"跳过检查"，黑名单照样拦。"""
        d = _gate(mail_dry_run=True, mail_denylist=("@evil.com",)).evaluate(to=["a@evil.com"])
        assert d.code == "blocked_denylist"
        assert d.to == ("a@evil.com",)

    def test_allowed_when_everything_configured(self):
        d = _gate(mail_dry_run=False, mail_can_really_send=True).evaluate(to=["ops@example.com"])
        assert d.code == "allowed"
        assert d.allowed

    def test_wildcard_allowlist(self):
        d = _gate(mail_allowlist=("*",), mail_dry_run=False, mail_can_really_send=True).evaluate(
            to=["anyone@anywhere.io"]
        )
        assert d.code == "allowed"

    def test_denylist_glob(self):
        d = _gate(mail_denylist=("*@gmail.com",)).evaluate(to=["x@gmail.com"])
        assert d.code == "blocked_denylist"


class TestAddressHandling:
    def test_display_name_is_unwrapped(self):
        """``张三 <a@b.com>`` 必须取出 ``a@b.com``。

        不处理的话，白名单里明明写了这个地址却匹配不上 ——
        这类问题排查起来极其痛苦。
        """
        d = _gate(mail_allowlist=("a@example.com",)).evaluate(to=["张三 <a@example.com>"])
        assert d.code == "dry_run"
        assert d.to == ("a@example.com",)

    def test_cross_field_dedupe(self):
        """同一地址既在 To 又在 Cc → 只算一个收件人。

        不跨字段去重的话收件人上限会被虚高的数字触发，
        运维看到"10 个收件人超限"，实际只有 6 个人。
        """
        to_c, cc_c, bcc_c = split_fields(["a@x.com", "b@x.com"], ["B@X.com", "c@x.com"], ["d@x.com"])
        assert to_c == ("a@x.com", "b@x.com")
        assert cc_c == ("c@x.com",)
        assert bcc_c == ("d@x.com",)

    def test_dedupe_does_not_trigger_recipient_limit(self):
        d = _gate(mail_max_recipients=2).evaluate(to=["a@example.com", "A@example.com"], cc=["a@example.com"])
        assert d.code == "dry_run"


# ===========================================================================
# 2. 投递后端
# ===========================================================================
class TestSender:
    def _msg(self, **kw) -> OutboundMessage:  # noqa: ANN003
        base = dict(
            from_addr="agent@fashionops.local",
            to=("ops@example.com",),
            subject="测试",
            body_text="正文",
        )
        base.update(kw)
        return OutboundMessage(**base)  # type: ignore[arg-type]

    def test_smtp_headers_exclude_bcc(self):
        """BCC 绝不能出现在头部，否则密送就废了。

        功能看起来还正常，但收件人列表里所有人都能看到本该隐藏的地址 ——
        这是个很难被发现的错误。
        """
        sender = SmtpSender(
            host="smtp.example.com",
            port=465,
            user="u",
            password="p",
            from_addr="agent@fashionops.local",
        )
        msg = sender.build(
            self._msg(
                to=("ops@example.com",),
                cc=("boss@example.com",),
                bcc=("secret@example.com",),
            )
        )
        assert msg["To"] == "ops@example.com"
        assert "boss@example.com" in str(msg["Cc"])
        assert msg["Bcc"] is None
        assert "secret@example.com" not in msg.as_string()
        # 但投递名单里必须有它
        assert "secret@example.com" in self._msg(to=("ops@example.com",), bcc=("secret@example.com",)).all_recipients

    def test_smtp_omits_empty_cc_header(self):
        """空收件人不能设头部 —— 会产出非法头部。"""
        sender = SmtpSender(host="h", port=465, user="", password="", from_addr="a@b.com")
        msg = sender.build(self._msg())
        assert msg["Cc"] is None

    def test_smtp_attaches_bytes(self):
        sender = SmtpSender(host="h", port=465, user="", password="", from_addr="a@b.com")
        msg = sender.build(self._msg(attachments=(("报表.xlsx", b"binary-content"),)))
        assert msg.is_multipart()
        payloads = [p.get_payload(decode=True) for p in msg.walk() if p.get_filename()]
        assert b"binary-content" in payloads

    def test_dry_run_sender_records_without_sending(self):
        sender = DryRunSender()
        result = sender.send(self._msg())
        assert result.ok and result.backend == "dry_run"
        assert len(sender.sent) == 1

    def test_dry_run_sender_never_touches_network(self):
        """DryRunSender 不该 import 到 smtplib 的任何连接路径 ——
        它连 host 都没有，物理上不可能发出去。"""
        assert not hasattr(DryRunSender(), "host")


# ===========================================================================
# 3. 端到端：工具 + 节点
# ===========================================================================
@pytest.fixture
def seeded_env(tmp_path, monkeypatch):
    """临时库 + 临时存储，并放开邮件策略（否则一切都会被 fail-closed 拦掉）。"""
    import fashionops.storage as storage_mod
    import fashionops.tools.mail as mail_tool
    from fashionops.config import Settings
    from fashionops.db import base as db_base

    settings = Settings(data_dir=tmp_path, db_host=None)
    reset_engine()
    monkeypatch.setattr(db_base, "get_settings", lambda: settings)
    monkeypatch.setattr(storage_mod, "get_settings", lambda: settings)
    storage_mod.reset_storage()

    engine = build_engine(settings)
    Base.metadata.create_all(engine)

    # 邮件策略：开启、dry_run、白名单放行 example.com
    monkeypatch.setattr(
        mail_tool,
        "get_settings",
        lambda: Settings(
            data_dir=tmp_path,
            db_host=None,
            mail_enabled=True,
            mail_dry_run=True,
            mail_send_allowlist="@example.com",
            mail_from="agent@fashionops.local",
        ),
    )
    # 限流器换成进程内的：默认会连真 Redis，键是全局的 `mail:<地址>`，
    # 于是同一个地址跨用例/跨次运行累计计数，第二个用例就莫名被限流。
    # 这是仓库里已经记过的坑 —— 测试造的外部状态必须自己能收干净。
    monkeypatch.setattr(mail_tool, "get_rate_limiter", lambda _backend: MemoryRateLimiter())

    from fashionops.storage import get_storage

    with Session(engine) as session:
        session.add(
            Asset(
                asset_no="AS2609120001",
                asset_type="spreadsheet",
                name="经营分析.xlsx",
                storage_backend="local",
                storage_path="generated/20260912/report.xlsx",
                mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                size_bytes=7647,
                source="generated",
                generator="analytics",
                review_status="approved",
                created_at=utcnow(),
            )
        )
        session.commit()
    get_storage().save("generated/20260912/report.xlsx", b"fake-xlsx-bytes")

    yield settings
    reset_engine()
    storage_mod.reset_storage()


def _build_context(*, model=None, scopes=None):  # noqa: ANN001
    registry = ToolRegistry()
    register_all_tools(registry)
    audit = MemoryAuditSink()
    gateway = ToolGateway(
        registry,
        rate_limiter=MemoryRateLimiter(),
        breaker=MemoryCircuitBreaker(),
        audit_sink=audit,
        idempotency=IdempotencyStore(use_redis=False),
        rate_limit_per_minute=100,
    )
    ctx = RunContext(
        gateway=gateway,
        model=model or MockModelClient(),
        actor="notification_agent",
        scopes=AGENT_SCOPES["notification"] if scopes is None else scopes,
    )
    return ctx, audit


def _call(ctx: RunContext, tool: str, args: dict, scopes=None):  # noqa: ANN001
    return ctx.gateway.call(
        tool,
        args,
        ctx=CallContext(
            actor="test",
            scopes=AGENT_SCOPES["notification"] if scopes is None else scopes,
        ),
    )


def _analytics_findings() -> dict:
    return {
        "window_days": 30,
        "totals": {
            "gmv": 154109.75,
            "order_count": 389,
            "avg_order_value": 396.17,
            "refund_amount": 19710.42,
        },
        "by_category": [
            {
                "group": "女装",
                "gmv": 44910.37,
                "order_count": 143,
                "avg_order_value": 314.06,
                "return_rate": 0.1469,
                "refund_count": 21,
            }
        ],
        "by_channel": [],
        "alerts": [
            {
                "code": "return_rate",
                "level": "critical",
                "dimension": "直播",
                "metric": "退货率",
                "value": 0.4,
                "threshold": 0.3,
                "message": "直播渠道退货率 40.0%，超过 30.0% 红线",
            }
        ],
        "critical_count": 1,
        "summary": "近 30 天 GMV 154109.75 元，退款金额 19710.42 元。",
        "insights": [],
        "data_quality_notes": [],
        "report": {"asset_no": "AS2609120001", "filename": "经营分析.xlsx"},
    }


class TestMailTool:
    def test_dry_run_records_draft(self, seeded_env):
        ctx, _ = _build_context()
        result = _call(
            ctx,
            "mail.send",
            {
                "to_addrs": ["ops@example.com"],
                "subject": "【日报】昨日经营数据",
                "body_text": "GMV 154109.75 元。",
                "attachment_asset_nos": ["AS2609120001"],
                "task_id": None,
                "purpose": "测试",
            },
        )
        assert result.ok, result.error
        assert result.data["decision"] == "dry_run"
        assert result.data["status"] == "draft"
        assert result.data["sent"] is False
        assert result.data["attachment_count"] == 1
        assert result.data["message_no"].startswith("EM")

    def test_blocked_mail_is_still_recorded(self, seeded_env):
        """**被拦下的邮件必须落库。**

        只记"发成功的"会让被拦的完全不可见：运维看到"这周只发了 12 封"，
        却不知道还有 8 封被策略挡住而业务方以为已经通知了。
        """
        ctx, _ = _build_context()
        result = _call(
            ctx,
            "mail.send",
            {
                "to_addrs": ["someone@gmail.com"],  # 不在白名单
                "subject": "【日报】",
                "body_text": "正文",
            },
        )
        assert result.ok, result.error
        assert result.data["decision"] == "blocked_whitelist"
        assert result.data["status"] == "blocked"
        assert result.data["sent"] is False
        assert result.data["hint"]

        with Session(get_engine()) as session:
            row = session.query(EmailMessage).filter_by(message_no=result.data["message_no"]).one()
        assert row.status == "blocked"
        assert row.gate_decision == "blocked_whitelist"
        assert row.gate_reason
        assert row.sent_at is None

    def test_missing_asset_is_reported(self, seeded_env):
        ctx, _ = _build_context()
        result = _call(
            ctx,
            "mail.send",
            {
                "to_addrs": ["ops@example.com"],
                "subject": "x",
                "body_text": "y",
                "attachment_asset_nos": ["AS9999999999"],
            },
        )
        assert result.ok, result.error
        assert result.data["missing_assets"] == ["AS9999999999"]
        assert result.data["attachment_count"] == 0

    def test_policy_tool_lists_rules(self, seeded_env):
        ctx, _ = _build_context()
        result = _call(ctx, "mail.policy", {})
        assert result.ok, result.error
        assert result.data["enabled"] is True
        assert result.data["allowlist"] == ["@example.com"]

    def test_requires_mail_scope(self, seeded_env):
        ctx, _ = _build_context()
        result = _call(
            ctx, "mail.send", {"to_addrs": ["a@example.com"], "subject": "s", "body_text": "b"}, scopes=frozenset()
        )  # noqa: E501
        assert not result.ok

    def test_idempotent_on_rerun(self, seeded_env):
        """节点重跑不该把同一封信发第二次。"""
        ctx, _ = _build_context()
        args = {"to_addrs": ["ops@example.com"], "subject": "s", "body_text": "b", "task_id": None}
        call_ctx = CallContext(actor="test", scopes=AGENT_SCOPES["notification"], idempotency_key="T1:notification")
        first = ctx.gateway.call("mail.send", args, ctx=call_ctx)
        second = ctx.gateway.call("mail.send", args, ctx=call_ctx)
        assert first.data["message_no"] == second.data["message_no"]

        with Session(get_engine()) as session:
            assert session.query(EmailMessage).count() == 1
