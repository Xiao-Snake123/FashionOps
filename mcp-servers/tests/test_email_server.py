"""邮件 MCP 服务器集成测试。

验证的是**模型实际看到的东西**：工具 schema、错误码、hint，
以及在未配置真实邮箱时的降级行为。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from office_mcp.common import audit as audit_module
from office_mcp.common import config as config_module
from office_mcp.common import secrets as secrets_module
from office_mcp.email import server as email_server


@pytest.fixture()
def mail_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """重置所有单例并在未配置邮箱的状态下运行。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setenv("OFFICE_MCP_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("OFFICE_MCP_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    for name in (
        "OFFICE_MCP_MAIL_ADDRESS",
        "OFFICE_MCP_MAIL_PASSWORD",
        "OFFICE_MCP_SMTP_HOST",
        "OFFICE_MCP_IMAP_HOST",
        "OFFICE_MCP_MAIL_SEND_ALLOWLIST",
        "OFFICE_MCP_DRY_RUN",
    ):
        monkeypatch.delenv(name, raising=False)

    _clear_caches()
    yield {"workspace": workspace, "audit": tmp_path / "audit.jsonl"}
    _clear_caches()


def _clear_caches() -> None:
    config_module.get_settings.cache_clear()
    audit_module.get_audit_logger.cache_clear()
    secrets_module.get_secret_store.cache_clear()
    email_server.get_service.cache_clear()


async def call(tool: str, **kwargs: Any) -> dict[str, Any]:
    result = await email_server.mcp.call_tool(tool, kwargs)
    return json.loads(result.content[0].text)


class TestToolSurface:
    async def test_expected_tools_registered(self, mail_env: dict) -> None:
        names = {t.name for t in await email_server.mcp.list_tools()}
        assert names == {
            "get_mail_status",
            "list_mail_folders",
            "search_emails",
            "read_email",
            "send_email",
            "save_email_draft",
            "mark_email",
        }

    async def test_every_tool_has_description(self, mail_env: dict) -> None:
        for tool in await email_server.mcp.list_tools():
            assert (tool.description or "").strip(), f"{tool.name} 缺少描述"

    async def test_no_credential_params_exposed(self, mail_env: dict) -> None:
        """邮件工具绝不能要求模型传密码 —— 那会让密钥进入对话历史。"""
        forbidden = {"password", "token", "secret", "api_key", "credential", "auth"}
        for tool in await email_server.mcp.list_tools():
            props = set((tool.parameters or {}).get("properties") or {})
            assert not (props & forbidden), f"{tool.name} 暴露了凭证参数: {props & forbidden}"


class TestStatus:
    async def test_reports_unconfigured_state(self, mail_env: dict) -> None:
        payload = await call("get_mail_status")
        assert payload["ok"] is True
        assert payload["smtp_configured"] is False
        assert payload["imap_configured"] is False
        # 未配白名单 → 明确告知模型发不了信
        assert payload["send_enabled"] is False

    async def test_never_leaks_credentials(
        self, mail_env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OFFICE_MCP_MAIL_PASSWORD", "super-secret-password")
        monkeypatch.setenv("OFFICE_MCP_SECRET_OA_TOKEN", "super-secret-token")
        _clear_caches()

        raw = json.dumps(await call("get_mail_status"))
        assert "super-secret-password" not in raw
        assert "super-secret-token" not in raw

    async def test_policy_visible_to_model(
        self, mail_env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OFFICE_MCP_MAIL_SEND_ALLOWLIST", '["@corp.example.com"]')
        _clear_caches()

        payload = await call("get_mail_status")
        assert payload["policy"]["allowlist"] == ["@corp.example.com"]


class TestSendGateThroughMcp:
    async def test_send_blocked_by_empty_allowlist(self, mail_env: dict) -> None:
        payload = await call(
            "send_email",
            to=["someone@example.com"],
            subject="测试",
            body_text="内容",
        )
        assert payload["ok"] is False
        assert payload["code"] == "mail_policy_violation"
        assert "白名单" in payload["error"]
        # 必须给出替代路径，否则模型会反复重试
        assert "draft" in payload["hint"].lower() or "草稿" in payload["hint"]

    async def test_recipient_outside_allowlist_blocked(
        self, mail_env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OFFICE_MCP_MAIL_SEND_ALLOWLIST", '["@corp.example.com"]')
        _clear_caches()

        payload = await call(
            "send_email",
            to=["stranger@outside.com"],
            subject="测试",
            body_text="内容",
        )
        assert payload["ok"] is False
        assert payload["code"] == "mail_policy_violation"

    async def test_send_reports_upstream_unavailable_when_unconfigured(
        self, mail_env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """白名单配了但 SMTP 没配 → 应报 upstream_unavailable，且给可执行的 hint。"""
        monkeypatch.setenv("OFFICE_MCP_MAIL_SEND_ALLOWLIST", '["@corp.example.com"]')
        _clear_caches()

        payload = await call(
            "send_email",
            to=["alice@corp.example.com"],
            subject="测试",
            body_text="内容",
        )
        assert payload["ok"] is False
        assert payload["code"] == "upstream_unavailable"
        assert "SMTP" in payload["error"] or "SMTP" in payload["hint"]

    async def test_dry_run_succeeds_without_smtp(
        self, mail_env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """dry_run 是让整条链路可离线验证的关键 —— 不连 SMTP 也能跑通。"""
        monkeypatch.setenv("OFFICE_MCP_MAIL_SEND_ALLOWLIST", '["@corp.example.com"]')
        monkeypatch.setenv("OFFICE_MCP_DRY_RUN", "1")
        _clear_caches()

        payload = await call(
            "send_email",
            to=["alice@corp.example.com"],
            subject="测试",
            body_text="内容",
        )
        assert payload["ok"] is True
        assert payload["dry_run"] is True
        assert payload["recipients"] == ["alice@corp.example.com"]

    async def test_attachment_escape_blocked_by_sandbox(
        self, mail_env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """附件路径必须过工作区沙箱，不能靠 ../ 把系统文件当附件发出。"""
        monkeypatch.setenv("OFFICE_MCP_MAIL_SEND_ALLOWLIST", '["@corp.example.com"]')
        monkeypatch.setenv("OFFICE_MCP_DRY_RUN", "1")
        _clear_caches()

        payload = await call(
            "send_email",
            to=["alice@corp.example.com"],
            subject="测试",
            body_text="看附件",
            attachments=["../../../Windows/win.ini"],
        )
        assert payload["ok"] is False
        assert payload["code"] == "path_violation"


class TestDraftPath:
    async def test_draft_saved_without_mail_config(self, mail_env: dict) -> None:
        """完全没配邮箱时，存草稿仍应成功 —— 这是 Agent 的兜底出口。"""
        payload = await call(
            "save_email_draft",
            to=["someone@example.com"],
            subject="无法发送的邮件",
            body_text="先存成草稿。",
        )
        assert payload["ok"] is True
        assert payload["path"].startswith("drafts/")
        assert (mail_env["workspace"] / payload["path"]).exists()

    async def test_draft_is_valid_eml(self, mail_env: dict) -> None:
        import email
        import email.policy

        payload = await call(
            "save_email_draft",
            to=["a@b.com"],
            subject="主题",
            body_text="正文内容",
        )
        raw = (mail_env["workspace"] / payload["path"]).read_bytes()
        message = email.message_from_bytes(raw, policy=email.policy.default)
        assert message["Subject"] == "主题"
        assert "正文内容" in message.get_content()


class TestReadToolsFailGracefully:
    async def test_search_without_imap_returns_hint(self, mail_env: dict) -> None:
        payload = await call("search_emails", folder="INBOX")
        assert payload["ok"] is False
        assert payload["code"] == "upstream_unavailable"
        assert payload["hint"]

    async def test_list_folders_without_imap_returns_hint(self, mail_env: dict) -> None:
        payload = await call("list_mail_folders")
        assert payload["ok"] is False
        assert payload["code"] == "upstream_unavailable"

    async def test_read_email_without_imap(self, mail_env: dict) -> None:
        payload = await call("read_email", folder="INBOX", uid="1")
        assert payload["ok"] is False
        assert payload["code"] == "upstream_unavailable"


class TestInputValidation:
    async def test_invalid_date_format(self, mail_env: dict) -> None:
        payload = await call("search_emails", since="2026/09/01")
        assert payload["ok"] is False
        assert "YYYY-MM-DD" in payload["hint"]

    async def test_invalid_action(self, mail_env: dict) -> None:
        payload = await call("mark_email", folder="INBOX", uid="1", action="explode")
        assert payload["ok"] is False
        assert payload["code"] == "invalid_action"
        assert "mark_read" in payload["hint"]

    async def test_move_requires_target_folder(self, mail_env: dict) -> None:
        payload = await call("mark_email", folder="INBOX", uid="1", action="move")
        assert payload["ok"] is False
        assert payload["code"] == "missing_argument"
        assert "target_folder" in payload["hint"]

    async def test_empty_subject_rejected(self, mail_env: dict) -> None:
        payload = await call("send_email", to=["a@b.com"], subject="   ", body_text="x")
        assert payload["ok"] is False
        # pydantic 校验失败也会被兜底成结构化错误，而不是抛 traceback
        assert payload["code"] in {"internal_error", "mail_policy_violation"}


class TestAudit:
    async def test_policy_violation_is_audited_as_error(self, mail_env: dict) -> None:
        """安全拒绝必须在审计里留下 error 记录，否则风控形同虚设。"""
        await call("send_email", to=["x@y.com"], subject="s", body_text="b")

        records = [
            json.loads(line) for line in mail_env["audit"].read_text(encoding="utf-8").splitlines()
        ]
        entry = next(r for r in records if r["tool"] == "send_email")
        assert entry["status"] == "error"
        assert entry["error_code"] == "mail_policy_violation"
