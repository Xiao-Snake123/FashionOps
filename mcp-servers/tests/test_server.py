"""MCP 层集成测试。

验证的不是内部函数，而是**模型实际看到的东西**：
工具 schema、返回值形状、错误码与 hint、以及凭证不外泄。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from office_mcp.common import audit as audit_module
from office_mcp.common import config as config_module
from office_mcp.common import secrets as secrets_module
from office_mcp.docs import server as server_module


@pytest.fixture()
def mcp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把工作区指向临时目录，并清掉所有单例缓存。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    audit_log = tmp_path / "audit.jsonl"

    monkeypatch.setenv("OFFICE_MCP_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("OFFICE_MCP_AUDIT_LOG", str(audit_log))
    monkeypatch.delenv("OFFICE_MCP_DRY_RUN", raising=False)

    config_module.get_settings.cache_clear()
    audit_module.get_audit_logger.cache_clear()
    secrets_module.get_secret_store.cache_clear()

    yield {"workspace": workspace, "audit": audit_log}

    config_module.get_settings.cache_clear()
    audit_module.get_audit_logger.cache_clear()
    secrets_module.get_secret_store.cache_clear()


async def call(tool: str, **kwargs: Any) -> dict[str, Any]:
    result = await server_module.mcp.call_tool(tool, kwargs)
    return json.loads(result.content[0].text)


class TestToolSurface:
    async def test_expected_tools_registered(self, mcp_env: dict) -> None:
        names = {t.name for t in await server_module.mcp.list_tools()}
        assert names == {
            "list_files",
            "read_document",
            "create_word_document",
            "create_excel_workbook",
            "create_presentation",
            "write_text_file",
            "get_workspace_info",
        }

    async def test_every_tool_has_description(self, mcp_env: dict) -> None:
        """描述会直接进模型上下文，不能为空。"""
        for tool in await server_module.mcp.list_tools():
            assert (tool.description or "").strip(), f"{tool.name} 缺少描述"

    async def test_schemas_have_descriptions(self, mcp_env: dict) -> None:
        """参数描述质量决定模型能否正确调用。"""
        with_params = 0
        for tool in await server_module.mcp.list_tools():
            props = (tool.parameters or {}).get("properties") or {}
            if not props:
                continue  # 无参工具（如 get_workspace_info）跳过
            with_params += 1
            for prop_name, prop in props.items():
                has_doc = (
                    prop.get("description")
                    or prop.get("anyOf")
                    or prop.get("$ref")
                    or prop.get("items")
                )
                assert has_doc, f"{tool.name}.{prop_name} 缺少 description"
        assert with_params >= 5, "应有至少 5 个工具带参数"

    async def test_no_credential_params_exposed(self, mcp_env: dict) -> None:
        """任何工具的入参都不得出现凭证字段。"""
        forbidden = {"password", "token", "secret", "api_key", "credential"}
        for tool in await server_module.mcp.list_tools():
            props = set((tool.parameters or {}).get("properties") or {})
            assert not (props & forbidden), f"{tool.name} 暴露了凭证参数"


class TestDocuments:
    async def test_create_and_read_docx(self, mcp_env: dict) -> None:
        created = await call(
            "create_word_document",
            filename="周报.docx",
            title="周报",
            blocks=[{"kind": "paragraph", "text": "内容"}],
        )
        assert created["ok"] is True
        assert created["path"] == "outputs/周报.docx"

        read = await call("read_document", path="outputs/周报.docx")
        assert read["ok"] is True
        assert "周报" in read["markdown"]

    async def test_create_excel(self, mcp_env: dict) -> None:
        created = await call(
            "create_excel_workbook",
            filename="表.xlsx",
            sheets=[{"name": "S1", "rows": [["a", "b"], [1, 2]]}],
        )
        assert created["ok"] is True
        assert created["sheets"] == ["S1"]

        read = await call("read_document", path="outputs/表.xlsx")
        assert "| a | b |" in read["markdown"]

    async def test_create_pptx(self, mcp_env: dict) -> None:
        created = await call(
            "create_presentation",
            filename="汇报.pptx",
            title="汇报",
            slides=[{"title": "第一页", "bullets": ["要点"]}],
        )
        assert created["ok"] is True
        assert created["slide_count"] == 2  # 封面 + 1

    async def test_write_text_and_list(self, mcp_env: dict) -> None:
        written = await call("write_text_file", filename="notes.md", content="# hi")
        assert written["ok"] is True

        listing = await call("list_files", pattern="*.md")
        assert listing["count"] == 1
        assert listing["files"][0]["path"] == "outputs/notes.md"


class TestErrorContract:
    async def test_path_escape_returns_structured_error(self, mcp_env: dict) -> None:
        payload = await call("read_document", path="../../etc/hosts")
        assert payload["ok"] is False
        assert payload["code"] == "path_violation"
        assert payload["hint"]

    async def test_missing_file_has_distinct_code(self, mcp_env: dict) -> None:
        payload = await call("read_document", path="outputs/nope.docx")
        assert payload["ok"] is False
        assert payload["code"] == "file_not_found"
        assert payload["hint"]

    async def test_unsupported_format_code(self, mcp_env: dict) -> None:
        workspace: Path = mcp_env["workspace"]
        (workspace / "x.xyz").write_text("x", encoding="utf-8")
        payload = await call("read_document", path="x.xyz")
        assert payload["ok"] is False
        assert payload["code"] == "unsupported_format"

    async def test_duplicate_file_respects_overwrite_flag(self, mcp_env: dict) -> None:
        first = await call("write_text_file", filename="dup.md", content="a")
        assert first["ok"] is True

        second = await call("write_text_file", filename="dup.md", content="b", overwrite=False)
        assert second["ok"] is False
        assert second["hint"]

    async def test_save_under_outputs_only(self, mcp_env: dict) -> None:
        """文件名里的路径分隔符必须被清洗，产物只能落在 outputs/。"""
        payload = await call(
            "write_text_file",
            filename="../../evil.md",
            content="x",
        )
        assert payload["ok"] is True
        assert payload["path"].startswith("outputs/")
        workspace: Path = mcp_env["workspace"]
        assert (workspace / "outputs").exists()
        assert not (workspace.parent / "evil.md").exists()


class TestDryRun:
    async def test_dry_run_writes_nothing(
        self, mcp_env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OFFICE_MCP_DRY_RUN", "1")
        config_module.get_settings.cache_clear()

        payload = await call("create_word_document", filename="x.docx", title="t", blocks=[])
        assert payload["ok"] is True
        assert payload["dry_run"] is True
        assert not (mcp_env["workspace"] / "outputs" / "x.docx").exists()


class TestInfoAndSecrets:
    async def test_workspace_info_reports_state(self, mcp_env: dict) -> None:
        info = await call("get_workspace_info")
        assert info["ok"] is True
        assert info["dry_run"] is False
        assert ".docx" in info["supported_formats"]
        assert isinstance(info["secrets_configured"], dict)

    async def test_workspace_info_never_leaks_secret_values(
        self, mcp_env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OFFICE_MCP_SECRET_OA_TOKEN", "super-secret-value")
        monkeypatch.setenv("OFFICE_MCP_MAIL_PASSWORD", "mail-secret-value")
        config_module.get_settings.cache_clear()
        secrets_module.get_secret_store.cache_clear()

        raw = json.dumps(await call("get_workspace_info"))
        assert "super-secret-value" not in raw
        assert "mail-secret-value" not in raw
        assert os.environ["OFFICE_MCP_SECRET_OA_TOKEN"] not in raw


class TestAuditIntegration:
    async def test_tool_calls_are_audited(self, mcp_env: dict) -> None:
        await call("write_text_file", filename="a.md", content="x")
        await call("read_document", path="../../etc/hosts")

        lines = mcp_env["audit"].read_text(encoding="utf-8").strip().splitlines()
        records = [json.loads(line) for line in lines]

        tools = {r["tool"]: r for r in records}
        assert tools["write_text_file"]["status"] == "ok"
        assert tools["read_document"]["status"] == "error"
        assert tools["read_document"]["error_type"] == "PathViolationError"
        assert tools["read_document"]["error_code"] == "path_violation"

    async def test_audit_has_timing(self, mcp_env: dict) -> None:
        await call("list_files")
        record = json.loads(mcp_env["audit"].read_text(encoding="utf-8").splitlines()[0])
        assert record["duration_ms"] >= 0
