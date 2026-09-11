"""审计：每次工具调用落一条记录。

## 三条原则

**1. 审计失败绝不阻断业务**

审计是"旁路"，不是"主路"。如果写审计库失败就让整个工具调用失败，
那是把可观测性问题的代价转嫁给了用户请求。所以这里所有写操作都
吞掉异常，只打 warning。这条原则是从 ``mcp-servers/common/audit.py`` 带过来的。

**2. 入参必须脱敏后入库**

工具入参里经常夹着密码、token、手机号、身份证号。
原样落库等于把敏感信息复制到一张访问控制更弱的表里。
所以走一遍 ``sanitize()``：按键名匹配敏感词，递归替换成 ``***``。

**3. 长内容要截断**

入参里可能有整篇商品描述或者一段 prompt。不截断的话审计表会被
几条大记录撑爆，查询也变慢。这里对每个字符串和整个 JSON 都设了上限。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger("fashionops.audit")

#: 键名命中这些词就脱敏（大小写不敏感，做子串匹配）
SENSITIVE_KEYWORDS: tuple[str, ...] = (
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "authorization", "auth", "credential", "private_key", "access_key",
    "session", "cookie", "signature", "mobile", "phone", "id_card", "idcard",
)

MASK = "***"

#: 单条字符串最大入库长度
MAX_STR_LEN = 300
#: 整个 args JSON 的最大入库长度
MAX_JSON_LEN = 4000


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(word in lowered for word in SENSITIVE_KEYWORDS)


def sanitize(value: Any, *, depth: int = 0) -> Any:
    """递归脱敏 + 截断。

    ``depth`` 上限 6 层：防御性地挡住自引用结构（理论上 JSON 入参不会有环，
    但工具的返回值可能是从别处拼出来的，小心一点不会错）。
    """
    if depth > 6:
        return "<max-depth>"

    if isinstance(value, dict):
        return {
            str(k): (MASK if _is_sensitive(str(k)) else sanitize(v, depth=depth + 1))
            for k, v in list(value.items())[:50]  # 字段太多也截断
        }

    if isinstance(value, (list, tuple)):
        items = [sanitize(v, depth=depth + 1) for v in list(value)[:20]]
        if len(value) > 20:
            items.append(f"<还有 {len(value) - 20} 项>")
        return items

    if isinstance(value, str):
        if len(value) > MAX_STR_LEN:
            return value[:MAX_STR_LEN] + f"…<截断 {len(value) - MAX_STR_LEN} 字>"
        return value

    if isinstance(value, (int, float, bool)) or value is None:
        return value

    # Decimal / datetime / Pydantic 模型等统一转成字符串，保证可 JSON 序列化
    return sanitize(str(value), depth=depth + 1)


def summarize_result(data: Any) -> str | None:
    """把返回值压缩成一行摘要，给审计表用。

    完整返回值可能很大，审计里只需要"大概返回了什么"。
    """
    if data is None:
        return None

    if isinstance(data, dict):
        keys = list(data.keys())
        head = ", ".join(keys[:6])
        more = f" …(+{len(keys) - 6})" if len(keys) > 6 else ""
        return f"dict({len(keys)} 键): {head}{more}"

    if isinstance(data, (list, tuple)):
        return f"list({len(data)} 项)"

    if isinstance(data, str):
        return f"str({len(data)} 字)"

    return f"{type(data).__name__}: {str(data)[:80]}"


@dataclass
class AuditEntry:
    """一条审计记录。字段对齐 ``audit_logs`` 表。"""

    actor: str
    tool_name: str
    status: str  # success / error / blocked
    trace_id: str = ""
    task_id: int | None = None
    action: str | None = None
    resource: str | None = None
    args_digest: dict[str, Any] = field(default_factory=dict)
    result_summary: str | None = None
    error_code: str | None = None
    duration_ms: int | None = None
    ip: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id or None,
            "task_id": self.task_id,
            "actor": self.actor,
            "tool_name": self.tool_name,
            "action": self.action,
            "resource": self.resource,
            "args_digest": self.args_digest or None,
            "result_summary": self.result_summary,
            "status": self.status,
            "error_code": self.error_code,
            "duration_ms": self.duration_ms,
            "ip": self.ip,
        }


class AuditSink(Protocol):
    def record(self, entry: AuditEntry) -> None: ...


class MemoryAuditSink:
    """收在内存里，单测断言用。"""

    def __init__(self, limit: int = 2000) -> None:
        self.entries: list[AuditEntry] = []
        self._limit = limit

    def record(self, entry: AuditEntry) -> None:
        self.entries.append(entry)
        if len(self.entries) > self._limit:
            # 防止跑长任务时把内存吃光
            del self.entries[: len(self.entries) - self._limit]


class SqlAuditSink:
    """写 ``audit_logs`` 表。

    每次调用开一个 Session —— 在 Demo 规模下完全够用。
    真上量的话应该改成队列 + 批量插入：审计不该和业务请求抢数据库连接。
    """

    def __init__(self, session_factory=None) -> None:  # noqa: ANN001
        self._session_factory = session_factory

    def _factory(self):  # noqa: ANN202
        if self._session_factory is None:
            from fashionops.db.base import get_session_factory

            self._session_factory = get_session_factory()
        return self._session_factory

    def record(self, entry: AuditEntry) -> None:
        try:
            from fashionops.db.models import AuditLog

            # 再压一道：入库前确认整个 args 能序列化且不超长
            digest = entry.args_digest
            blob = json.dumps(digest, ensure_ascii=False, default=str)
            if len(blob) > MAX_JSON_LEN:
                digest = {"_truncated": True, "_preview": blob[:MAX_JSON_LEN]}

            session = self._factory()()
            try:
                session.add(AuditLog(**{**entry.to_row(), "args_digest": digest}))
                session.commit()
            finally:
                session.close()
        except Exception as exc:  # noqa: BLE001
            # 一定要吞掉：审计写失败不能影响工具调用本身
            logger.warning(
                "审计写入失败（已忽略，不影响业务）：tool=%s err=%s: %s",
                entry.tool_name,
                type(exc).__name__,
                exc,
            )


class FanoutAuditSink:
    """一条记录发给多个 sink。

    用来同时做"落库"和"内存统计"，或者将来接 Kafka。
    任何一个 sink 失败都不影响其他 sink。
    """

    def __init__(self, *sinks: AuditSink) -> None:
        self._sinks = list(sinks)

    def add(self, sink: AuditSink) -> None:
        self._sinks.append(sink)

    def record(self, entry: AuditEntry) -> None:
        for sink in self._sinks:
            try:
                sink.record(entry)
            except Exception as exc:  # noqa: BLE001
                logger.warning("审计 sink %s 失败：%s", type(sink).__name__, exc)
