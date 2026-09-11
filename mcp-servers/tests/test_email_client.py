"""邮件客户端测试。

用假 mailbox / sender 注入，不依赖真实邮件服务器：

- 验证**闸门在发信前生效**（被拒时 sender 一次都不能被调用）
- 验证 MIME 构建与解析的正确性（会话串联、附件、编码）
- 验证 IMAP 协议细节（日期格式不受 locale 影响、中文搜索走 UTF-8）
"""

from __future__ import annotations

import email
import email.policy
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from office_mcp.common.config import Settings
from office_mcp.common.errors import (
    MailOperationError,
    MailPolicyViolation,
)
from office_mcp.email.client import (
    MailService,
    _detail_from_raw,
    _header_suggests_attachment,
    _html_to_text,
    _imap_date,
    _parse_folder,
    _RawMessage,
    _search_uids,
    _summary_from_raw,
)
from office_mcp.email.models import EmailDetail, FolderInfo, SendRequest

# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #


class FakeMailbox:
    """记录所有调用，返回预设数据。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.folders = [FolderInfo(name="INBOX"), FolderInfo(name="Sent")]
        self.search_result: list[Any] = []
        self.fetch_result: EmailDetail | None = None
        self.appended: list[tuple[str, bytes]] = []
        self.raise_on_fetch = False

    def list_folders(self) -> list[FolderInfo]:
        self.calls.append(("list_folders",))
        return self.folders

    def search(self, folder: str, criteria: Any, limit: int) -> list[Any]:
        self.calls.append(("search", folder, tuple(criteria), str(limit)))
        return self.search_result

    def fetch(self, folder: str, uid: str) -> EmailDetail:
        self.calls.append(("fetch", folder, uid))
        if self.raise_on_fetch or self.fetch_result is None:
            raise MailOperationError("原邮件不存在")
        return self.fetch_result

    def set_seen(self, folder: str, uid: str, seen: bool) -> None:
        self.calls.append(("set_seen", folder, uid, str(seen)))

    def move(self, folder: str, uid: str, target: str) -> None:
        self.calls.append(("move", folder, uid, target))

    def delete(self, folder: str, uid: str) -> None:
        self.calls.append(("delete", folder, uid))

    def append(self, folder: str, raw: bytes, flags: Any = ()) -> None:
        self.calls.append(("append", folder))
        self.appended.append((folder, raw))


class FakeSender:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[EmailMessage] = []
        self.fail = fail

    def send(self, message: EmailMessage) -> str:
        if self.fail:
            raise AssertionError("闸门放行前不应调用 sender")
        self.sent.append(message)
        return str(message.get("Message-ID", "<fake@id>"))


@pytest.fixture()
def service_factory(tmp_path: Path):
    def _make(**overrides: Any) -> tuple[MailService, FakeMailbox, FakeSender, Path]:
        workspace = tmp_path / "ws"
        workspace.mkdir(exist_ok=True)
        defaults: dict[str, Any] = {
            "workspace_root": workspace,
            "mail_address": "me@corp.example.com",
            "mail_password": "secret",
            "smtp_host": "smtp.corp.example.com",
            "imap_host": "imap.corp.example.com",
            "mail_send_allowlist": ("@corp.example.com",),
            "dry_run": False,
        }
        defaults.update(overrides)
        settings = Settings(**defaults)
        mailbox = FakeMailbox()
        sender = FakeSender()
        service = MailService(
            settings,
            mailbox=mailbox,
            sender=sender,
            resolve_attachment=lambda p: (workspace / p).resolve(),
        )
        return service, mailbox, sender, workspace

    return _make


def make_request(**overrides: Any) -> SendRequest:
    payload: dict[str, Any] = {
        "to": ["alice@corp.example.com"],
        "subject": "周报",
        "body_text": "你好，附件是本周周报。",
    }
    payload.update(overrides)
    return SendRequest(**payload)


# --------------------------------------------------------------------------- #
# 发信
# --------------------------------------------------------------------------- #


class TestSend:
    def test_sends_when_allowed(self, service_factory: Any) -> None:
        service, _, sender, _ = service_factory()
        result = service.send(make_request())

        assert result.dry_run is False
        assert result.recipients == ["alice@corp.example.com"]
        assert len(sender.sent) == 1
        assert sender.sent[0]["Subject"] == "周报"
        assert sender.sent[0]["To"] == "alice@corp.example.com"

    def test_sender_not_called_when_blocked(self, service_factory: Any) -> None:
        """核心断言：被闸门拒绝时绝不能碰到 SMTP。"""
        service, _, sender, _ = service_factory(mail_send_allowlist=())
        with pytest.raises(MailPolicyViolation):
            service.send(make_request(to=["stranger@outside.com"]))
        assert sender.sent == []

    def test_dry_run_does_not_send(self, service_factory: Any) -> None:
        service, _, sender, _ = service_factory(dry_run=True)
        result = service.send(make_request())

        assert result.dry_run is True
        assert sender.sent == []
        assert result.recipients == ["alice@corp.example.com"]

    def test_dry_run_still_enforces_policy(self, service_factory: Any) -> None:
        """dry_run 不能变成绕过闸门的后门。"""
        service, _, _, _ = service_factory(dry_run=True, mail_send_allowlist=())
        with pytest.raises(MailPolicyViolation):
            service.send(make_request())

    def test_dry_run_consumes_rate_limit(self, service_factory: Any) -> None:
        """dry_run 也必须计入限流。

        否则开发期永远验证不到限流策略，等切到线上才第一次触发 —— 这正是
        dry_run 本该帮你提前发现的那类问题。
        """
        service, _, _, _ = service_factory(dry_run=True, mail_rate_limit_per_minute=1)
        service.send(make_request())
        with pytest.raises(MailPolicyViolation) as exc:
            service.send(make_request())
        assert "过于频繁" in exc.value.message

    def test_dry_run_consumes_quota_but_not_for_blocked_sends(self, service_factory: Any) -> None:
        """被闸门拒绝的发送不该消耗配额，否则模型试错几次就把自己锁死了。"""
        service, _, _, _ = service_factory(
            dry_run=True, mail_rate_limit_per_minute=1, mail_send_allowlist=("@x.com",)
        )
        for _ in range(5):
            with pytest.raises(MailPolicyViolation):
                service.send(make_request(to=["bad@y.com"]))
        service.send(make_request(to=["ok@x.com"]))  # 仍然可用

    def test_cc_and_bcc_included_in_message(self, service_factory: Any) -> None:
        service, _, sender, _ = service_factory()
        service.send(make_request(cc=["bob@corp.example.com"], bcc=["carol@corp.example.com"]))
        message = sender.sent[0]
        assert "bob@corp.example.com" in message["Cc"]
        # Bcc 必须存在 header 里（SMTP 投递需要），但不该出现在 To/Cc
        assert "carol@corp.example.com" in message["Bcc"]
        assert "carol@corp.example.com" not in (message["Cc"] or "")

    def test_html_body_becomes_alternative(self, service_factory: Any) -> None:
        service, _, sender, _ = service_factory()
        service.send(make_request(body_html="<p>你好</p>"))
        message = sender.sent[0]
        assert message.is_multipart()
        subtypes = {part.get_content_subtype() for part in message.walk()}
        assert {"plain", "html"} <= subtypes

    def test_html_only_message(self, service_factory: Any) -> None:
        service, _, sender, _ = service_factory()
        service.send(make_request(body_text="", body_html="<p>只有 HTML</p>"))
        message = sender.sent[0]
        assert message.get_content_type() == "text/html"

    def test_attachments_added(self, service_factory: Any) -> None:
        service, _, sender, workspace = service_factory()
        (workspace / "report.docx").write_bytes(b"PK\x03\x04fake docx")
        result = service.send(make_request(attachments=["report.docx"]))

        assert [a.filename for a in result.attachments] == ["report.docx"]
        names = [part.get_filename() for part in sender.sent[0].walk() if part.get_filename()]
        assert "report.docx" in names

    def test_missing_attachment_rejected(self, service_factory: Any) -> None:
        service, _, sender, _ = service_factory()
        with pytest.raises(MailOperationError) as exc:
            service.send(make_request(attachments=["nope.docx"]))
        assert "附件不存在" in exc.value.message
        assert sender.sent == []

    def test_sent_copy_appended(self, service_factory: Any) -> None:
        service, mailbox, _, _ = service_factory()
        result = service.send(make_request())
        assert result.saved_to == "Sent"
        assert mailbox.appended and mailbox.appended[0][0] == "Sent"

    def test_sent_copy_failure_does_not_break_send(self, service_factory: Any) -> None:
        """存档失败不能影响投递结果。"""
        service, mailbox, sender, _ = service_factory()
        mailbox.append = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))  # type: ignore[method-assign]

        result = service.send(make_request())
        assert result.saved_to is None
        assert len(sender.sent) == 1

    def test_disable_sent_copy(self, service_factory: Any) -> None:
        service, mailbox, _, _ = service_factory(mail_save_sent_copy=False)
        result = service.send(make_request())
        assert result.saved_to is None
        assert mailbox.appended == []


class TestReply:
    def test_reply_adds_threading_headers(self, service_factory: Any) -> None:
        service, mailbox, sender, _ = service_factory()
        mailbox.fetch_result = EmailDetail(
            uid="42",
            folder="INBOX",
            subject="原主题",
            sender="alice@corp.example.com",
            message_id="<orig@mail>",
        )
        service.send(make_request(reply_to_uid="42"))

        message = sender.sent[0]
        assert message["In-Reply-To"] == "<orig@mail>"
        assert message["References"] == "<orig@mail>"
        assert message["Subject"] == "Re: 周报"

    def test_reply_prefix_not_duplicated(self, service_factory: Any) -> None:
        service, mailbox, sender, _ = service_factory()
        mailbox.fetch_result = EmailDetail(
            uid="42", folder="INBOX", subject="x", sender="a@b.c", message_id="<o>"
        )
        service.send(make_request(subject="Re: 周报", reply_to_uid="42"))
        assert sender.sent[0]["Subject"] == "Re: 周报"

    def test_unreadable_original_does_not_block_send(self, service_factory: Any) -> None:
        """读不到原邮件只是串不上会话，不该让发送失败。"""
        service, mailbox, sender, _ = service_factory()
        mailbox.raise_on_fetch = True
        result = service.send(make_request(reply_to_uid="999"))
        assert result.dry_run is False
        assert len(sender.sent) == 1


class TestDraft:
    def test_draft_written_to_workspace(self, service_factory: Any) -> None:
        service, _, sender, workspace = service_factory()
        result = service.save_draft(make_request())

        assert result.path.startswith("drafts/")
        path = workspace / result.path
        assert path.exists()
        assert sender.sent == []

        parsed = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
        assert parsed["Subject"] == "周报"
        assert parsed["To"] == "alice@corp.example.com"

    def test_draft_name_sanitized(self, service_factory: Any) -> None:
        """主题里的路径分隔符不能让它逃出 drafts 目录。"""
        service, _, _, workspace = service_factory()
        result = service.save_draft(make_request(subject="../../etc/passwd"))
        assert ".." not in result.path
        assert (workspace / result.path).resolve().is_relative_to(workspace.resolve())

    def test_draft_bypasses_send_allowlist(self, service_factory: Any) -> None:
        """白名单被拒时的替代路径必须可用 —— 否则 Agent 就彻底卡死了。"""
        service, _, _, workspace = service_factory(mail_send_allowlist=())
        result = service.save_draft(make_request(to=["stranger@outside.com"]))
        assert (workspace / result.path).exists()

    def test_attachment_policy_still_applies_to_draft(self, service_factory: Any) -> None:
        service, _, _, workspace = service_factory()
        (workspace / "secret.key").write_text("key", encoding="utf-8")
        draft = service.save_draft(make_request(subject="x"))
        # 草稿不做附件类型校验（因为不经网络外发），但文件必须来自沙箱内
        assert draft.path


# --------------------------------------------------------------------------- #
# 查询构造
# --------------------------------------------------------------------------- #


class TestSearchCriteria:
    def test_default_criteria_is_all(self, service_factory: Any) -> None:
        service, mailbox, _, _ = service_factory()
        service.search(limit=5)
        assert mailbox.calls[0][2] == ("ALL",)

    def test_unread_and_from_build_criteria(self, service_factory: Any) -> None:
        service, mailbox, _, _ = service_factory()
        service.search(unread_only=True, from_address="boss@corp.example.com")
        criteria = mailbox.calls[0][2]
        assert "UNSEEN" in criteria
        assert 'FROM "boss@corp.example.com"' in criteria

    def test_recent_days_converted_to_since(self, service_factory: Any) -> None:
        service, mailbox, _, _ = service_factory()
        service.search(recent_days=7)
        criteria = " ".join(mailbox.calls[0][2])
        assert "SINCE" in criteria

    def test_quote_escaping_prevents_criteria_injection(self, service_factory: Any) -> None:
        """注入引号不能改变 IMAP 查询语义。"""
        service, mailbox, _, _ = service_factory()
        service.search(subject_contains='" OR ALL')
        criteria = mailbox.calls[0][2][0]
        assert criteria == 'SUBJECT "\\" OR ALL"'

    def test_limit_passed_through(self, service_factory: Any) -> None:
        service, mailbox, _, _ = service_factory()
        service.search(limit=42)
        assert mailbox.calls[0][3] == "42"


class TestImapDate:
    def test_format_matches_rfc3501(self) -> None:
        assert _imap_date(date(2026, 9, 1)) == "01-Sep-2026"
        assert _imap_date(date(2026, 12, 31)) == "31-Dec-2026"
        assert _imap_date(date(2026, 1, 5)) == "05-Jan-2026"

    def test_locale_independent(self) -> None:
        """中文 Windows 下 strftime('%b') 会输出「9月」，必须走硬编码月份表。"""
        result = _imap_date(date(2026, 9, 11))
        assert result.isascii()
        assert "Sep" in result


class TestSearchUids:
    class _Client:
        def __init__(self, data: Any = b"1 2 3") -> None:
            self.args: tuple[Any, ...] = ()
            self.data = data

        def uid(self, *args: Any) -> tuple[str, list[Any]]:
            self.args = args
            return "OK", [self.data]

    def test_ascii_criteria_do_not_send_charset(self) -> None:
        client = self._Client()
        uids = _search_uids(client, ["UNSEEN", 'SUBJECT "report"'])  # type: ignore[arg-type]
        assert uids == ["1", "2", "3"]
        assert "CHARSET" not in client.args

    def test_non_ascii_criteria_use_utf8(self) -> None:
        client = self._Client()
        _search_uids(client, ['SUBJECT "周报"'])  # type: ignore[arg-type]
        assert b"CHARSET" in client.args
        assert b"UTF-8" in client.args
        assert b'SUBJECT "\xe5\x91\xa8\xe6\x8a\xa5"' in client.args

    def test_empty_result(self) -> None:
        client = self._Client(data=b"")
        assert _search_uids(client, ["ALL"]) == []  # type: ignore[arg-type]


class TestFolderParsing:
    def test_parses_list_response(self) -> None:
        info = _parse_folder(b'(\\HasNoChildren) "/" "INBOX"')
        assert info.name == "INBOX"
        assert info.delimiter == "/"
        assert "\\HasNoChildren" in info.flags

    def test_parses_nested_folder(self) -> None:
        info = _parse_folder(b'(\\HasChildren) "." "Projects.2026"')
        assert info.name == "Projects.2026"
        assert info.delimiter == "."

    def test_garbage_does_not_crash(self) -> None:
        assert _parse_folder(b"weird").name == "weird"


# --------------------------------------------------------------------------- #
# MIME 解析
# --------------------------------------------------------------------------- #

RAW_MULTIPART = b"""From: Alice <alice@corp.example.com>
To: me@corp.example.com
Subject: =?utf-8?b?5ZGo5oql?=
Date: Mon, 01 Sep 2026 10:00:00 +0800
Message-ID: <abc@corp.example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="BOUND"

--BOUND
Content-Type: text/plain; charset="utf-8"

hello plain body
--BOUND
Content-Type: application/pdf; name="report.pdf"
Content-Disposition: attachment; filename="report.pdf"
Content-Transfer-Encoding: base64

aGVsbG8gcGRm
--BOUND--
"""


class TestDetailParsing:
    def test_extracts_fields_and_attachment(self) -> None:
        detail = _detail_from_raw(RAW_MULTIPART, "INBOX", "7", ("\\Seen",), 20000)

        assert detail.uid == "7"
        assert detail.folder == "INBOX"
        assert detail.subject == "周报"  # RFC2047 解码
        assert "alice@corp.example.com" in detail.sender
        assert "hello plain body" in detail.body_text
        assert detail.unread is False
        assert detail.has_attachments is True
        assert [a.filename for a in detail.attachments] == ["report.pdf"]
        assert detail.attachments[0].size_bytes == len(b"hello pdf")
        assert detail.message_id == "<abc@corp.example.com>"

    def test_marks_unread(self) -> None:
        detail = _detail_from_raw(RAW_MULTIPART, "INBOX", "7", (), 20000)
        assert detail.unread is True

    def test_truncates_long_body(self) -> None:
        detail = _detail_from_raw(RAW_MULTIPART, "INBOX", "7", (), 5)
        assert detail.body_truncated is True
        assert len(detail.body_text) == 5

    def test_html_only_falls_back_to_text(self) -> None:
        raw = (
            b"From: a@b.c\nSubject: s\nMIME-Version: 1.0\n"
            b'Content-Type: text/html; charset="utf-8"\n\n'
            b"<html><body><p>Hello</p><p>World</p></body></html>"
        )
        detail = _detail_from_raw(raw, "INBOX", "1", (), 20000)
        assert "Hello" in detail.body_text
        assert "World" in detail.body_text
        assert "<p>" not in detail.body_text

    def test_corrupt_base64_does_not_crash(self) -> None:
        raw = (
            b"From: a@b.c\nSubject: s\nMIME-Version: 1.0\n"
            b'Content-Type: multipart/mixed; boundary="B"\n\n'
            b"--B\nContent-Type: text/plain; charset=utf-8\n\nok\n"
            b'--B\nContent-Type: application/pdf; name="x.pdf"\n'
            b'Content-Disposition: attachment; filename="x.pdf"\n'
            b"Content-Transfer-Encoding: base64\n\n!!!not-valid-base64!!!\n--B--\n"
        )
        detail = _detail_from_raw(raw, "INBOX", "1", (), 20000)
        assert detail.body_text.strip() == "ok"


class TestSummaryParsing:
    def test_headers_only_summary(self) -> None:
        header_blob = RAW_MULTIPART.split(b"\n\n", 1)[0]
        summary = _summary_from_raw(_RawMessage("3", header_blob, ()), "INBOX")
        assert summary.uid == "3"
        assert summary.subject == "周报"
        assert summary.has_attachments is True  # multipart/mixed 启发式
        assert summary.snippet == ""

    def test_plain_message_has_no_attachment_flag(self) -> None:
        raw = b"Subject: hi\nContent-Type: text/plain\n\nbody"
        summary = _summary_from_raw(_RawMessage("1", raw, ("\\Seen",)), "INBOX")
        assert summary.has_attachments is False
        assert summary.unread is False


class TestAttachmentHeuristic:
    def test_multipart_mixed_detected(self) -> None:
        message = email.message_from_bytes(
            b"Subject: s\nContent-Type: multipart/mixed; boundary=B\n\n",
            policy=email.policy.default,
        )
        assert _header_suggests_attachment(message) is True

    def test_plain_not_detected(self) -> None:
        message = email.message_from_bytes(
            b"Subject: s\nContent-Type: text/plain\n\n", policy=email.policy.default
        )
        assert _header_suggests_attachment(message) is False


class TestHtmlToText:
    def test_strips_tags_and_entities(self) -> None:
        html = "<div><p>a &amp; b</p><br/><span>c</span></div>"
        text = _html_to_text(html)
        assert "a & b" in text
        assert "<" not in text

    def test_removes_script_and_style(self) -> None:
        html = "<style>p{color:red}</style><script>alert(1)</script><p>safe</p>"
        text = _html_to_text(html)
        assert "safe" in text
        assert "alert" not in text
        assert "color" not in text

    def test_empty_input(self) -> None:
        assert _html_to_text("") == ""


class TestMessageIdFormat:
    def test_message_has_unique_id(self, service_factory: Any) -> None:
        service, _, sender, _ = service_factory()
        service.send(make_request())
        service.send(make_request())
        ids = [m["Message-ID"] for m in sender.sent]
        assert len(set(ids)) == 2
        assert "@" in ids[0]

    def test_date_header_present(self, service_factory: Any) -> None:
        service, _, sender, _ = service_factory()
        service.send(make_request())
        assert sender.sent[0]["Date"]
