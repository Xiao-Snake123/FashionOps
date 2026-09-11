"""审计日志测试：脱敏、截断、trace 关联、失败不阻断。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from office_mcp.common.audit import AuditLogger, trace_scope
from office_mcp.common.config import Settings


@pytest.fixture()
def logger_with(tmp_path: Path):
    def _make(**overrides: object) -> tuple[AuditLogger, Path]:
        log_path = tmp_path / "audit.jsonl"
        settings = Settings(
            workspace_root=tmp_path / "ws",
            audit_log=log_path,
            **overrides,  # type: ignore[arg-type]
        )
        return AuditLogger(settings), log_path

    return _make


def read_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class TestAuditLogger:
    def test_disabled_when_no_path(self, tmp_path: Path) -> None:
        logger = AuditLogger(Settings(workspace_root=tmp_path / "ws", audit_log=None))
        assert logger.enabled is False
        logger.log("tool_call", tool="x")  # 不应抛错

    def test_writes_jsonl(self, logger_with) -> None:
        logger, path = logger_with()
        logger.log("tool_call", tool="read_document", status="ok")
        records = read_records(path)
        assert len(records) == 1
        assert records[0]["tool"] == "read_document"
        assert records[0]["status"] == "ok"
        assert "ts" in records[0]

    def test_redacts_sensitive_keys(self, logger_with) -> None:
        logger, path = logger_with()
        logger.log(
            "tool_call",
            tool="send_mail",
            args={"to": "a@b.com", "password": "hunter2", "api_key": "sk-123"},
        )
        args = read_records(path)[0]["args"]
        assert args["password"] == "***"
        assert args["api_key"] == "***"
        assert args["to"] == "a@b.com"  # 非敏感字段保留

    def test_redacts_nested_and_case_insensitive(self, logger_with) -> None:
        logger, path = logger_with()
        logger.log(
            "tool_call",
            tool="x",
            args={"outer": {"Authorization": "Bearer abc", "ok": 1}},
        )
        outer = read_records(path)[0]["args"]["outer"]
        assert outer["Authorization"] == "***"
        assert outer["ok"] == 1

    def test_truncates_long_strings(self, logger_with) -> None:
        logger, path = logger_with()
        logger.log("tool_call", tool="x", content="y" * 5000)
        value = read_records(path)[0]["content"]
        assert len(value) < 600
        assert value.endswith("<truncated>")

    def test_caps_container_size(self, logger_with) -> None:
        logger, path = logger_with()
        logger.log("tool_call", tool="x", items=list(range(500)))
        assert len(read_records(path)[0]["items"]) == 50

    def test_tool_call_context_records_success(self, logger_with) -> None:
        logger, path = logger_with()
        with logger.tool_call("write_docx", path="a.docx") as ctx:
            ctx["status"] = "ok"
        record = read_records(path)[0]
        assert record["tool"] == "write_docx"
        assert record["status"] == "ok"
        assert record["args"]["path"] == "a.docx"
        assert isinstance(record["duration_ms"], float)

    def test_tool_call_records_failure_and_reraises(self, logger_with) -> None:
        logger, path = logger_with()
        with pytest.raises(ValueError), logger.tool_call("boom"):
            raise ValueError("bad")
        record = read_records(path)[0]
        assert record["status"] == "error"
        assert record["error_type"] == "ValueError"

    def test_trace_id_recorded(self, logger_with) -> None:
        logger, path = logger_with()
        with trace_scope("trace-abc"):
            logger.log("tool_call", tool="x")
        assert read_records(path)[0]["trace_id"] == "trace-abc"

    def test_audit_write_failure_does_not_raise(self, tmp_path: Path) -> None:
        """审计写失败不能影响业务。"""
        log_path = tmp_path / "audit.jsonl"
        settings = Settings(workspace_root=tmp_path / "ws", audit_log=log_path)
        logger = AuditLogger(settings)
        # 用一个目录占住日志路径，制造 OSError
        log_path.mkdir(parents=True, exist_ok=True)
        logger.log("tool_call", tool="x")  # 不应抛错
