"""日历 MCP 服务器。

对外暴露的工具：

==========================  ====================================================
工具                        作用
==========================  ====================================================
``get_calendar_status``     时区、已有日历、事件数量（不含隐私内容）
``list_calendars``          列出所有日历
``list_events``             查询时间段内的事件
``create_event``            新建事件（默认做冲突检测）
``update_event``            修改事件
``delete_event``            删除事件
``find_free_slots``         找共同空闲时间
``import_ics_file``         从工作区导入 ``.ics``
``export_ics_file``         导出 ``.ics``（可直接作为会议邀请邮件附件）
==========================  ====================================================

运行::

    python -m office_mcp.calendar.server
"""

from __future__ import annotations

import logging
import os
from datetime import date
from functools import lru_cache
from typing import Any

from fastmcp import FastMCP

from office_mcp.calendar import ics
from office_mcp.calendar.models import Attendee, CalendarEvent, new_uid
from office_mcp.calendar.slots import find_free_slots as compute_free_slots
from office_mcp.calendar.slots import overlaps_any
from office_mcp.calendar.store import CalendarStore
from office_mcp.calendar.timeutil import day_bounds, get_tz, parse_datetime
from office_mcp.common import (
    DocumentParseError,
    get_settings,
    resolve_in_root,
)
from office_mcp.common.paths import safe_filename
from office_mcp.common.tooling import guard_tool

logger = logging.getLogger("office_mcp.calendar")

mcp = FastMCP(
    name="office-calendar",
    instructions=(
        "办公日历服务器。可以查询日程、创建/修改/删除事件、找共同空闲时间。\n\n"
        "使用建议：\n"
        "1. 时间一律用 ISO 格式（如 2026-09-11T14:30）。不带时区时按服务器默认时区解释。\n"
        "2. 创建会议前先用 find_free_slots 确认参会人都有空。\n"
        "3. 创建事件时默认会做冲突检测，若有冲突会在返回值里提示，请先与用户确认。\n"
        "4. 重复事件（RRULE）只保留规则原文，不会被展开成具体场次。"
    ),
)


@lru_cache(maxsize=1)
def get_store() -> CalendarStore:
    return CalendarStore(get_settings().workspace_root)


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #


@mcp.tool
@guard_tool("get_calendar_status")
def get_calendar_status() -> dict[str, Any]:
    """返回日历能力的当前状态：默认时区、已有日历、事件总数、是否 dry_run。"""
    settings = get_settings()
    store = get_store()
    calendars = store.calendars()
    return {
        "timezone": settings.timezone,
        "dry_run": settings.dry_run,
        "calendars": calendars,
        "event_counts": {name: len(store.load(name)) for name in calendars},
        "capabilities": [
            "list_events",
            "create_event",
            "update_event",
            "delete_event",
            "find_free_slots",
            "import_ics_file",
            "export_ics_file",
        ],
        "note": "recurrence 字段只保留 RRULE 原文，本服务不展开重复事件实例。",
    }


@mcp.tool
@guard_tool("list_calendars")
def list_calendars() -> dict[str, Any]:
    """列出工作区里已有的所有日历。"""
    store = get_store()
    calendars = store.calendars()
    return {
        "count": len(calendars),
        "calendars": [{"name": name, "events": len(store.load(name))} for name in calendars],
    }


@mcp.tool
@guard_tool("list_events")
def list_events(
    start: str,
    end: str,
    calendar: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """查询指定时间段内的事件（返回与区间有交集的事件。

    Args:
        start: 起始时间，ISO 格式，如 2026-09-11T00:00。
        end: 结束时间，ISO 格式。
        calendar: 只查某个日历；不填则查全部。
        limit: 最多返回多少条。
    """
    settings = get_settings()
    tz = get_tz(settings.timezone)
    window_start = parse_datetime(start, tz, field="start")
    window_end = parse_datetime(end, tz, field="end")
    if window_end <= window_start:
        raise DocumentParseError(
            "end 必须晚于 start。",
            hint="请检查起止时间是否写反了。",
        )

    events = get_store().in_range(start=window_start, end=window_end, calendar_name=calendar)
    selected = events[: max(1, limit)]
    return {
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        "timezone": settings.timezone,
        "count": len(events),
        "truncated": len(events) > len(selected),
        "events": [_present(event) for event in selected],
    }


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #


@mcp.tool
@guard_tool("create_event")
def create_event(
    title: str,
    start: str,
    end: str,
    location: str | None = None,
    description: str | None = None,
    attendees: list[str] | None = None,
    calendar: str = "default",
    recurrence: str | None = None,
    check_conflicts: bool = True,
) -> dict[str, Any]:
    """创建日历事件。

    默认会做冲突检测：若有重叠事件，返回值里会带上 ``conflicts``，
    但**事件仍会创建成功**（由调用方决定是否回滚或改期）。

    Args:
        title: 事件标题。
        start: 开始时间，ISO 格式。
        end: 结束时间，ISO 格式。
        location: 地点。
        description: 描述。
        attendees: 参会人，格式为 ``email`` 或 ``张三 <email>``。
        calendar: 写入哪个日历，默认 default。
        recurrence: RRULE 规则原文，如 ``FREQ=WEEKLY;COUNT=5``。
        check_conflicts: 是否做冲突检测。
    """
    settings = get_settings()
    tz = get_tz(settings.timezone)
    store = get_store()

    event = CalendarEvent(
        uid=new_uid(),
        title=title,
        start=parse_datetime(start, tz, field="start"),
        end=parse_datetime(end, tz, field="end"),
        location=location,
        description=description,
        attendees=_parse_attendees(attendees or []),
        calendar=calendar,
        organizer=settings.mail_address,
        recurrence=recurrence,
    )

    conflicts: list[dict[str, Any]] = []
    if check_conflicts:
        existing = store.in_range(start=event.start, end=event.end, calendar_name=calendar)
        conflicts = [_present(item) for item in overlaps_any(event, existing)]

    if settings.dry_run:
        return {
            "dry_run": True,
            "would_create": _present(event),
            "conflicts": conflicts,
        }

    stored = store.add(event)
    payload: dict[str, Any] = {"event": _present(stored), "conflicts": conflicts}
    if conflicts:
        payload["next_step"] = "存在时间冲突。请把 conflicts 展示给用户，确认是改期还是接受冲突。"
    return payload


@mcp.tool
@guard_tool("update_event")
def update_event(
    uid: str,
    title: str | None = None,
    start: str | None = None,
    end: str | None = None,
    location: str | None = None,
    description: str | None = None,
    attendees: list[str] | None = None,
    recurrence: str | None = None,
) -> dict[str, Any]:
    """修改事件。只传需要改的字段。

    Args:
        uid: 事件 UID，来自 ``list_events``。
        title: 新标题。
        start: 新开始时间，ISO 格式。
        end: 新结束时间，ISO 格式。
        location: 新地点。
        description: 新描述。
        attendees: 新参会人列表（会整体替换原列表）。
        recurrence: 新 RRULE 规则原文。
    """
    settings = get_settings()
    tz = get_tz(settings.timezone)
    store = get_store()
    current = store.get(uid)

    changes: dict[str, Any] = {}
    if title is not None:
        changes["title"] = title
    if start is not None:
        changes["start"] = parse_datetime(start, tz, field="start")
    if end is not None:
        changes["end"] = parse_datetime(end, tz, field="end")
    if location is not None:
        changes["location"] = location
    if description is not None:
        changes["description"] = description
    if attendees is not None:
        changes["attendees"] = _parse_attendees(attendees)
    if recurrence is not None:
        changes["recurrence"] = recurrence

    if not changes:
        return {
            "ok": False,
            "code": "no_changes",
            "error": "没有提供任何要修改的字段。",
            "hint": "请至少传入 title / start / end / location / description / attendees 之一。",
        }

    merged = current.model_copy(update=changes)
    conflicts = [
        _present(item)
        for item in overlaps_any(
            merged,
            store.in_range(start=merged.start, end=merged.end, calendar_name=current.calendar),
        )
    ]

    if settings.dry_run:
        return {"dry_run": True, "would_update": _present(merged), "conflicts": conflicts}

    updated = store.update(uid, changes)
    return {"event": _present(updated), "conflicts": conflicts}


@mcp.tool
@guard_tool("delete_event")
def delete_event(uid: str) -> dict[str, Any]:
    """删除事件。

    Args:
        uid: 事件 UID。
    """
    settings = get_settings()
    store = get_store()
    event = store.get(uid)
    if settings.dry_run:
        return {"dry_run": True, "would_delete": _present(event)}
    calendar_name = store.delete(uid)
    return {"deleted_uid": uid, "calendar": calendar_name, "title": event.title}


# --------------------------------------------------------------------------- #
# 空闲时间
# --------------------------------------------------------------------------- #


@mcp.tool
@guard_tool("find_free_slots")
def find_free_slots(
    day: str,
    duration_minutes: int,
    work_start: str = "09:00",
    work_end: str = "18:00",
    calendar: str | None = None,
    step_minutes: int = 15,
    limit: int = 10,
    include_weekend: bool = False,
) -> dict[str, Any]:
    """找出某一天里时长足够的共同空闲时段。

    Args:
        day: 日期，格式 YYYY-MM-DD。
        duration_minutes: 需要的会议时长（分钟）。
        work_start: 工作时间起点，HH:MM，默认 09:00。
        work_end: 工作时间终点，HH:MM，默认 18:00。
        calendar: 只参考某个日历；不填则合并全部日历。
        step_minutes: 候选时间的对齐粒度（分钟），默认 15，让开始时间"整齐"。
        limit: 最多返回几段。
        include_weekend: 是否允许周末（默认不允许；若当天是周末且未开启，返回空）。
    """
    settings = get_settings()
    tz = get_tz(settings.timezone)
    target_day = date.fromisoformat(day.strip())

    if not include_weekend and target_day.weekday() >= 5:
        return {
            "day": day,
            "is_weekend": True,
            "slots": [],
            "note": "当天是周末，未开启 include_weekend，因此不推荐安排会议。",
        }

    window_start, window_end = day_bounds(target_day, tz)
    window_start = parse_datetime(f"{day}T{work_start}", tz, field="work_start")
    window_end = parse_datetime(f"{day}T{work_end}", tz, field="work_end")
    if window_end <= window_start:
        raise DocumentParseError(
            "work_end 必须晚于 work_start。",
            hint="默认工作时段是 09:00–18:00，请检查传入的时间。",
        )

    events = get_store().in_range(start=window_start, end=window_end, calendar_name=calendar)
    slots = compute_free_slots(
        events,
        window_start=window_start,
        window_end=window_end,
        duration_minutes=duration_minutes,
        step_minutes=step_minutes,
        limit=limit,
    )
    return {
        "day": day,
        "timezone": settings.timezone,
        "work_window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        "duration_minutes": duration_minutes,
        "busy_count": len(events),
        "count": len(slots),
        # 达到 limit 说明可能还有更多，明确告知模型可以放宽 limit
        "truncated": len(slots) >= limit,
        "slots": [
            {"start": s.start.isoformat(), "end": s.end.isoformat(), "minutes": s.minutes}
            for s in slots
        ],
        "note": (
            "候选开始时间已按 step_minutes 对齐；相邻会议不算冲突（左闭右开）。"
            "count 是本次返回的段数，truncated 为 true 时可能还有更多。"
        ),
    }


# --------------------------------------------------------------------------- #
# ICS 导入导出
# --------------------------------------------------------------------------- #


@mcp.tool
@guard_tool("import_ics_file")
def import_ics_file(
    path: str, calendar: str = "default", replace_by_uid: bool = True
) -> dict[str, Any]:
    """从工作区导入 ``.ics`` 文件。

    Args:
        path: 工作区内的相对路径，如 ``uploads/邀请.ics``。
        calendar: 导入到哪个日历。
        replace_by_uid: UID 相同的事件是否覆盖（默认覆盖，符合"同一次会议是更新"的直觉）。
    """
    settings = get_settings()
    tz = get_tz(settings.timezone)
    target = resolve_in_root(path, settings.workspace_root, must_exist=True)
    events = ics.parse_ics(target.read_bytes(), tz=tz, calendar_name=calendar)

    if settings.dry_run:
        return {
            "dry_run": True,
            "parsed": len(events),
            "would_import": [_present(event) for event in events[:20]],
        }

    stats = get_store().import_events(events, calendar_name=calendar, replace_by_uid=replace_by_uid)
    return {
        "source": str(target.relative_to(settings.workspace_root)).replace("\\", "/"),
        "calendar": calendar,
        **stats,
        "note": "重复事件（RRULE）只保留规则原文，未展开为具体场次。",
    }


@mcp.tool
@guard_tool("export_ics_file")
def export_ics_file(
    start: str,
    end: str,
    filename: str = "calendar.ics",
    calendar: str | None = None,
) -> dict[str, Any]:
    """把时间段内的事件导出成 ``.ics`` 文件（可直接作为会议邀请邮件附件）。

    Args:
        start: 起始时间，ISO 格式。
        end: 结束时间，ISO 格式。
        filename: 输出文件名，会写入工作区 ``outputs/`` 目录。
        calendar: 导出哪个日历；不填则导出全部。
    """
    settings = get_settings()
    tz = get_tz(settings.timezone)
    window_start = parse_datetime(start, tz, field="start")
    window_end = parse_datetime(end, tz, field="end")

    events = get_store().in_range(start=window_start, end=window_end, calendar_name=calendar)
    raw = ics.build_ics(events, calendar_name=calendar or "全部日历")

    name = safe_filename(filename, default_ext=".ics")
    target = settings.workspace_root / "outputs" / name

    if settings.dry_run:
        return {
            "dry_run": True,
            "would_write": str(target),
            "event_count": len(events),
            "bytes": len(raw),
        }

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return {
        "path": str(target.relative_to(settings.workspace_root)).replace("\\", "/"),
        "event_count": len(events),
        "bytes": len(raw),
        "next_step": "可用 send_email 把这个 .ics 作为附件发出，收件人客户端会渲染成会议邀请。",
    }


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _present(event: CalendarEvent) -> dict[str, Any]:
    """把事件转成给模型看的精简形态。"""
    return {
        "uid": event.uid,
        "title": event.title,
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "minutes": event.duration_minutes,
        "all_day": event.all_day,
        "location": event.location,
        "calendar": event.calendar,
        "status": event.status,
        "attendees": [a.email for a in event.attendees],
        "recurrence": event.recurrence,
    }


def _parse_attendees(values: list[str]) -> list[Attendee]:
    """解析参会人：支持 ``email`` 与 ``张三 <email>`` 两种写法。"""
    from office_mcp.email.guard import _extract_address

    attendees: list[Attendee] = []
    for raw in values:
        text = (raw or "").strip()
        if not text:
            continue
        email = _extract_address(text)
        name = text[: text.rindex("<")].strip() if "<" in text else None
        if not email:
            continue
        attendees.append(Attendee(email=email, name=name or None))
    return attendees


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("OFFICE_MCP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    transport = os.environ.get("OFFICE_MCP_TRANSPORT", "stdio").lower()
    if transport in {"http", "streamable-http", "sse"}:
        mcp.run(
            transport="http",
            host=os.environ.get("OFFICE_MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("OFFICE_MCP_PORT", "8767")),
        )
    else:
        mcp.run()


if __name__ == "__main__":
    main()
