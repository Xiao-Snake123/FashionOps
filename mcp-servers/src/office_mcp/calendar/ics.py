"""iCalendar（RFC 5545）解析与生成。

用 ``icalendar`` 库处理协议细节（转义、折行、编码），本模块只负责
「ICS ↔ 内部模型」的映射与错误收敛。

**限制**：重复事件（``RRULE``）只保留规则原文，不展开成实例。
展开需要处理 EXDATE / RDATE / 时区转换 / 无限重复，属于独立课题；
当前实现明确不猜，把 ``recurrence`` 原样交给调用方判断。
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from zoneinfo import ZoneInfo

from icalendar import Calendar as ICalendar
from icalendar import Event as IEvent

from office_mcp.calendar.models import Attendee, CalendarEvent, new_uid
from office_mcp.calendar.timeutil import normalize_from_ics
from office_mcp.common.errors import DocumentParseError, OfficeMCPError

PRODID = "-//office-mcp//Office Agent Calendar//CN"


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #


def parse_ics(
    raw: bytes,
    *,
    tz: ZoneInfo,
    calendar_name: str = "default",
) -> list[CalendarEvent]:
    """把 ICS 内容解析成事件列表。

    容忍常见的不规范输入：
    - 缺少 ``UID`` → 自动生成
    - 缺少 ``DTEND`` → 用 ``DURATION``，再没有就按 0 时长处理
    - 缺少 ``SUMMARY`` → 用「(无标题)」占位
    """
    try:
        parsed = ICalendar.from_ical(raw)
    except Exception as exc:  # noqa: BLE001 - icalendar 抛的异常类型不稳定
        raise DocumentParseError(
            f"ICS 文件解析失败：{exc}",
            hint="请确认文件是合法的 iCalendar 格式（扩展名 .ics）。",
        ) from exc

    events: list[CalendarEvent] = []
    for component in parsed.walk("VEVENT"):
        try:
            events.append(_to_event(component, tz=tz, calendar_name=calendar_name))
        except OfficeMCPError:
            # 单个事件坏了不该让整个文件解析失败
            continue
    return events


def _to_event(component: IEvent, *, tz: ZoneInfo, calendar_name: str) -> CalendarEvent:
    dtstart = component.get("DTSTART")
    if dtstart is None:
        raise DocumentParseError(
            "事件缺少 DTSTART 字段。",
            hint="iCalendar 规范要求事件必须有开始时间，请检查文件来源。",
        )

    start, all_day = normalize_from_ics(dtstart.dt, tz)

    dtend = component.get("DTEND")
    if dtend is not None:
        end, _ = normalize_from_ics(dtend.dt, tz)
    else:
        duration = component.get("DURATION")
        if duration is not None and getattr(duration, "dt", None) is not None:
            end = start + duration.dt
        else:
            # 零时长事件在日历里合法（如提醒），这里给一个最小占位
            end = start

    if end < start:
        end = start

    return CalendarEvent(
        uid=str(component.get("UID") or new_uid()),
        title=str(component.get("SUMMARY") or "(无标题)"),
        start=start,
        end=end,
        all_day=all_day,
        location=_optional_str(component.get("LOCATION")),
        description=_optional_str(component.get("DESCRIPTION")),
        attendees=_parse_attendees(component),
        calendar=calendar_name,
        status=_parse_status(component.get("STATUS")),
        organizer=_parse_organizer(component.get("ORGANIZER")),
        recurrence=_parse_rrule(component.get("RRULE")),
        created_at=_optional_datetime(component.get("CREATED"), tz),
        updated_at=_optional_datetime(component.get("LAST-MODIFIED"), tz),
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_datetime(value: object, tz: ZoneInfo) -> datetime | None:
    if value is None or not hasattr(value, "dt"):
        return None
    try:
        return normalize_from_ics(value.dt, tz)[0]  # type: ignore[attr-defined]
    except OfficeMCPError:
        return None


def _parse_status(value: object) -> str:
    text = str(value or "CONFIRMED").strip().upper()
    return text if text in {"CONFIRMED", "TENTATIVE", "CANCELLED"} else "CONFIRMED"


def _parse_organizer(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[len("mailto:") :] if text.lower().startswith("mailto:") else text


def _parse_rrule(value: object) -> str | None:
    if value is None:
        return None
    try:
        return value.to_ical().decode("utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return str(value)


def _parse_attendees(component: IEvent) -> list[Attendee]:
    raw = component.get("ATTENDEE")
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]

    attendees: list[Attendee] = []
    for item in items:
        text = str(item)
        email = text[len("mailto:") :] if text.lower().startswith("mailto:") else text
        params = getattr(item, "params", {}) or {}
        name = params.get("CN")
        role = str(params.get("ROLE", "")).upper()
        attendees.append(
            Attendee(
                email=email,
                name=str(name) if name else None,
                # ROLE=OPT-PARTICIPANT 表示可选参加
                required=role != "OPT-PARTICIPANT",
            )
        )
    return attendees


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #


def build_ics(events: list[CalendarEvent], *, calendar_name: str = "Office Agent") -> bytes:
    """把事件列表生成 ICS 内容。

    生成的时间统一转成 UTC 并加 ``Z`` 后缀 —— 这是跨客户端兼容性最好的做法，
    可以彻底避开"接收方时区设置不同导致时间错位"的问题。
    """
    cal = ICalendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", calendar_name)

    for event in events:
        cal.add_component(build_event(event))
    return cal.to_ical()


def build_event(event: CalendarEvent) -> IEvent:
    """把单个事件转成 iCalendar 组件。"""
    component = IEvent()
    component.add("uid", event.uid)
    component.add("summary", event.title)
    component.add("dtstart", event.start)
    component.add("dtend", event.end)
    component.add("dtstamp", event.updated_at or event.created_at or event.start)
    component.add("status", event.status)

    if event.location:
        component.add("location", event.location)
    if event.description:
        component.add("description", event.description)
    if event.organizer:
        component.add("organizer", f"mailto:{event.organizer}")
    if event.recurrence:
        # 规则串非法就忽略重复属性，不让整个导出失败
        with contextlib.suppress(Exception):
            component.add("rrule", _parse_rrule_string(event.recurrence))

    for attendee in event.attendees:
        params: dict[str, object] = {
            "ROLE": "REQ-PARTICIPANT" if attendee.required else "OPT-PARTICIPANT"
        }
        if attendee.name:
            params["CN"] = attendee.name
        component.add("attendee", f"mailto:{attendee.email}", parameters=params)
    return component


def _parse_rrule_string(value: str) -> object:
    """把 ``FREQ=WEEKLY;COUNT=5`` 这样的字符串转成 icalendar 的 vRecur。"""
    from icalendar import vRecur

    pairs: dict[str, list[str]] = {}
    for chunk in value.split(";"):
        if "=" not in chunk:
            continue
        key, _, raw = chunk.partition("=")
        pairs[key.strip().upper()] = [part for part in raw.split(",") if part]
    return vRecur(pairs)


def build_invite(event: CalendarEvent, *, organizer: str, method: str = "REQUEST") -> bytes:
    """生成会议邀请 ICS（可直接作为邮件附件发出）。

    Args:
        organizer: 组织者邮箱。
        method: ``REQUEST``（邀请）/ ``CANCEL``（取消）/ ``REPLY``（回复）。
    """
    cal = ICalendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add("method", method.upper())
    payload = event.model_copy(update={"organizer": organizer})
    cal.add_component(build_event(payload))
    return cal.to_ical()
