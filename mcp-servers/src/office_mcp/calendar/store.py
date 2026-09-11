"""本地日历存储。

用工作区内的一个 JSON 文件存事件（``calendar/<name>.json``）。
选文件而不是数据库的理由：日历数据量小、需要人工可读可备份、
且要能被 Agent 用文档工具直接打开检查。

写入走「临时文件 + 原子替换」，避免进程中断留下损坏的日历文件。
所有读写都加进程内锁，防止并发工具调用互相覆盖。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from office_mcp.calendar.models import CalendarEvent
from office_mcp.common.errors import OfficeMCPError

SCHEMA_VERSION = 1


class CalendarStore:
    """按日历名分文件的本地事件存储。"""

    def __init__(self, root: Path) -> None:
        self._root = Path(root) / "calendar"
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: dict[str, list[CalendarEvent]] = {}

    # ---- 文件路径 ----

    def path_for(self, calendar_name: str) -> Path:
        safe = _safe_calendar_name(calendar_name)
        return self._root / f"{safe}.json"

    def calendars(self) -> list[str]:
        return sorted(item.stem for item in self._root.glob("*.json"))

    # ---- 读写 ----

    def load(self, calendar_name: str = "default") -> list[CalendarEvent]:
        with self._lock:
            if calendar_name in self._cache:
                return list(self._cache[calendar_name])

            path = self.path_for(calendar_name)
            if not path.exists():
                self._cache[calendar_name] = []
                return []

            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise OfficeMCPError(
                    f"日历文件损坏，无法读取：{path.name}（{exc}）",
                    hint=f"请检查或备份并删除 {path} 后重新创建日历。",
                ) from exc

            events: list[CalendarEvent] = []
            for item in payload.get("events", []):
                try:
                    events.append(CalendarEvent.model_validate(item))
                except Exception:  # noqa: BLE001 - 单条坏了不该让整个日历打不开
                    continue
            self._cache[calendar_name] = events
            return list(events)

    def save(self, calendar_name: str, events: list[CalendarEvent]) -> None:
        with self._lock:
            path = self.path_for(calendar_name)
            payload = {
                "schema_version": SCHEMA_VERSION,
                "calendar": calendar_name,
                "updated_at": datetime.now().astimezone().isoformat(),
                "events": [event.model_dump(mode="json") for event in events],
            }
            _atomic_write_json(path, payload)
            self._cache[calendar_name] = list(events)

    # ---- 事件操作 ----

    def get(self, uid: str, calendar_name: str = "default") -> CalendarEvent:
        for event in self.load(calendar_name):
            if event.uid == uid:
                return event
        raise OfficeMCPError(
            f"未找到事件：{uid}",
            hint="请先用 list_events 查看该时间段内的事件，确认 uid 是否正确。",
        )

    def find(self, uid: str) -> tuple[CalendarEvent, str] | None:
        """跨所有日历查找事件，返回 ``(事件, 日历名)``。"""
        for name in self.calendars():
            for event in self.load(name):
                if event.uid == uid:
                    return event, name
        return None

    def add(self, event: CalendarEvent) -> CalendarEvent:
        with self._lock:
            events = self.load(event.calendar)
            now = datetime.now().astimezone()
            stored = event.model_copy(update={"created_at": now, "updated_at": now})
            events.append(stored)
            self.save(event.calendar, events)
            return stored

    def update(self, uid: str, changes: dict[str, object]) -> CalendarEvent:
        with self._lock:
            found = self.find(uid)
            if found is None:
                raise OfficeMCPError(
                    f"未找到事件：{uid}",
                    hint="请先用 list_events 确认事件 uid。",
                )
            event, calendar_name = found
            events = self.load(calendar_name)
            updated = event.model_copy(
                update={**changes, "updated_at": datetime.now().astimezone()}
            )
            self.save(
                calendar_name,
                [updated if item.uid == uid else item for item in events],
            )
            return updated

    def delete(self, uid: str) -> str:
        with self._lock:
            found = self.find(uid)
            if found is None:
                raise OfficeMCPError(f"未找到事件：{uid}", hint="请确认事件 uid。")
            _, calendar_name = found
            events = [item for item in self.load(calendar_name) if item.uid != uid]
            self.save(calendar_name, events)
            return calendar_name

    def import_events(
        self, events: list[CalendarEvent], *, calendar_name: str, replace_by_uid: bool = True
    ) -> dict[str, int]:
        """批量导入事件。相同 UID 默认覆盖（符合 ICS 导入的直觉：同一次会议是更新）。"""
        with self._lock:
            existing = {event.uid: event for event in self.load(calendar_name)}
            added = 0
            replaced = 0
            for event in events:
                payload = event.model_copy(update={"calendar": calendar_name})
                if payload.uid in existing:
                    if not replace_by_uid:
                        continue
                    replaced += 1
                else:
                    added += 1
                existing[payload.uid] = payload
            ordered = sorted(existing.values(), key=lambda item: item.start)
            self.save(calendar_name, ordered)
            return {"added": added, "replaced": replaced, "total": len(ordered)}

    # ---- 查询 ----

    def in_range(
        self,
        *,
        start: datetime,
        end: datetime,
        calendar_name: str | None = None,
    ) -> list[CalendarEvent]:
        """返回与 ``[start, end)`` 有交集的事件。

        注意是**交集**而不是包含：一个跨越查询窗口的长会议也必须返回，
        否则"这天有没有会"会算错。
        """
        names = [calendar_name] if calendar_name else (self.calendars() or ["default"])
        collected: list[CalendarEvent] = []
        for name in names:
            for event in self.load(name):
                if event.status == "CANCELLED":
                    continue
                if event.start < end and start < event.end:
                    collected.append(event)
                elif event.start == event.end and start <= event.start < end:
                    # 零时长事件（提醒）用点位判断
                    collected.append(event)
        return sorted(collected, key=lambda item: item.start)


def _safe_calendar_name(name: str) -> str:
    from office_mcp.common.paths import safe_filename

    return safe_filename(name or "default", default_ext="") or "default"


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".json")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
