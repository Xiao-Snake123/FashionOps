"""审计日志。

每个工具调用写一行 JSONL：

.. code-block:: json

    {"ts":"2026-09-11T10:00:00.123Z","event":"tool_call","tool":"write_docx",
     "args":{"path":"report.docx"},"status":"ok","duration_ms":42.7,
     "trace_id":"3f2a...","actor":"mcp-client"}

设计要点：
- **脱敏**：键名命中 ``audit_redact_keys`` 的参数值替换为 ``***``，且做递归处理。
- **截断**：超长字符串只保留前 N 字符，避免把整篇文档写进日志。
- **失败不阻断**：审计写失败不能让业务失败（best-effort），但会打警告。
- **trace_id**：与 Agent 侧链路追踪对齐，便于把 Agent 的一次决策与工具调用串起来。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from office_mcp.common.config import Settings, get_settings

logger = logging.getLogger(__name__)

_MAX_STR_LEN = 500
_MAX_CONTAINER_ITEMS = 50


class AuditLogger:
    """线程安全的 JSONL 审计写入器。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._path: Path | None = self._settings.audit_log
        self._redact = {k.lower() for k in self._settings.audit_redact_keys}
        self._lock = threading.Lock()
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self._path is not None

    # ---- 写入 ----

    def log(self, event: str, **fields: Any) -> None:
        if self._path is None:
            return
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "event": event,
            "pid": os.getpid(),
            "trace_id": current_trace_id(),
            **{k: _sanitize(v, self._redact, key=k) for k, v in fields.items()},
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        try:
            with self._lock, self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:  # pragma: no cover - 磁盘异常
            logger.warning("审计日志写入失败（不影响业务）: %s", exc)

    @contextmanager
    def tool_call(self, tool: str, **args: Any) -> Iterator[dict[str, Any]]:
        """工具调用的上下文管理器，自动记录耗时与结果状态。

        用法::

            with audit.tool_call("write_docx", path=str(target)) as ctx:
                ...
                ctx["status"] = "ok"
        """
        started = time.perf_counter()
        ctx: dict[str, Any] = {"status": "error"}
        try:
            yield ctx
        except Exception as exc:
            ctx["status"] = "error"
            ctx["error_type"] = type(exc).__name__
            self.log(
                "tool_call",
                tool=tool,
                args=args,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                **ctx,
            )
            raise
        else:
            self.log(
                "tool_call",
                tool=tool,
                args=args,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                **ctx,
            )


# --------------------------------------------------------------------------- #
# 链路追踪
# --------------------------------------------------------------------------- #

_trace_local = threading.local()


def set_trace_id(trace_id: str | None) -> None:
    """设置当前线程的 trace id（由 MCP 层从请求头/上下文注入）。"""
    _trace_local.trace_id = trace_id


def current_trace_id() -> str | None:
    return getattr(_trace_local, "trace_id", None)


@contextmanager
def trace_scope(trace_id: str | None) -> Iterator[None]:
    previous = current_trace_id()
    set_trace_id(trace_id)
    try:
        yield
    finally:
        set_trace_id(previous)


# --------------------------------------------------------------------------- #
# 脱敏
# --------------------------------------------------------------------------- #


def _sanitize(value: Any, redact: set[str], *, key: str | None = None, depth: int = 0) -> Any:
    if key is not None and key.lower() in redact:
        return "***"
    if depth > 6:
        return "<max-depth>"
    if isinstance(value, str):
        return value if len(value) <= _MAX_STR_LEN else f"{value[:_MAX_STR_LEN]}…<truncated>"
    if isinstance(value, Mapping):
        items = list(value.items())[:_MAX_CONTAINER_ITEMS]
        return {str(k): _sanitize(v, redact, key=str(k), depth=depth + 1) for k, v in items}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(v, redact, depth=depth + 1) for v in list(value)[:_MAX_CONTAINER_ITEMS]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _sanitize(str(value), redact, depth=depth + 1)


@lru_cache(maxsize=1)
def get_audit_logger() -> AuditLogger:
    return AuditLogger()
