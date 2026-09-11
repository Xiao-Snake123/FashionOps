"""日历 MCP 服务器集成测试。

重点覆盖：
- 时间与时区的端到端正确性（最容易出错的地方）
- 冲突检测与空闲时间计算在 MCP 层的表现
- ICS 导入导出的往返一致
- 错误码与 hint 是否可执行
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from office_mcp.calendar import server as cal_server
from office_mcp.common import audit as audit_module
from office_mcp.common import config as config_module
from office_mcp.common import secrets as secrets_module


@pytest.fixture()
def cal_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setenv("OFFICE_MCP_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("OFFICE_MCP_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("OFFICE_MCP_TIMEZONE", "Asia/Shanghai")
    monkeypatch.setenv("OFFICE_MCP_MAIL_ADDRESS", "agent@corp.example.com")
    monkeypatch.delenv("OFFICE_MCP_DRY_RUN", raising=False)

    _clear_caches()
    yield {"workspace": workspace, "audit": tmp_path / "audit.jsonl"}
    _clear_caches()


def _clear_caches() -> None:
    config_module.get_settings.cache_clear()
    audit_module.get_audit_logger.cache_clear()
    secrets_module.get_secret_store.cache_clear()
    cal_server.get_store.cache_clear()


async def call(tool: str, **kwargs: Any) -> dict[str, Any]:
    result = await cal_server.mcp.call_tool(tool, kwargs)
    return json.loads(result.content[0].text)


async def create(title: str, start: str, end: str, **extra: Any) -> dict[str, Any]:
    return await call("create_event", title=title, start=start, end=end, **extra)


class TestToolSurface:
    async def test_expected_tools_registered(self, cal_env: dict) -> None:
        names = {t.name for t in await cal_server.mcp.list_tools()}
        assert names == {
            "get_calendar_status",
            "list_calendars",
            "list_events",
            "create_event",
            "update_event",
            "delete_event",
            "find_free_slots",
            "import_ics_file",
            "export_ics_file",
        }

    async def test_every_tool_has_description(self, cal_env: dict) -> None:
        for tool in await cal_server.mcp.list_tools():
            assert (tool.description or "").strip(), f"{tool.name} 缺少描述"

    async def test_no_credential_params_exposed(self, cal_env: dict) -> None:
        forbidden = {"password", "token", "secret", "api_key", "credential"}
        for tool in await cal_server.mcp.list_tools():
            props = set((tool.parameters or {}).get("properties") or {})
            assert not (props & forbidden), f"{tool.name} 暴露了凭证参数"

    async def test_create_event_documents_time_format(self, cal_env: dict) -> None:
        """时间格式是最容易出错的地方，schema 描述里必须写明示例。"""
        tools = {t.name: t for t in await cal_server.mcp.list_tools()}
        start_desc = ((tools["create_event"].parameters or {}).get("properties") or {}).get(
            "start", {}
        )
        assert "ISO" in json.dumps(start_desc, ensure_ascii=False)


class TestCrud:
    async def test_status_on_empty_workspace(self, cal_env: dict) -> None:
        payload = await call("get_calendar_status")
        assert payload["ok"] is True
        assert payload["timezone"] == "Asia/Shanghai"
        assert payload["dry_run"] is False
        assert payload["calendars"] == []

    async def test_create_then_list(self, cal_env: dict) -> None:
        created = await create("周会", "2026-09-11T10:00", "2026-09-11T11:00")
        assert created["ok"] is True
        assert created["event"]["title"] == "周会"
        assert created["event"]["minutes"] == 60
        assert created["conflicts"] == []

        listed = await call("list_events", start="2026-09-11T00:00", end="2026-09-12T00:00")
        assert listed["count"] == 1
        assert listed["events"][0]["title"] == "周会"

    async def test_event_written_to_disk(self, cal_env: dict) -> None:
        await create("落盘", "2026-09-11T10:00", "2026-09-11T11:00")
        assert (cal_env["workspace"] / "calendar" / "default.json").exists()

    async def test_conflict_detected(self, cal_env: dict) -> None:
        await create("已有会议", "2026-09-11T10:00", "2026-09-11T11:00")
        second = await create("冲突会议", "2026-09-11T10:30", "2026-09-11T11:30")

        assert second["ok"] is True  # 仍然创建成功，冲突需要人工决策
        assert len(second["conflicts"]) == 1
        assert second["conflicts"][0]["title"] == "已有会议"
        assert "next_step" in second

    async def test_adjacent_events_no_conflict(self, cal_env: dict) -> None:
        await create("上午会", "2026-09-11T10:00", "2026-09-11T11:00")
        second = await create("下午会", "2026-09-11T11:00", "2026-09-11T12:00")
        assert second["conflicts"] == []

    async def test_conflict_check_can_be_disabled(self, cal_env: dict) -> None:
        await create("已有会议", "2026-09-11T10:00", "2026-09-11T11:00")
        second = await create(
            "重叠会议", "2026-09-11T10:30", "2026-09-11T11:30", check_conflicts=False
        )
        assert second["conflicts"] == []

    async def test_custom_calendar(self, cal_env: dict) -> None:
        await create("工作日历", "2026-09-11T10:00", "2026-09-11T11:00", calendar="work")
        payload = await call("list_calendars")
        names = {item["name"] for item in payload["calendars"]}
        assert names == {"work"}

    async def test_calendar_scoped_query(self, cal_env: dict) -> None:
        await create("A", "2026-09-11T10:00", "2026-09-11T11:00", calendar="work")
        await create("B", "2026-09-11T10:00", "2026-09-11T11:00", calendar="personal")

        only_work = await call(
            "list_events", start="2026-09-11T00:00", end="2026-09-12T00:00", calendar="work"
        )
        assert [e["title"] for e in only_work["events"]] == ["A"]

        both = await call("list_events", start="2026-09-11T00:00", end="2026-09-12T00:00")
        assert both["count"] == 2

    async def test_update_event(self, cal_env: dict) -> None:
        created = await create("原标题", "2026-09-11T10:00", "2026-09-11T11:00")
        uid = created["event"]["uid"]

        updated = await call("update_event", uid=uid, title="新标题", location="3F")
        assert updated["ok"] is True
        assert updated["event"]["title"] == "新标题"
        assert updated["event"]["location"] == "3F"
        assert updated["event"]["start"] == created["event"]["start"]  # 未改的字段保持

    async def test_update_no_changes(self, cal_env: dict) -> None:
        created = await create("X", "2026-09-11T10:00", "2026-09-11T11:00")
        payload = await call("update_event", uid=created["event"]["uid"])
        assert payload["ok"] is False
        assert payload["code"] == "no_changes"
        assert "title" in payload["hint"]

    async def test_update_detects_new_conflict(self, cal_env: dict) -> None:
        first = await create("A", "2026-09-11T09:00", "2026-09-11T10:00")
        await create("B", "2026-09-11T14:00", "2026-09-11T15:00")

        moved = await call(
            "update_event",
            uid=first["event"]["uid"],
            start="2026-09-11T14:30",
            end="2026-09-11T15:30",
        )
        assert len(moved["conflicts"]) == 1
        assert moved["conflicts"][0]["title"] == "B"

    async def test_delete_event(self, cal_env: dict) -> None:
        created = await create("要删的会", "2026-09-11T10:00", "2026-09-11T11:00")
        payload = await call("delete_event", uid=created["event"]["uid"])
        assert payload["ok"] is True
        assert payload["title"] == "要删的会"

        listed = await call("list_events", start="2026-09-11T00:00", end="2026-09-12T00:00")
        assert listed["count"] == 0

    async def test_delete_unknown_uid(self, cal_env: dict) -> None:
        payload = await call("delete_event", uid="does-not-exist")
        assert payload["ok"] is False
        assert payload["hint"]


class TestTimeHandling:
    async def test_utc_input_converted_to_config_tz(self, cal_env: dict) -> None:
        """输入 UTC 时间，返回必须是上海时间 —— 但时刻一致。"""
        created = await create("跨时区", "2026-09-11T06:00:00Z", "2026-09-11T07:00:00Z")
        event = created["event"]
        assert event["start"].startswith("2026-09-11T14:00")
        assert "+08:00" in event["start"]

    async def test_naive_input_uses_config_tz(self, cal_env: dict) -> None:
        created = await create("本地时间", "2026-09-11T10:00", "2026-09-11T11:00")
        assert "+08:00" in created["event"]["start"]
        assert created["event"]["start"].startswith("2026-09-11T10:00")

    async def test_date_only_treated_as_midnight(self, cal_env: dict) -> None:
        created = await create("全天", "2026-09-11", "2026-09-12")
        assert created["event"]["start"].startswith("2026-09-11T00:00")

    async def test_invalid_time_has_actionable_hint(self, cal_env: dict) -> None:
        payload = await create("坏时间", "11/09/2026", "12/09/2026")
        assert payload["ok"] is False
        assert "ISO" in payload["hint"]

    async def test_end_before_start_rejected(self, cal_env: dict) -> None:
        payload = await create("倒置", "2026-09-11T12:00", "2026-09-11T11:00")
        assert payload["ok"] is False

    async def test_list_events_inverted_window(self, cal_env: dict) -> None:
        payload = await call("list_events", start="2026-09-12T00:00", end="2026-09-11T00:00")
        assert payload["ok"] is False
        assert "end" in payload["hint"].lower() or "起止" in payload["hint"]


class TestFreeSlots:
    async def test_free_slots_on_empty_day(self, cal_env: dict) -> None:
        payload = await call(
            "find_free_slots",
            day="2026-09-11",  # 星期五
            duration_minutes=60,
            work_start="09:00",
            work_end="12:00",
            step_minutes=60,
            limit=10,
        )
        assert payload["ok"] is True
        assert payload["count"] == 3
        assert payload["slots"][0]["start"].startswith("2026-09-11T09:00")

    async def test_free_slots_avoid_meetings(self, cal_env: dict) -> None:
        await create("占用", "2026-09-11T10:00", "2026-09-11T11:00")
        payload = await call(
            "find_free_slots",
            day="2026-09-11",
            duration_minutes=60,
            work_start="09:00",
            work_end="12:00",
            step_minutes=60,
        )
        starts = [slot["start"][11:16] for slot in payload["slots"]]
        assert "10:00" not in starts
        assert "09:00" in starts
        assert "11:00" in starts

    async def test_weekend_returns_empty_with_note(self, cal_env: dict) -> None:
        payload = await call(
            "find_free_slots",
            day="2026-09-12",
            duration_minutes=60,  # 星期六
        )
        assert payload["is_weekend"] is True
        assert payload["slots"] == []
        assert "note" in payload

    async def test_weekend_allowed_when_requested(self, cal_env: dict) -> None:
        payload = await call(
            "find_free_slots",
            day="2026-09-12",
            duration_minutes=60,
            work_start="09:00",
            work_end="11:00",
            step_minutes=60,
            include_weekend=True,
        )
        assert payload["count"] == 2

    async def test_fully_booked_day(self, cal_env: dict) -> None:
        await create("全天会议", "2026-09-11T09:00", "2026-09-11T18:00")
        payload = await call("find_free_slots", day="2026-09-11", duration_minutes=30)
        assert payload["slots"] == []

    async def test_invalid_work_window(self, cal_env: dict) -> None:
        payload = await call(
            "find_free_slots",
            day="2026-09-11",
            duration_minutes=60,
            work_start="18:00",
            work_end="09:00",
        )
        assert payload["ok"] is False
        assert payload["hint"]

    async def test_invalid_day_format(self, cal_env: dict) -> None:
        payload = await call("find_free_slots", day="2026/09/11", duration_minutes=60)
        assert payload["ok"] is False


class TestIcs:
    SAMPLE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
UID:import-1@example.com
SUMMARY:导入的会议
DTSTART:20260911T060000Z
DTEND:20260911T070000Z
LOCATION:线上
ATTENDEE;CN=Alice;ROLE=REQ-PARTICIPANT:mailto:alice@corp.example.com
END:VEVENT
END:VCALENDAR
"""

    async def test_import_ics(self, cal_env: dict) -> None:
        target = cal_env["workspace"] / "uploads"
        target.mkdir()
        (target / "invite.ics").write_text(self.SAMPLE, encoding="utf-8")

        payload = await call("import_ics_file", path="uploads/invite.ics")
        assert payload["ok"] is True
        assert payload["added"] == 1

        listed = await call("list_events", start="2026-09-11T00:00", end="2026-09-12T00:00")
        event = listed["events"][0]
        assert event["title"] == "导入的会议"
        assert event["location"] == "线上"
        assert event["attendees"] == ["alice@corp.example.com"]
        assert event["start"].startswith("2026-09-11T14:00")  # 06:00 UTC

    async def test_import_missing_file(self, cal_env: dict) -> None:
        payload = await call("import_ics_file", path="uploads/nope.ics")
        assert payload["ok"] is False
        assert payload["code"] == "file_not_found"
        assert "list_files" in payload["hint"]

    async def test_import_path_escape_blocked(self, cal_env: dict) -> None:
        payload = await call("import_ics_file", path="../../../Windows/win.ini")
        assert payload["ok"] is False
        assert payload["code"] == "path_violation"

    async def test_import_corrupt_ics(self, cal_env: dict) -> None:
        target = cal_env["workspace"] / "bad.ics"
        target.write_text("this is not ical", encoding="utf-8")
        payload = await call("import_ics_file", path="bad.ics")
        assert payload["ok"] is False
        assert payload["code"] == "document_parse_error"

    async def test_import_is_idempotent_by_uid(self, cal_env: dict) -> None:
        target = cal_env["workspace"] / "invite.ics"
        target.write_text(self.SAMPLE, encoding="utf-8")

        await call("import_ics_file", path="invite.ics")
        second = await call("import_ics_file", path="invite.ics")
        assert second["added"] == 0
        assert second["replaced"] == 1
        assert second["total"] == 1

    async def test_export_ics_round_trip(self, cal_env: dict) -> None:
        await create(
            "导出的会议",
            "2026-09-11T14:00",
            "2026-09-11T15:00",
            location="3F",
            attendees=["alice@corp.example.com"],
        )
        exported = await call(
            "export_ics_file",
            start="2026-09-11T00:00",
            end="2026-09-12T00:00",
            filename="邀请.ics",
        )
        assert exported["ok"] is True
        assert exported["event_count"] == 1
        assert exported["path"].startswith("outputs/")

        # 再导入到另一个日历，验证往返一致
        payload = await call("import_ics_file", path=exported["path"], calendar="roundtrip")
        assert payload["added"] == 1
        listed = await call(
            "list_events", start="2026-09-11T00:00", end="2026-09-12T00:00", calendar="roundtrip"
        )
        assert listed["events"][0]["title"] == "导出的会议"
        assert listed["events"][0]["location"] == "3F"

    async def test_export_empty_range(self, cal_env: dict) -> None:
        payload = await call("export_ics_file", start="2026-09-11T00:00", end="2026-09-12T00:00")
        assert payload["ok"] is True
        assert payload["event_count"] == 0

    async def test_export_filename_sanitized(self, cal_env: dict) -> None:
        payload = await call(
            "export_ics_file",
            start="2026-09-11T00:00",
            end="2026-09-12T00:00",
            filename="../../evil.ics",
        )
        assert payload["ok"] is True
        assert payload["path"].startswith("outputs/")
        assert ".." not in payload["path"]


class TestDryRun:
    async def test_dry_run_does_not_persist(
        self, cal_env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OFFICE_MCP_DRY_RUN", "1")
        _clear_caches()

        created = await create("演练", "2026-09-11T10:00", "2026-09-11T11:00")
        assert created["ok"] is True
        assert created["dry_run"] is True
        assert "would_create" in created
        assert not (cal_env["workspace"] / "calendar" / "default.json").exists()

    async def test_dry_run_delete_and_export(
        self, cal_env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        created = await create("X", "2026-09-11T10:00", "2026-09-11T11:00")
        uid = created["event"]["uid"]

        monkeypatch.setenv("OFFICE_MCP_DRY_RUN", "1")
        _clear_caches()

        deleted = await call("delete_event", uid=uid)
        assert deleted["dry_run"] is True
        exported = await call("export_ics_file", start="2026-09-11T00:00", end="2026-09-12T00:00")
        assert exported["dry_run"] is True
        assert not (cal_env["workspace"] / "outputs").exists()


class TestAudit:
    async def test_calendar_writes_are_audited(self, cal_env: dict) -> None:
        await create("审计测试", "2026-09-11T10:00", "2026-09-11T11:00")
        records = [
            json.loads(line) for line in cal_env["audit"].read_text(encoding="utf-8").splitlines()
        ]
        entry = next(r for r in records if r["tool"] == "create_event")
        assert entry["status"] == "ok"
        assert entry["args"]["title"] == "审计测试"

    async def test_error_path_audited(self, cal_env: dict) -> None:
        await create("坏时间", "not-a-date", "also-bad")
        records = [
            json.loads(line) for line in cal_env["audit"].read_text(encoding="utf-8").splitlines()
        ]
        entry = next(r for r in records if r["tool"] == "create_event")
        assert entry["status"] == "error"
        assert entry["error_code"]
