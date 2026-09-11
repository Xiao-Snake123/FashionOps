"""发信安全闸门测试。

这是整个邮件模块风险最高的部分：闸门失效 = Agent 可以给任意人发任意内容。
因此测试覆盖到每一种绕过路径。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from office_mcp.common.config import Settings
from office_mcp.common.errors import MailPolicyViolation
from office_mcp.email.guard import SendGuard, _extract_address, _matches
from office_mcp.email.models import AttachmentInfo


def make_guard(tmp_path: Path, **overrides: object) -> SendGuard:
    options: dict[str, object] = {
        "workspace_root": tmp_path / "ws",
        "mail_send_allowlist": ("@corp.example.com",),
    }
    options.update(overrides)
    return SendGuard(Settings(**options))  # type: ignore[arg-type]


class TestAddressMatching:
    @pytest.mark.parametrize(
        ("address", "pattern", "expected"),
        [
            ("a@corp.com", "@corp.com", True),
            ("a@corp.com", "@other.com", False),
            ("a@corp.com", "a@corp.com", True),
            ("b@corp.com", "a@corp.com", False),
            ("a@sub.corp.com", "@sub.corp.com", True),
            # 后缀匹配必须锚定在末尾，不能被子串欺骗
            ("a@evil.com.corp.com.attacker.net", "@corp.com", False),
            ("a@corp.com.attacker.net", "@corp.com", False),
            ("anyone@anywhere.com", "*", True),
            ("a@corp.com", "*@corp.com", True),
            ("a@corp.com", "", False),
        ],
    )
    def test_matches(self, address: str, pattern: str, expected: bool) -> None:
        assert _matches(address, pattern) is expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("a@b.com", "a@b.com"),
            ("张三 <a@b.com>", "a@b.com"),
            ("  a@b.com  ", "a@b.com"),
            ("", ""),
        ],
    )
    def test_extract_address(self, raw: str, expected: str) -> None:
        assert _extract_address(raw) == expected


class TestFailClosed:
    def test_empty_allowlist_blocks_everything(self, tmp_path: Path) -> None:
        """最重要的一条：不配白名单 → 谁都发不出去。"""
        guard = make_guard(tmp_path, mail_send_allowlist=())
        with pytest.raises(MailPolicyViolation) as exc:
            guard.check(to=["anyone@anywhere.com"], cc=[], bcc=[], attachments=[])
        assert exc.value.code == "mail_policy_violation"
        assert "白名单" in exc.value.message
        # 必须告诉模型替代方案，否则它会反复重试
        assert "草稿" in (exc.value.hint or "")

    def test_send_enabled_requires_allowlist(self, tmp_path: Path) -> None:
        configured = dict(
            mail_address="me@x.com",
            mail_password="p",
            smtp_host="smtp.x.com",
        )
        assert (
            Settings(
                workspace_root=tmp_path / "a", mail_send_allowlist=(), **configured
            ).mail_send_enabled
            is False
        )

        assert (
            Settings(
                workspace_root=tmp_path / "b",
                mail_send_allowlist=("@x.com",),
                **configured,
            ).mail_send_enabled
            is True
        )


class TestAllowDeny:
    def test_recipient_outside_allowlist_rejected(self, tmp_path: Path) -> None:
        guard = make_guard(tmp_path)
        with pytest.raises(MailPolicyViolation) as exc:
            guard.check(to=["stranger@outside.com"], cc=[], bcc=[], attachments=[])
        assert "不在允许的白名单" in exc.value.message

    def test_recipient_inside_allowlist_passes(self, tmp_path: Path) -> None:
        guard = make_guard(tmp_path)
        result = guard.check(to=["alice@corp.example.com"], cc=[], bcc=[], attachments=[])
        assert result == ["alice@corp.example.com"]

    def test_denylist_wins_over_allowlist(self, tmp_path: Path) -> None:
        """即使命中白名单，命中黑名单也要拒绝。"""
        guard = make_guard(
            tmp_path,
            mail_send_denylist=("bad@corp.example.com",),
        )
        with pytest.raises(MailPolicyViolation) as exc:
            guard.check(to=["bad@corp.example.com"], cc=[], bcc=[], attachments=[])
        assert "黑名单" in exc.value.message

    def test_cc_and_bcc_are_also_checked(self, tmp_path: Path) -> None:
        """不能只在 to 上做校验 —— 攻击者可以把目标塞进 cc/bcc。"""
        guard = make_guard(tmp_path)
        with pytest.raises(MailPolicyViolation):
            guard.check(to=["ok@corp.example.com"], cc=["evil@outside.com"], bcc=[], attachments=[])
        with pytest.raises(MailPolicyViolation):
            guard.check(to=["ok@corp.example.com"], cc=[], bcc=["evil@outside.com"], attachments=[])

    def test_case_insensitive(self, tmp_path: Path) -> None:
        guard = make_guard(tmp_path, mail_send_allowlist=("@Corp.Example.COM",))
        assert guard.check(to=["A@corp.example.com"], cc=[], bcc=[], attachments=[])

    def test_deduplicates_recipients(self, tmp_path: Path) -> None:
        guard = make_guard(tmp_path)
        result = guard.check(
            to=["a@corp.example.com", "A@CORP.EXAMPLE.COM"],
            cc=["a@corp.example.com"],
            bcc=[],
            attachments=[],
        )
        assert result == ["a@corp.example.com"]

    def test_no_recipients_rejected(self, tmp_path: Path) -> None:
        guard = make_guard(tmp_path)
        with pytest.raises(MailPolicyViolation):
            guard.check(to=["", "  "], cc=[], bcc=[], attachments=[])


class TestRecipientLimit:
    def test_too_many_recipients_rejected(self, tmp_path: Path) -> None:
        guard = make_guard(tmp_path, mail_max_recipients=2)
        with pytest.raises(MailPolicyViolation) as exc:
            guard.check(
                to=[f"u{i}@corp.example.com" for i in range(5)],
                cc=[],
                bcc=[],
                attachments=[],
            )
        assert "收件人过多" in exc.value.message


class TestAttachmentPolicy:
    def test_disallowed_extension_rejected(self, tmp_path: Path) -> None:
        guard = make_guard(tmp_path)
        with pytest.raises(MailPolicyViolation) as exc:
            guard.check(
                to=["a@corp.example.com"],
                cc=[],
                bcc=[],
                attachments=[AttachmentInfo(filename="id_rsa", size_bytes=100)],
            )
        assert "附件类型" in exc.value.message

    def test_env_file_rejected(self, tmp_path: Path) -> None:
        """`.env` 这类敏感文件绝不能通过附件外发。"""
        guard = make_guard(tmp_path)
        with pytest.raises(MailPolicyViolation):
            guard.check(
                to=["a@corp.example.com"],
                cc=[],
                bcc=[],
                attachments=[AttachmentInfo(filename=".env", size_bytes=10)],
            )

    def test_oversized_attachment_rejected(self, tmp_path: Path) -> None:
        guard = make_guard(tmp_path, mail_max_attachment_bytes=1000)
        with pytest.raises(MailPolicyViolation) as exc:
            guard.check(
                to=["a@corp.example.com"],
                cc=[],
                bcc=[],
                attachments=[AttachmentInfo(filename="big.pdf", size_bytes=5000)],
            )
        assert "过大" in exc.value.message

    def test_allowed_attachment_passes(self, tmp_path: Path) -> None:
        guard = make_guard(tmp_path)
        guard.check(
            to=["a@corp.example.com"],
            cc=[],
            bcc=[],
            attachments=[AttachmentInfo(filename="周报.docx", size_bytes=2048)],
        )


class TestRateLimit:
    def test_exceeding_rate_limit_is_blocked(self, tmp_path: Path) -> None:
        guard = make_guard(tmp_path, mail_rate_limit_per_minute=3)
        for _ in range(3):
            guard.check(to=["a@corp.example.com"], cc=[], bcc=[], attachments=[])
            guard.record_attempt()

        with pytest.raises(MailPolicyViolation) as exc:
            guard.check(to=["a@corp.example.com"], cc=[], bcc=[], attachments=[])
        assert "过于频繁" in exc.value.message
        assert "秒后重试" in (exc.value.hint or "")

    def test_checks_do_not_count_as_sent(self, tmp_path: Path) -> None:
        """只校验不发送，不应该消耗配额，否则模型探索几次就把自己锁死了。"""
        guard = make_guard(tmp_path, mail_rate_limit_per_minute=2)
        for _ in range(10):
            guard.check(to=["a@corp.example.com"], cc=[], bcc=[], attachments=[])

    def test_reset_clears_counter(self, tmp_path: Path) -> None:
        guard = make_guard(tmp_path, mail_rate_limit_per_minute=1)
        guard.check(to=["a@corp.example.com"], cc=[], bcc=[], attachments=[])
        guard.record_attempt()
        guard.reset()
        guard.check(to=["a@corp.example.com"], cc=[], bcc=[], attachments=[])


class TestStatus:
    def test_status_exposes_policy_without_secrets(self, tmp_path: Path) -> None:
        guard = make_guard(tmp_path, mail_address="me@corp.example.com", mail_password="s3cret")
        status = guard.status
        assert status["allowlist"] == ["@corp.example.com"]
        assert status["dry_run"] is False
        assert "s3cret" not in str(status)
        assert "password" not in status
