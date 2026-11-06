"""工具网关：所有工具调用的唯一入口。

治理能力是横切的 —— Agent 只负责决策，限流/熔断/重试/幂等/审计/权限
统一在这里收口。

基本用法::

    from fashionops.gateway import CallContext, ToolGateway, registry

    gw = ToolGateway(registry)          # 限流 / 熔断 / 审计落库都是默认接好的
    ctx = CallContext(actor="selection_agent", scopes=frozenset({Scope.ERP_READ}))

    result = gw.call("erp.query_inventory", {"sku_codes": ["SKU00001"]}, ctx=ctx)
    if result.ok:
        print(result.data)
    else:
        print(result.error.code, result.error.hint)   # hint 是给模型的下一步建议

**审计默认落库**（`SqlAuditSink`）。要内存 sink（单测、纯计算脚本）就显式传
``audit_sink=MemoryAuditSink()`` —— 理由见 `core.ToolGateway.__init__` 里的说明。
"""

from __future__ import annotations

from fashionops.gateway.audit import (
    AuditEntry,
    AuditSink,
    FanoutAuditSink,
    MemoryAuditSink,
    SqlAuditSink,
    sanitize,
    summarize_result,
)
from fashionops.gateway.circuit_breaker import (
    BreakerDecision,
    BreakerState,
    CircuitBreaker,
    MemoryCircuitBreaker,
    RedisCircuitBreaker,
    get_circuit_breaker,
)
from fashionops.gateway.core import CallContext, IdempotencyStore, ToolGateway, ToolStats
from fashionops.gateway.errors import (
    GATEWAY_CODES,
    RETRYABLE,
    ErrorCode,
    ToolCallFailed,
    ToolError,
    ToolResult,
)
from fashionops.gateway.rate_limit import (
    LimiterUnavailable,
    MemoryRateLimiter,
    RateLimitDecision,
    RateLimiter,
    RedisRateLimiter,
    get_rate_limiter,
)
from fashionops.gateway.registry import (
    Scope,
    ToolNotFound,
    ToolRegistry,
    ToolSpec,
    from_llm_name,
    registry,
    to_llm_name,
)

__all__ = [
    "GATEWAY_CODES",
    "RETRYABLE",
    "AuditEntry",
    "AuditSink",
    "BreakerDecision",
    "BreakerState",
    "CallContext",
    "CircuitBreaker",
    "ErrorCode",
    "FanoutAuditSink",
    "IdempotencyStore",
    "LimiterUnavailable",
    "MemoryAuditSink",
    "MemoryCircuitBreaker",
    "MemoryRateLimiter",
    "RateLimitDecision",
    "RateLimiter",
    "RedisCircuitBreaker",
    "RedisRateLimiter",
    "Scope",
    "SqlAuditSink",
    "ToolCallFailed",
    "ToolError",
    "ToolGateway",
    "ToolNotFound",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "ToolStats",
    "from_llm_name",
    "get_circuit_breaker",
    "get_rate_limiter",
    "registry",
    "sanitize",
    "summarize_result",
    "to_llm_name",
]
