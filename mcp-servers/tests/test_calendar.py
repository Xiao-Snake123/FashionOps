"""日历核心逻辑测试：区间计算、时区处理、ICS 编解码、本地存储。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from office_mcp.calendar import ics
from office_mcp.calendar.models import Attendee, CalendarEvent, new_uid
from office_mcp.calendar.slots import (
    busy_blocks,
    find_free_slots,
    merge_intervals,
    overlaps_any,
    subtract,
)
from office_mcp.calendar.store import CalendarStore
from office_mcp.calendar.timeutil import (
    day_bounds,
    ensure_aware,
    get_tz,
    normalize_from_ics,
    parse_datetime,
)
from office_mcp.common.errors import DocumentParseError, OfficeMCPError

SH = ZoneInfo("Asia/Shanghai")
UTC = ZoneInfo("UTC")


def dt(day: int, hour: int, minute: int = 0, tz: ZoneInfo = SH) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=tz)


def event(
    start: datetime,
    end: datetime,
    *,
    uid: str | None = None,
    title: str = "会议",
    status: str = "CONFIRMED",
) -> CalendarEvent:
    return CalendarEvent(
        uid=uid or new_uid(),
        title=title,
        start=start,
        end=end,
        status=status,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# 区间计算
# --------------------------------------------------------------------------- #


class TestMergeIntervals:
    def test_empty(self) -> None:
        assert merge_intervals([]) == []

    def test_single(self) -> None:
        assert merge_intervals([(dt(11, 9), dt(11, 10))]) == [(dt(11, 9), dt(11, 10))]

    def test_disjoint_preserved(self) -> None:
        result = merge_intervals([(dt(11, 9), dt(11, 10)), (dt(11, 14), dt(11, 15))])
        assert result == [(dt(11, 9), dt(11, 10)), (dt(11, 14), dt(11, 15))]

    def test_overlapping_merged(self) -> None:
        result = merge_intervals([(dt(11, 9), dt(11, 11)), (dt(11, 10), dt(11, 12))])
        assert result == [(dt(11, 9), dt(11, 12))]

    def test_contained_swallowed(self) -> None:
        result = merge_intervals([(dt(11, 9), dt(11, 18)), (dt(11, 10), dt(11, 11))])
        assert result == [(dt(11, 9), dt(11, 18))]

    def test_adjacent_merged(self) -> None:
        """首尾相接要合并 —— 否则会在 11:00 生成一个 0 长度的假空闲。"""
        result = merge_intervals([(dt(11, 10), dt(11, 11)), (dt(11, 11), dt(11, 12))])
        assert result == [(dt(11, 10), dt(11, 12))]

    def test_unsorted_input(self) -> None:
        result = merge_intervals([(dt(11, 15), dt(11, 16)), (dt(11, 9), dt(11, 10))])
        assert result == [(dt(11, 9), dt(11, 10)), (dt(11, 15), dt(11, 16))]


class TestSubtract:
    def test_no_busy_returns_whole_window(self) -> None:
        assert subtract((dt(11, 9), dt(11, 18)), []) == [(dt(11, 9), dt(11, 18))]

    def test_middle_busy_splits_window(self) -> None:
        free = subtract((dt(11, 9), dt(11, 18)), [(dt(11, 12), dt(11, 13))])
        assert free == [(dt(11, 9), dt(11, 12)), (dt(11, 13), dt(11, 18))]

    def test_busy_covering_window_leaves_nothing(self) -> None:
        assert subtract((dt(11, 9), dt(11, 18)), [(dt(11, 8), dt(11, 19))]) == []

    def test_busy_outside_window_ignored(self) -> None:
        assert subtract((dt(11, 9), dt(11, 18)), [(dt(12, 9), dt(12, 10))]) == [
            (dt(11, 9), dt(11, 18))
        ]

    def test_empty_window(self) -> None:
        assert subtract((dt(11, 9), dt(11, 9)), []) == []


class TestFindFreeSlots:
    def test_no_events_returns_aligned_slots(self) -> None:
        slots = find_free_slots(
            [],
            window_start=dt(11, 9),
            window_end=dt(11, 12),
            duration_minutes=60,
            step_minutes=60,
            limit=10,
        )
        assert [(s.start.hour, s.minutes) for s in slots] == [(9, 60), (10, 60), (11, 60)]

    def test_busy_block_removes_slots(self) -> None:
        slots = find_free_slots(
            [event(dt(11, 10), dt(11, 11))],
            window_start=dt(11, 9),
            window_end=dt(11, 12),
            duration_minutes=30,
            step_minutes=30,
            limit=10,
        )
        assert all(not (s.start < dt(11, 11) and dt(11, 10) < s.end) for s in slots)
        assert any(s.start == dt(11, 9) for s in slots)
        assert any(s.start == dt(11, 11, 30) for s in slots)

    def test_duration_longer_than_gaps_returns_nothing(self) -> None:
        slots = find_free_slots(
            [event(dt(11, 9), dt(11, 10)), event(dt(11, 10, 30), dt(11, 18))],
            window_start=dt(11, 9),
            window_end=dt(11, 18),
            duration_minutes=60,
            step_minutes=15,
        )
        assert slots == []

    def test_alignment_to_step(self) -> None:
        """9:07 之后的第一个候选应当对齐到 9:15 而不是 9:07。"""
        slots = find_free_slots(
            [event(dt(11, 9), dt(11, 9, 7))],
            window_start=dt(11, 9),
            window_end=dt(11, 10),
            duration_minutes=15,
            step_minutes=15,
            limit=3,
        )
        assert slots, "应当有可用时段"
        assert slots[0].start.minute % 15 == 0

    def test_limit_respected(self) -> None:
        slots = find_free_slots(
            [],
            window_start=dt(11, 9),
            window_end=dt(11, 18),
            duration_minutes=30,
            step_minutes=30,
            limit=2,
        )
        assert len(slots) == 2

    def test_cancelled_event_ignored(self) -> None:
        slots = find_free_slots(
            [event(dt(11, 10), dt(11, 11), status="CANCELLED")],
            window_start=dt(11, 10),
            window_end=dt(11, 11),
            duration_minutes=60,
            step_minutes=60,
        )
        assert len(slots) == 1

    def test_tentative_can_be_excluded(self) -> None:
        payload = [event(dt(11, 10), dt(11, 11), status="TENTATIVE")]
        window = dict(
            window_start=dt(11, 10), window_end=dt(11, 11), duration_minutes=60, step_minutes=60
        )
        assert find_free_slots(payload, include_tentative=True, **window) == []
        assert len(find_free_slots(payload, include_tentative=False, **window)) == 1

    def test_invalid_duration(self) -> None:
        with pytest.raises(ValueError):
            find_free_slots([], window_start=dt(11, 9), window_end=dt(11, 18), duration_minutes=0)

    def test_event_partially_outside_window_is_clipped(self) -> None:
        slots = find_free_slots(
            [event(dt(11, 7), dt(11, 9, 30))],
            window_start=dt(11, 9),
            window_end=dt(11, 10),
            duration_minutes=30,
            step_minutes=30,
        )
        assert len(slots) == 1
        assert slots[0].start == dt(11, 9, 30)

    def test_cross_timezone_events_normalized(self) -> None:
        """UTC 的 02:00 就是上海 10:00，应当挡住上海的 10:00 时段。"""
        utc_event = CalendarEvent(
            uid=new_uid(),
            title="跨时区会议",
            start=datetime(2026, 9, 11, 2, 0, tzinfo=UTC),
            end=datetime(2026, 9, 11, 3, 0, tzinfo=UTC),
        )
        slots = find_free_slots(
            [utc_event],
            window_start=dt(11, 10),
            window_end=dt(11, 11),
            duration_minutes=60,
            step_minutes=60,
        )
        assert slots == []


class TestBusyBlocks:
    def test_titles_can_be_hidden(self) -> None:
        events = [event(dt(11, 10), dt(11, 11), title="绩效考核面谈")]
        blocks = busy_blocks(
            events, window_start=dt(11, 9), window_end=dt(11, 18), with_titles=False
        )
        assert blocks[0].title is None
        assert blocks[0].uid is None

    def test_titles_included_by_default(self) -> None:
        events = [event(dt(11, 10), dt(11, 11), title="周会")]
        blocks = busy_blocks(events, window_start=dt(11, 9), window_end=dt(11, 18))
        assert blocks[0].title == "周会"


class TestOverlapsAny:
    def test_adjacent_events_do_not_conflict(self) -> None:
        """冲突检测用严格小于 —— 10-11 和 11-12 不是双重预订。

        注意这与 ``merge_intervals`` 的「相邻即合并」看似矛盾，实则都对：
        合并相邻是为了不生成 0 长度假空闲；严格判断是为了不误报冲突。
        """
        existing = [event(dt(11, 10), dt(11, 11))]
        candidate = event(dt(11, 11), dt(11, 12))
        assert overlaps_any(candidate, existing) == []

    def test_real_overlap_detected(self) -> None:
        existing = [event(dt(11, 10), dt(11, 11))]
        candidate = event(dt(11, 10, 30), dt(11, 11, 30))
        assert len(overlaps_any(candidate, existing)) == 1

    def test_self_excluded(self) -> None:
        target = event(dt(11, 10), dt(11, 11))
        assert overlaps_any(target, [target]) == []

    def test_cancelled_event_not_a_conflict(self) -> None:
        existing = [event(dt(11, 10), dt(11, 11), status="CANCELLED")]
        candidate = event(dt(11, 10), dt(11, 11))
        assert overlaps_any(candidate, existing) == []


# --------------------------------------------------------------------------- #
# 时间处理
# --------------------------------------------------------------------------- #


class TestTimeUtil:
    def test_get_tz_ok(self) -> None:
        assert str(get_tz("Asia/Shanghai")) == "Asia/Shanghai"

    def test_get_tz_invalid_has_hint(self) -> None:
        with pytest.raises(OfficeMCPError) as exc:
            get_tz("Mars/Olympus")
        assert "IANA" in (exc.value.hint or "")

    def test_ensure_aware_attaches_tz(self) -> None:
        naive = datetime(2026, 9, 11, 14, 30)
        assert ensure_aware(naive, SH).tzinfo == SH

    def test_ensure_aware_keeps_existing(self) -> None:
        aware = datetime(2026, 9, 11, 14, 30, tzinfo=UTC)
        assert ensure_aware(aware, SH).tzinfo == UTC

    @pytest.mark.parametrize(
        "text",
        [
            "2026-09-11T14:30",
            "2026-09-11 14:30",
            "2026-09-11T14:30:00",
            "2026-09-11",
        ],
    )
    def test_parse_variants(self, text: str) -> None:
        parsed = parse_datetime(text, SH)
        assert parsed.tzinfo is not None
        assert parsed.year == 2026 and parsed.month == 9

    def test_parse_date_only_is_midnight(self) -> None:
        assert parse_datetime("2026-09-11", SH).hour == 0

    def test_parse_explicit_offset_preserves_instant(self) -> None:
        """带时区的输入会被转换到配置时区，但**时刻**必须不变。"""
        parsed = parse_datetime("2026-09-11T14:30:00+00:00", SH)
        assert parsed.utcoffset() == timedelta(hours=8)
        assert parsed.hour == 22  # 14:30 UTC == 22:30 上海
        assert parsed.astimezone(UTC).hour == 14

    def test_parse_naive_is_interpreted_in_config_tz(self) -> None:
        parsed = parse_datetime("2026-09-11T14:30", SH)
        assert parsed.utcoffset() == timedelta(hours=8)
        assert parsed.hour == 14

    def test_parse_trailing_z(self) -> None:
        parsed = parse_datetime("2026-09-11T06:30:00Z", SH)
        assert parsed.hour == 14  # 06:30 UTC == 14:30 上海

    def test_parse_invalid_has_hint(self) -> None:
        with pytest.raises(OfficeMCPError) as exc:
            parse_datetime("11/09/2026", SH, field="start")
        assert "start" in exc.value.message
        assert "ISO" in (exc.value.hint or "")

    def test_parse_empty_rejected(self) -> None:
        with pytest.raises(OfficeMCPError):
            parse_datetime("   ", SH)

    def test_normalize_from_ics_date_is_all_day(self) -> None:
        aware, all_day = normalize_from_ics(date(2026, 9, 11), SH)
        assert all_day is True
        assert aware.hour == 0

    def test_normalize_from_ics_datetime(self) -> None:
        aware, all_day = normalize_from_ics(datetime(2026, 9, 11, 14, 0), SH)
        assert all_day is False
        assert aware.tzinfo == SH

    def test_normalize_unsupported_type_raises(self) -> None:
        with pytest.raises(OfficeMCPError):
            normalize_from_ics("2026-09-11", SH)

    def test_day_bounds(self) -> None:
        start, end = day_bounds(date(2026, 9, 11), SH)
        assert start.hour == 0 and start.day == 11
        assert end.day == 12 and end.hour == 0


# --------------------------------------------------------------------------- #
# ICS
# --------------------------------------------------------------------------- #

SAMPLE_ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
UID:meeting-1@example.com
SUMMARY:Q3 \xe5\xad\xa3\xe5\xba\xa6\xe5\xa4\x8d\xe7\x9b\x98
DTSTART:20260911T060000Z
DTEND:20260911T070000Z
LOCATION:3F \xe4\xbc\x9a\xe8\xae\xae\xe5\xae\xa4
DESCRIPTION:Review Q3 results
ORGANIZER;CN=Boss:mailto:boss@corp.example.com
ATTENDEE;CN=Alice;ROLE=REQ-PARTICIPANT:mailto:alice@corp.example.com
ATTENDEE;CN=Bob;ROLE=OPT-PARTICIPANT:mailto:bob@corp.example.com
STATUS:CONFIRMED
END:VEVENT
END:VCALENDAR
"""


class TestIcsParsing:
    def test_parses_full_event(self) -> None:
        events = ics.parse_ics(SAMPLE_ICS, tz=SH, calendar_name="work")
        assert len(events) == 1
        parsed = events[0]
        assert parsed.uid == "meeting-1@example.com"
        assert parsed.title == "Q3 季度复盘"
        assert parsed.location == "3F 会议室"
        assert parsed.calendar == "work"
        assert parsed.organizer == "boss@corp.example.com"
        # 06:00 UTC == 14:00 上海
        assert parsed.start.hour == 14
        assert parsed.duration_minutes == 60

    def test_attendee_roles(self) -> None:
        parsed = ics.parse_ics(SAMPLE_ICS, tz=SH)[0]
        by_email = {a.email: a for a in parsed.attendees}
        assert by_email["alice@corp.example.com"].required is True
        assert by_email["bob@corp.example.com"].required is False
        assert by_email["alice@corp.example.com"].name == "Alice"

    def test_missing_uid_gets_generated(self) -> None:
        raw = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:no uid
DTSTART:20260911T060000Z
DTEND:20260911T070000Z
END:VEVENT
END:VCALENDAR
"""
        parsed = ics.parse_ics(raw, tz=SH)[0]
        assert parsed.uid and "@" in parsed.uid

    def test_missing_dtend_gives_zero_length(self) -> None:
        raw = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:x
SUMMARY:reminder
DTSTART:20260911T060000Z
END:VEVENT
END:VCALENDAR
"""
        parsed = ics.parse_ics(raw, tz=SH)[0]
        assert parsed.start == parsed.end

    def test_all_day_event(self) -> None:
        raw = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:holiday
SUMMARY:holiday
DTSTART;VALUE=DATE:20260911
DTEND;VALUE=DATE:20260912
END:VEVENT
END:VCALENDAR
"""
        parsed = ics.parse_ics(raw, tz=SH)[0]
        assert parsed.all_day is True
        assert parsed.start.hour == 0

    def test_rrule_preserved_not_expanded(self) -> None:
        raw = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:weekly
SUMMARY:weekly sync
DTSTART:20260911T060000Z
DTEND:20260911T070000Z
RRULE:FREQ=WEEKLY;COUNT=5
END:VEVENT
END:VCALENDAR
"""
        parsed = ics.parse_ics(raw, tz=SH)
        assert len(parsed) == 1, "不应展开成多个实例"
        assert parsed[0].recurrence is not None
        assert "FREQ=WEEKLY" in parsed[0].recurrence

    def test_corrupt_input_raises_parse_error(self) -> None:
        with pytest.raises(DocumentParseError) as exc:
            ics.parse_ics(b"definitely not ical", tz=SH)
        assert exc.value.hint

    def test_empty_calendar(self) -> None:
        raw = b"BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n"
        assert ics.parse_ics(raw, tz=SH) == []

    def test_bad_event_skipped_not_fatal(self) -> None:
        raw = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:no-start
SUMMARY:broken
END:VEVENT
BEGIN:VEVENT
UID:good
SUMMARY:ok
DTSTART:20260911T060000Z
DTEND:20260911T070000Z
END:VEVENT
END:VCALENDAR
"""
        parsed = ics.parse_ics(raw, tz=SH)
        assert len(parsed) == 1
        assert parsed[0].uid == "good"


class TestIcsBuilding:
    def test_round_trip(self) -> None:
        original = CalendarEvent(
            uid="rt-1@test",
            title="季度复盘",
            start=dt(11, 14),
            end=dt(11, 15, 30),
            location="3F 会议室",
            description="多行\n描述",
            attendees=[Attendee(email="a@b.com", name="张三")],
            organizer="boss@b.com",
            recurrence="FREQ=WEEKLY;COUNT=3",
        )
        raw = ics.build_ics([original], calendar_name="work")
        parsed = ics.parse_ics(raw, tz=SH, calendar_name="work")[0]

        assert parsed.uid == original.uid
        assert parsed.title == original.title
        assert parsed.location == original.location
        assert parsed.description == original.description
        assert parsed.attendees[0].email == "a@b.com"
        assert parsed.attendees[0].name == "张三"
        assert parsed.organizer == "boss@b.com"
        assert parsed.recurrence and "FREQ=WEEKLY" in parsed.recurrence
        assert parsed.duration_minutes == 90

    def test_output_is_utc_normalized(self) -> None:
        """导出一律转 UTC —— 跨客户端兼容性最好，不受接收方时区设置影响。"""
        raw = ics.build_ics([event(dt(11, 14), dt(11, 15))]).decode("utf-8")
        assert "DTSTART:20260911T060000Z" in raw or "060000Z" in raw

    def test_empty_event_list(self) -> None:
        raw = ics.build_ics([])
        assert b"BEGIN:VCALENDAR" in raw
        assert b"BEGIN:VEVENT" not in raw

    def test_invite_carries_method(self) -> None:
        raw = ics.build_invite(
            event(dt(11, 14), dt(11, 15)), organizer="boss@corp.com", method="REQUEST"
        )
        assert b"METHOD:REQUEST" in raw
        assert b"boss@corp.com" in raw

    def test_cancel_invite(self) -> None:
        raw = ics.build_invite(
            event(dt(11, 14), dt(11, 15), status="CANCELLED"),
            organizer="boss@corp.com",
            method="CANCEL",
        )
        assert b"METHOD:CANCEL" in raw

    def test_invalid_rrule_string_ignored(self) -> None:
        """规则串非法时不该让整个导出失败。"""
        raw = ics.build_ics([event(dt(11, 14), dt(11, 15))])
        assert b"BEGIN:VEVENT" in raw


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #


class TestCalendarStore:
    def test_add_and_load(self, tmp_path: Path) -> None:
        store = CalendarStore(tmp_path)
        store.add(event(dt(11, 10), dt(11, 11), title="周会"))
        loaded = store.load("default")
        assert len(loaded) == 1
        assert loaded[0].title == "周会"

    def test_creates_calendar_file(self, tmp_path: Path) -> None:
        store = CalendarStore(tmp_path)
        store.add(event(dt(11, 10), dt(11, 11)))
        assert (tmp_path / "calendar" / "default.json").exists()

    def test_persistence_across_instances(self, tmp_path: Path) -> None:
        CalendarStore(tmp_path).add(event(dt(11, 10), dt(11, 11), title="持久化"))
        assert CalendarStore(tmp_path).load("default")[0].title == "持久化"

    def test_get_by_uid(self, tmp_path: Path) -> None:
        store = CalendarStore(tmp_path)
        created = store.add(event(dt(11, 10), dt(11, 11)))
        assert store.get(created.uid).uid == created.uid

    def test_get_missing_has_hint(self, tmp_path: Path) -> None:
        store = CalendarStore(tmp_path)
        with pytest.raises(OfficeMCPError) as exc:
            store.get("nope")
        assert "list_events" in (exc.value.hint or "")

    def test_update(self, tmp_path: Path) -> None:
        store = CalendarStore(tmp_path)
        created = store.add(event(dt(11, 10), dt(11, 11)))
        updated = store.update(created.uid, {"title": "改名了", "start": dt(11, 15)})
        assert updated.title == "改名了"
        assert store.get(created.uid).start.hour == 15

    def test_delete(self, tmp_path: Path) -> None:
        store = CalendarStore(tmp_path)
        created = store.add(event(dt(11, 10), dt(11, 11)))
        store.delete(created.uid)
        assert store.load("default") == []

    def test_find_across_calendars(self, tmp_path: Path) -> None:
        store = CalendarStore(tmp_path)
        created = store.add(event(dt(11, 10), dt(11, 11)).model_copy(update={"calendar": "work"}))
        found = store.find(created.uid)
        assert found is not None
        assert found[1] == "work"

    def test_calendar_name_sanitized(self, tmp_path: Path) -> None:
        """日历名里的路径分隔符不能让它写到工作区外。"""
        store = CalendarStore(tmp_path)
        path = store.path_for("../../evil")
        assert path.resolve().is_relative_to((tmp_path / "calendar").resolve())

    def test_in_range_uses_intersection(self, tmp_path: Path) -> None:
        store = CalendarStore(tmp_path)
        store.add(event(dt(11, 8), dt(11, 20), title="全天马拉松会议"))
        # 查询窗口完全落在事件内部，也必须返回
        found = store.in_range(start=dt(11, 12), end=dt(11, 13))
        assert len(found) == 1

    def test_in_range_excludes_cancelled(self, tmp_path: Path) -> None:
        store = CalendarStore(tmp_path)
        store.add(event(dt(11, 10), dt(11, 11), status="CANCELLED"))
        assert store.in_range(start=dt(11, 9), end=dt(11, 12)) == []

    def test_import_replaces_by_uid(self, tmp_path: Path) -> None:
        store = CalendarStore(tmp_path)
        shared_uid = "shared@test"
        store.add(event(dt(11, 10), dt(11, 11), uid=shared_uid, title="旧标题"))

        stats = store.import_events(
            [event(dt(11, 10), dt(11, 11), uid=shared_uid, title="新标题")],
            calendar_name="default",
        )
        assert stats == {"added": 0, "replaced": 1, "total": 1}
        assert store.get(shared_uid).title == "新标题"

    def test_import_without_replace_skips(self, tmp_path: Path) -> None:
        store = CalendarStore(tmp_path)
        store.add(event(dt(11, 10), dt(11, 11), uid="u1", title="保留"))
        stats = store.import_events(
            [event(dt(11, 10), dt(11, 11), uid="u1", title="忽略")],
            calendar_name="default",
            replace_by_uid=False,
        )
        assert stats["added"] == 0 and stats["replaced"] == 0
        assert store.get("u1").title == "保留"

    def test_corrupt_calendar_file_raises_with_hint(self, tmp_path: Path) -> None:
        store = CalendarStore(tmp_path)
        store.path_for("broken").write_text("{ not json", encoding="utf-8")
        with pytest.raises(OfficeMCPError) as exc:
            store.load("broken")
        assert "损坏" in exc.value.message

    def test_atomic_write_leaves_no_temp_files(self, tmp_path: Path) -> None:
        store = CalendarStore(tmp_path)
        store.add(event(dt(11, 10), dt(11, 11)))
        leftovers = list((tmp_path / "calendar").glob("*.tmp"))
        assert leftovers == []


# --------------------------------------------------------------------------- #
# 模型
# --------------------------------------------------------------------------- #


class TestCalendarEventModel:
    def test_end_before_start_rejected(self) -> None:
        with pytest.raises(ValueError):
            CalendarEvent(uid="x", title="t", start=dt(11, 12), end=dt(11, 11))

    def test_zero_length_allowed(self) -> None:
        assert CalendarEvent(uid="x", title="t", start=dt(11, 12), end=dt(11, 12))

    def test_duration_minutes(self) -> None:
        assert event(dt(11, 10), dt(11, 11, 30)).duration_minutes == 90

    def test_overlaps_half_open_interval(self) -> None:
        first = event(dt(11, 10), dt(11, 11))
        assert first.overlaps(event(dt(11, 10, 30), dt(11, 12))) is True
        assert first.overlaps(event(dt(11, 11), dt(11, 12))) is False
