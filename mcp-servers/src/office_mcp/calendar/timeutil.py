"""时间处理工具。

日历场景最大的坑是**时区歧义**，所以这里把"任何进入系统的时间都必须带时区"
这条规则收口到一处：

- 解析用户输入：若不带时区，按配置的默认时区补全（而不是留成 naive）
- 从 ICS 读取：``DTSTART`` 可能是 ``date``（全天）、naive ``datetime``（浮动时间）
  或带时区 ``datetime``，统一归一化成"带时区 datetime + all_day 标记"
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from office_mcp.common.errors import OfficeMCPError


def get_tz(name: str) -> ZoneInfo:
    """按名称取时区，取不到时抛出带 hint 的业务异常。"""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise OfficeMCPError(
            f"未知时区：{name}",
            hint="请使用 IANA 时区名，例如 Asia/Shanghai、UTC、America/New_York。",
        ) from exc


def ensure_aware(value: datetime, tz: ZoneInfo) -> datetime:
    """naive datetime 按 ``tz`` 解释；已带时区的原样返回。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return value


def parse_datetime(value: str, tz: ZoneInfo, *, field: str = "时间") -> datetime:
    """解析 ISO 8601 时间字符串，并**统一转换到 ``tz``**。

    支持 ``2026-09-11``（视为当天 00:00）、``2026-09-11T14:30``、
    ``2026-09-11T14:30:00+08:00``、``2026-09-11 14:30``、``2026-09-11T06:30:00Z``。

    为什么要把带时区的输入也转成 ``tz``：如果保留原时区，系统里会同时存在
    ``+00:00`` 和 ``+08:00`` 的事件，回给模型和用户的 ``isoformat()`` 就不一致，
    排查问题时很容易看错。比较运算本身没问题，但可读性代价更大。
    """
    text = (value or "").strip()
    if not text:
        raise OfficeMCPError(
            f"{field}不能为空。",
            hint="请提供 ISO 格式时间，例如 2026-09-11T14:30 或 2026-09-11。",
        )

    normalized = text.replace(" ", "T", 1) if " " in text and "T" not in text else text
    # 允许 Python 3.10 也能解析末尾的 Z
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed_date = date.fromisoformat(normalized)
        except ValueError as exc:
            raise OfficeMCPError(
                f"{field}格式不正确：{value}",
                hint=(
                    "请使用 ISO 格式，例如 2026-09-11T14:30、2026-09-11 14:30 "
                    "或 2026-09-11（全天）。"
                ),
            ) from exc
        parsed = datetime.combine(parsed_date, time.min)

    return ensure_aware(parsed, tz).astimezone(tz)


def normalize_from_ics(value: object, tz: ZoneInfo) -> tuple[datetime, bool]:
    """归一化 ICS 里的 ``DTSTART`` / ``DTEND`` 值。

    Returns:
        ``(带时区的 datetime, 是否全天)``
    """
    if isinstance(value, datetime):
        aware = ensure_aware(value, tz)
        return aware.astimezone(tz), False
    if isinstance(value, date):
        # 全天事件：ICS 用 DATE 类型表示，转成当地 00:00
        return datetime.combine(value, time.min, tzinfo=tz), True
    raise OfficeMCPError(
        f"无法解析日历时间：{value!r}",
        hint="该 ICS 文件的时间字段格式异常，请确认文件来源是否合法。",
    )


def to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """返回某一天的 ``[00:00, 次日 00:00)``。"""
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start, start + timedelta(days=1)
