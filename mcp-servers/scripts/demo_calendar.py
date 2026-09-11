"""日历 MCP 服务器端到端演示。

演示一条完整的办公日历流程：
导入会议邀请 → 查看日程 → 找共同空闲时间 → 排新会（含冲突检测）
→ 改期消除冲突 → 导出 ICS 作为会议邀请附件。

用法::

    uv run python scripts/demo_calendar.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

DEMO_ROOT = Path(__file__).resolve().parents[1] / "workspace"

# 每次从干净状态开始，保证演示可重复
shutil.rmtree(DEMO_ROOT / "calendar", ignore_errors=True)

os.environ["OFFICE_MCP_WORKSPACE_ROOT"] = str(DEMO_ROOT)
os.environ["OFFICE_MCP_AUDIT_LOG"] = str(DEMO_ROOT / "audit-calendar.jsonl")
os.environ["OFFICE_MCP_TIMEZONE"] = "Asia/Shanghai"
os.environ["OFFICE_MCP_MAIL_ADDRESS"] = "agent@corp.example.com"
os.environ["OFFICE_MCP_DRY_RUN"] = "0"

from office_mcp.calendar.server import mcp  # noqa: E402

SEP = "─" * 74

INVITE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Demo//Demo//CN
BEGIN:VEVENT
UID:q3-review@corp.example.com
SUMMARY:Q3 季度复盘
DTSTART:20260911T013000Z
DTEND:20260911T030000Z
LOCATION:3F 会议室
ATTENDEE;CN=Alice;ROLE=REQ-PARTICIPANT:mailto:alice@corp.example.com
ATTENDEE;CN=Bob;ROLE=REQ-PARTICIPANT:mailto:bob@corp.example.com
ORGANIZER;CN=Boss:mailto:boss@corp.example.com
END:VEVENT
END:VCALENDAR
"""


def section(text: str) -> None:
    print(f"\n{SEP}\n  {text}\n{SEP}")


async def call(tool: str, **kwargs: object) -> dict:
    result = await mcp.call_tool(tool, kwargs)
    payload = json.loads(result.content[0].text)
    ok = payload.get("ok")
    print(f"  {'✓' if ok else '✗'} {tool}")
    if not ok:
        print(f"      code: {payload.get('code')}")
        print(f"      hint: {payload.get('hint')}")
    return payload


def hhmm(iso: str) -> str:
    """``2026-09-11T14:00:00+08:00`` → ``14:00``"""
    return iso[11:16]


async def main() -> int:
    problems: list[str] = []

    # ---------------------------------------------------------------- #
    section("1. 注册的工具")
    tools = await mcp.list_tools()
    print(f"  共 {len(tools)} 个：")
    for tool in tools:
        print(f"    • {tool.name:22s} {(tool.description or '').splitlines()[0][:44]}")

    # ---------------------------------------------------------------- #
    section("2. 初始状态")
    status = await call("get_calendar_status")
    print(f"      时区 = {status.get('timezone')}   日历 = {status.get('calendars')}")

    # ---------------------------------------------------------------- #
    section("3. 导入一份会议邀请（.ics）")
    uploads = DEMO_ROOT / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    (uploads / "q3-review.ics").write_text(INVITE, encoding="utf-8")
    imported = await call("import_ics_file", path="uploads/q3-review.ics")
    print(f"      新增 {imported.get('added')} 条，覆盖 {imported.get('replaced')} 条")

    # ---------------------------------------------------------------- #
    section("4. 查看 09-11 的日程")
    listed = await call("list_events", start="2026-09-11T00:00", end="2026-09-12T00:00")
    for event in listed.get("events", []):
        print(
            f"      {hhmm(event['start'])}–{hhmm(event['end'])}  {event['title']}"
            f"  @ {event.get('location')}  参会人 {len(event.get('attendees', []))} 人"
        )
    if listed.get("count") != 1:
        problems.append("导入后日程数量不对")

    # ---------------------------------------------------------------- #
    section("5. 找 09-11 的共同空闲时间（需 60 分钟，工作时段 09:00–18:00）")
    free = await call(
        "find_free_slots", day="2026-09-11", duration_minutes=60, step_minutes=30, limit=8
    )
    for slot in free.get("slots", []):
        print(f"      {hhmm(slot['start'])}–{hhmm(slot['end'])}  ({slot['minutes']} 分钟)")
    if not free.get("slots"):
        problems.append("空闲时间计算")

    # ---------------------------------------------------------------- #
    section("6. 排一个新会 → 触发冲突检测")
    conflict = await call(
        "create_event",
        title="客户需求评审",
        start="2026-09-11T10:00",
        end="2026-09-11T11:00",
        location="线上",
        attendees=["alice@corp.example.com", "carol@corp.example.com"],
    )
    if conflict.get("conflicts"):
        for item in conflict["conflicts"]:
            print(f"      ⚠ 与「{item['title']}」{hhmm(item['start'])}–{hhmm(item['end'])} 冲突")
    else:
        problems.append("未检测到预期冲突")

    # ---------------------------------------------------------------- #
    section("7. 改期消除冲突（挪到 15:00）")
    moved = await call(
        "update_event",
        uid=conflict["event"]["uid"],
        start="2026-09-11T15:00",
        end="2026-09-11T16:00",
    )
    print(f"      新时间 {hhmm(moved['event']['start'])}–{hhmm(moved['event']['end'])}")
    print(f"      剩余冲突 {len(moved.get('conflicts', []))} 个")
    if moved.get("conflicts"):
        problems.append("改期后仍有冲突")

    # ---------------------------------------------------------------- #
    section("8. 重新计算空闲时间（把新排的会也算进去）")
    free2 = await call(
        "find_free_slots", day="2026-09-11", duration_minutes=60, step_minutes=30, limit=8
    )
    starts_before = {hhmm(s["start"]) for s in free.get("slots", [])}
    starts_after = {hhmm(s["start"]) for s in free2.get("slots", [])}
    removed = sorted(starts_before - starts_after)
    print(f"      新增忙碌导致消失的候选时段：{removed or '（无，均在 limit 之外）'}")
    for slot in free2.get("slots", []):
        print(f"      {hhmm(slot['start'])}–{hhmm(slot['end'])}")

    # ---------------------------------------------------------------- #
    section("9. 周末保护")
    weekend = await call("find_free_slots", day="2026-09-12", duration_minutes=60)
    slots = weekend.get("slots", [])
    print(f"      09-12 是周末：{weekend.get('is_weekend')}，推荐时段 {len(slots)} 段")

    # ---------------------------------------------------------------- #
    section("10. 导出为 ICS（可直接作为会议邀请邮件附件）")
    exported = await call(
        "export_ics_file",
        start="2026-09-11T00:00",
        end="2026-09-12T00:00",
        filename="2026-09-11 日程.ics",
    )
    count = exported.get("event_count")
    size = exported.get("bytes")
    print(f"      写入 {exported.get('path')}（{count} 个事件，{size} 字节）")

    path = DEMO_ROOT / str(exported.get("path"))
    if path.exists():
        head = path.read_text(encoding="utf-8").splitlines()[:6]
        for line in head:
            print(f"      │ {line}")

    # ---------------------------------------------------------------- #
    section("11. 往返验证：把导出的 ICS 再导入回一个新日历")
    roundtrip = await call("import_ics_file", path=str(exported.get("path")), calendar="roundtrip")
    print(f"      新增 {roundtrip.get('added')} 条（UID 不同时会新增）")
    if roundtrip.get("ok") is not True:
        problems.append("ICS 往返导入")

    # ---------------------------------------------------------------- #
    section("12. 审计日志")
    audit = Path(os.environ["OFFICE_MCP_AUDIT_LOG"])
    if audit.exists():
        records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        errors = [r for r in records if r.get("status") == "error"]
        print(f"  共 {len(records)} 条记录，其中失败 {len(errors)} 条")
        print("  按工具统计：")
        counts: dict[str, int] = {}
        for record in records:
            counts[record["tool"]] = counts.get(record["tool"], 0) + 1
        for tool_name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"    • {tool_name:20s} {count} 次")
    else:
        problems.append("审计日志")

    print(f"\n{SEP}")
    if problems:
        print(f"  ✗ 有 {len(problems)} 项异常：{', '.join(problems)}")
        return 1
    print("  ✓ 全部通过")
    print(SEP)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
