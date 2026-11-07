"""工具网关的统一错误契约。

## 为什么错误要"契约化"

Agent 调工具失败时，最糟的处理是抛一个裸异常上去 —— 模型只会看到
`ToolException: ...` 这一句，既不知道能不能重试，也不知道下一步该干什么，
于是它要么重试到天荒地老，要么直接放弃。

所以这里把错误拆成四个明确的部分：

* ``code``      —— 机器可判别的错误类别，用来做统计和错误归因
* ``message``   —— 给人看的描述
* ``hint``      —— **给模型的下一步建议**（这个字段是这套设计的核心价值）
* ``retryable`` —— 明确告诉调用方该不该重试

``hint`` 不是客套话。举例：``RATE_LIMITED`` 的 hint 写成
"该工具已触发限流，请在 12 秒后重试，或改用 mock_erp.query_orders 拿到汇总数据"，
模型就有很大概率换一条路走完任务，而不是卡死在同一个调用上。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class ErrorCode(StrEnum):
    """错误类别。前 5 个是网关自己产生的，后 4 个是下游产生的。"""

    # ---- 网关层 ----
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RATE_LIMITED = "RATE_LIMITED"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"

    # ---- 下游 ----
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    SCHEMA_VIOLATION = "SCHEMA_VIOLATION"  # 返回值不符合声明的 schema
    INTERNAL_ERROR = "INTERNAL_ERROR"


#: 哪些错误重试有意义。
#:
#: 注意 ``RATE_LIMITED`` **不在**这里：限流说明我们已经在给对方压力了，
#: 立刻重试只会让情况更糟 —— 应该等冷却时间过去（hint 里会写清楚等多久）。
RETRYABLE: frozenset[ErrorCode] = frozenset({ErrorCode.UPSTREAM_TIMEOUT, ErrorCode.UPSTREAM_ERROR})

#: 网关层错误（限流/熔断/权限）意味着"这次调用根本没发出去"。
#: 审计日志里要区分它们和下游错误，否则熔断统计会把两件事混在一起。
GATEWAY_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.TOOL_NOT_FOUND,
        ErrorCode.PERMISSION_DENIED,
        ErrorCode.RATE_LIMITED,
        ErrorCode.CIRCUIT_OPEN,
        ErrorCode.INVALID_ARGUMENTS,
    }
)


@dataclass(frozen=True)
class ToolError:
    """结构化错误。"""

    code: ErrorCode
    message: str
    hint: str = ""
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # 允许调用方忘记写 retryable —— 按 code 自动推导，避免两处配置不一致
        if not self.retryable and self.code in RETRYABLE:
            object.__setattr__(self, "retryable", True)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": str(self.code), "message": self.message}
        if self.hint:
            payload["hint"] = self.hint
        payload["retryable"] = self.retryable
        if self.details:
            payload["details"] = self.details
        return payload

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


@dataclass(frozen=True)
class ToolResult(Generic[T]):
    """工具调用结果。**成功和失败用同一个类型**，调用方不用写 try/except。

    这样做的好处是：审计、重试、错误归因都能用统一的方式处理，
    不会出现"成功走一条路径、失败走另一条"导致漏记审计的情况。
    """

    ok: bool
    tool: str
    data: T | None = None
    error: ToolError | None = None
    duration_ms: int = 0
    attempts: int = 1
    trace_id: str = ""
    cached: bool = False

    # ---- 构造 ----
    @classmethod
    def success(
        cls,
        tool: str,
        data: T,
        *,
        duration_ms: int = 0,
        attempts: int = 1,
        trace_id: str = "",
        cached: bool = False,
    ) -> ToolResult[T]:
        return cls(
            ok=True,
            tool=tool,
            data=data,
            duration_ms=duration_ms,
            attempts=attempts,
            trace_id=trace_id,
            cached=cached,
        )

    @classmethod
    def failure(
        cls,
        tool: str,
        error: ToolError,
        *,
        duration_ms: int = 0,
        attempts: int = 1,
        trace_id: str = "",
    ) -> ToolResult[T]:
        return cls(
            ok=False,
            tool=tool,
            error=error,
            duration_ms=duration_ms,
            attempts=attempts,
            trace_id=trace_id,
        )

    # ---- 序列化 ----
    def to_dict(self) -> dict[str, Any]:
        """转成可以直接喂给 LLM 的 JSON。

        失败时**一定包含 hint**，让模型有机会自我纠正。
        """
        payload: dict[str, Any] = {"ok": self.ok, "tool": self.tool}
        if self.ok:
            payload["data"] = self.data
        elif self.error is not None:
            payload["error"] = self.error.to_dict()
        if self.cached:
            payload["cached"] = True
        return payload

    def unwrap(self) -> T:
        """成功取数据，失败抛异常。给"确定不会失败"的场景用。"""
        if not self.ok or self.error is not None:
            raise ToolCallFailed(self.error or ToolError(ErrorCode.INTERNAL_ERROR, "未知错误"))
        return self.data  # type: ignore[return-value]


class ToolCallFailed(Exception):
    """``ToolResult.unwrap()`` 在失败分支抛的异常。

    网关本身**不抛异常**（靠 ToolResult 表达失败），
    这个异常只出现在调用方主动要求"失败即中断"的地方。
    """

    def __init__(self, error: ToolError) -> None:
        super().__init__(str(error))
        self.error = error
