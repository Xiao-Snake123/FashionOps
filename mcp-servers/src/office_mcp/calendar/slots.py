"""空闲时间计算。

这是日历模块里唯一有算法含量的部分，也是面试常问的区间类问题。

核心思路：
1. 把所有忙碌事件**按时区统一**后裁剪到查询窗口内
2. 归并重叠区间（O(n log n)）
3. 从窗口里"减去"忙碌区间，得到空闲区间
4. 按粒度对齐候选开始时间，过滤掉时长不够的

边界约定统一为**左闭右开** ``[start, end)``：
相邻的两个会议（10:00–11:00 与 11:00–12:00）不算冲突，
这是日历场景下唯一合理的约定。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from office_mcp.calendar.models import BusyBlock, CalendarEvent, FreeSlot

Interval = tuple[datetime, datetime]


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """归并重叠或相邻的区间，返回按开始时间升序的互不重叠列表。"""
    if not intervals:
        return []

    ordered = sorted(intervals, key=lambda item: item[0])
    merged: list[Interval] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        # 用 <= 而不是 <：把首尾相接的区间也合并，
        # 这样 10:00-11:00 与 11:00-12:00 会合成一段 10:00-12:00 的忙碌。
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def subtract(window: Interval, busy: list[Interval]) -> list[Interval]:
    """从 ``window`` 中减去 ``busy``，返回空闲区间列表。"""
    window_start, window_end = window
    if window_end <= window_start:
        return []

    free: list[Interval] = []
    cursor = window_start
    for start, end in merge_intervals(busy):
        if end <= cursor:
            continue
        if start >= window_end:
            break
        if start > cursor:
            free.append((cursor, min(start, window_end)))
        cursor = max(cursor, end)
        if cursor >= window_end:
            break
    if cursor < window_end:
        free.append((cursor, window_end))
    return free


def find_free_slots(
    events: list[CalendarEvent],
    *,
    window_start: datetime,
    window_end: datetime,
    duration_minutes: int,
    step_minutes: int = 15,
    limit: int = 20,
    include_tentative: bool = True,
) -> list[FreeSlot]:
    """在 ``[window_start, window_end)`` 内找出时长足够的空闲时段。

    Args:
        events: 已有的日历事件（会被裁剪到窗口内）。
        window_start / window_end: 查询窗口。
        duration_minutes: 需要的会议时长。
        step_minutes: 候选开始时间的对齐粒度（默认 15 分钟，让会议时间"整齐"）。
        limit: 最多返回多少段。
        include_tentative: 是否把 ``TENTATIVE``（待定）事件也算作忙碌。

    Returns:
        按开始时间升序的空闲时段列表。
    """
    if duration_minutes <= 0:
        raise ValueError("duration_minutes 必须为正数")
    if window_end <= window_start:
        return []

    busy: list[Interval] = []
    for event in events:
        if event.status == "CANCELLED":
            continue
        if event.status == "TENTATIVE" and not include_tentative:
            continue
        start = max(event.start, window_start)
        end = min(event.end, window_end)
        if end > start:
            busy.append((start, end))

    needed = timedelta(minutes=duration_minutes)
    step = timedelta(minutes=max(1, step_minutes))
    tz = window_start.tzinfo

    slots: list[FreeSlot] = []
    for free_start, free_end in subtract((window_start, window_end), busy):
        # 把候选开始时间对齐到 step 边界
        cursor = _align_up(free_start, step, tz)
        while cursor + needed <= free_end:
            slots.append(FreeSlot(start=cursor, end=cursor + needed))
            if len(slots) >= limit:
                return slots
            cursor += step
    return slots


def busy_blocks(
    events: list[CalendarEvent],
    *,
    window_start: datetime,
    window_end: datetime,
    with_titles: bool = True,
) -> list[BusyBlock]:
    """把事件裁剪成忙碌区间。

    对外暴露"某人什么时候忙"时通常**不需要给出会议标题**（涉及隐私），
    因此 ``with_titles=False`` 可以只返回时间段。
    """
    blocks: list[BusyBlock] = []
    for event in events:
        if event.status == "CANCELLED":
            continue
        start = max(event.start, window_start)
        end = min(event.end, window_end)
        if end <= start:
            continue
        blocks.append(
            BusyBlock(
                start=start,
                end=end,
                title=event.title if with_titles else None,
                uid=event.uid if with_titles else None,
            )
        )
    return sorted(blocks, key=lambda block: block.start)


def overlaps_any(candidate: CalendarEvent, events: list[CalendarEvent]) -> list[CalendarEvent]:
    """返回与 ``candidate`` 冲突的已有事件。新建/改期前用它做冲突检测。"""
    return [
        event
        for event in events
        if event.uid != candidate.uid and event.status != "CANCELLED" and candidate.overlaps(event)
    ]


def _align_up(moment: datetime, step: timedelta, tz: object) -> datetime:
    """把时间向上对齐到 ``step`` 的整数倍边界（相对当日零点）。"""
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = moment - midnight
    steps = -(-int(elapsed.total_seconds()) // int(step.total_seconds()))  # 向上取整
    return midnight + step * steps
