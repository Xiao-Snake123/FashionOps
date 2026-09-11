"""日历模块的数据模型。

时间一律用**带时区**的 ``datetime``：
- 不带时区的时间在跨时区协作、夏令时切换时会产生歧义，日历是最不能容忍这种歧义的场景。
- 解析用户输入时若没带时区，按配置的 ``OFFICE_MCP_TIMEZONE`` 补全（默认 Asia/Shanghai）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

EventStatus = Literal["CONFIRMED", "TENTATIVE", "CANCELLED"]

#: UID 统一带域名后缀，符合 RFC 5545 建议，也便于排查时看出是本系统生成的
UID_DOMAIN = "office-mcp"


def new_uid() -> str:
    """生成一个全局唯一的事件 ID。"""
    return f"{uuid4()}@{UID_DOMAIN}"


class Attendee(BaseModel):
    """参会人。"""

    email: str = Field(description="邮箱地址")
    name: str | None = Field(default=None, description="显示名")
    required: bool = Field(default=True, description="是否必选（false 表示可选参加）")


class CalendarEvent(BaseModel):
    """一个日历事件。"""

    uid: str = Field(description="全局唯一 ID，更新/删除时用它定位")
    title: str = Field(description="事件标题")
    start: datetime = Field(description="开始时间（带时区）")
    end: datetime = Field(description="结束时间（带时区）")
    all_day: bool = Field(default=False, description="是否全天事件")
    location: str | None = Field(default=None, description="地点")
    description: str | None = Field(default=None, description="描述")
    attendees: list[Attendee] = Field(default_factory=list, description="参会人")
    calendar: str = Field(default="default", description="所属日历名")
    status: EventStatus = Field(default="CONFIRMED", description="事件状态")
    organizer: str | None = Field(default=None, description="组织者邮箱")
    recurrence: str | None = Field(
        default=None,
        description="RRULE 重复规则原文（如 ``FREQ=WEEKLY;COUNT=5``）。本模块不展开重复实例。",
    )
    created_at: datetime | None = Field(default=None, description="创建时间")
    updated_at: datetime | None = Field(default=None, description="最后修改时间")

    @model_validator(mode="after")
    def _check_range(self) -> CalendarEvent:
        if self.end < self.start:
            raise ValueError("结束时间不能早于开始时间")
        return self

    @property
    def duration_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    def overlaps(self, other: CalendarEvent) -> bool:
        """两个事件是否时间重叠（半开区间：[start, end)）。"""
        return self.start < other.end and other.start < self.end


class FreeSlot(BaseModel):
    """一段可用空闲时间。"""

    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


class BusyBlock(BaseModel):
    """一段忙碌时间（由事件裁剪而来，不含标题等敏感信息时可只回时间）。"""

    start: datetime
    end: datetime
    title: str | None = None
    uid: str | None = None
