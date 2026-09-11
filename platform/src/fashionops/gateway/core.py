"""工具网关 —— 所有工具调用的唯一入口。

## 为什么要有这一层

如果 Agent 直接调 `mock_erp.query_inventory()`，下面这些事就没人管：

| 问题 | 不管会怎样 |
| --- | --- |
| 限流 | Agent 循环调用能把下游打挂 |
| 熔断 | 下游已经挂了，还是每个请求都傻等 10 秒超时 |
| 重试 | 偶发网络抖动直接变成任务失败 |
| 幂等 | 重试一次上架，店铺里多出一个重复商品 |
| 审计 | 出了问题查不出「谁在什么时候调了什么、带了什么参数」 |
| 权限 | 通知类 Agent 也能调「改广告预算」 |
| 超时 | 一个卡住的工具把 worker 线程占死 |

把这些收口到一层，**Agent 只负责决策**，治理是横切的。这也是"工具网关"
在 §3 技术点矩阵里排在 P0 的原因。

## 调用链

```
call(tool, args, ctx)
  ├─ 1. 查注册表          → TOOL_NOT_FOUND
  ├─ 2. 权限检查          → PERMISSION_DENIED
  ├─ 3. 参数校验(Pydantic)→ INVALID_ARGUMENTS
  ├─ 4. 幂等查缓存        → 命中直接返回（cached=True）
  ├─ 5. 限流              → RATE_LIMITED
  ├─ 6. 熔断              → CIRCUIT_OPEN
  ├─ 7. 执行（重试+超时） → UPSTREAM_TIMEOUT / UPSTREAM_ERROR
  ├─ 8. 熔断记账（成功/失败）
  ├─ 9. 幂等写缓存
  └─ 10. 审计（无论成败，一定写）
```

## 降级策略：fail-open 还是 fail-closed

限流器和熔断器的状态存在 Redis 里。**Redis 挂了怎么办？**

* **只读工具 → fail-open（放行）**
  查库存失败比查不到库存更糟。而且只读操作不会产生副作用，
  放开最多是下游压力大一点。
* **写工具 → fail-closed（拒绝）**
  没有限流保护就发上架请求、改预算请求，可能被下游当成攻击，
  也可能因为重试刷出重复数据。宁可失败让 Agent 报错，也不能失控。

这个区分是刻意做的：一刀切地 fail-open 会失去保护，
一刀切地 fail-closed 会让"缓存挂了"升级成"整个系统不可用"。
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from fashionops.cache import DB_CACHE, get_redis
from fashionops.config import get_settings
from fashionops.gateway.audit import (
    AuditEntry,
    AuditSink,
    MemoryAuditSink,
    sanitize,
    summarize_result,
)
from fashionops.gateway.circuit_breaker import (
    BreakerDecision,
    BreakerState,
    CircuitBreaker,
    get_circuit_breaker,
)
from fashionops.gateway.errors import ErrorCode, ToolError, ToolResult
from fashionops.gateway.rate_limit import (
    LimiterUnavailable,
    RateLimiter,
    get_rate_limiter,
)
from fashionops.gateway.registry import ToolNotFound, ToolRegistry, ToolSpec

logger = logging.getLogger("fashionops.gateway")

#: 重试的基础退避秒数。真实延迟 = min(base * 2^n, cap) * jitter
_BACKOFF_BASE_S = 0.25
_BACKOFF_CAP_S = 2.0

#: 幂等结果缓存时长
_IDEMPOTENCY_TTL_S = 3600


# ---------------------------------------------------------------------------
# 调用上下文
# ---------------------------------------------------------------------------
@dataclass
class CallContext:
    """一次工具调用的上下文。

    ``scopes`` 是调用方（某个 Agent）被授予的权限集合。
    **由调用方传入而不是网关自己去查** —— 网关不关心权限从哪来
    （可能是配置文件、数据库、或者 OAuth 的 token claim），
    只负责拿它做判断。
    """

    actor: str
    scopes: frozenset[str] = frozenset()
    task_id: int | None = None
    trace_id: str = ""
    idempotency_key: str | None = None
    ip: str | None = None

    def resolved_trace_id(self) -> str:
        # 没有外部 trace 时自己生成一个，保证审计记录一定能关联上
        return self.trace_id or uuid.uuid4().hex[:16]


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
@dataclass
class ToolStats:
    calls: int = 0
    success: int = 0
    failed: int = 0
    rate_limited: int = 0
    circuit_open: int = 0
    denied: int = 0
    invalid_args: int = 0
    total_duration_ms: int = 0
    max_duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        avg = self.total_duration_ms / self.calls if self.calls else 0.0
        return {
            "calls": self.calls,
            "success": self.success,
            "failed": self.failed,
            "rate_limited": self.rate_limited,
            "circuit_open": self.circuit_open,
            "denied": self.denied,
            "invalid_args": self.invalid_args,
            "avg_duration_ms": round(avg, 2),
            "max_duration_ms": self.max_duration_ms,
            "success_rate": round(self.success / self.calls, 4) if self.calls else None,
        }


# ---------------------------------------------------------------------------
# 幂等存储
# ---------------------------------------------------------------------------
class IdempotencyStore:
    """幂等结果缓存。

    用途：**写操作重试不能变成两次写**。

    典型场景：上架请求发出去了、下游也处理了，但响应在网络里丢了。
    网关判定超时 → 重试 → 店铺里出现两个一样的商品。

    带上 ``idempotency_key`` 后，重试会命中缓存直接返回第一次的结果。
    key 由调用方给（通常是 task_no + 节点名），**不能由网关自己生成** ——
    网关每次调用生成一个新 key，那每次都是"第一次"，等于没做。

    Redis 不可用时降级为不缓存：**宁可多写一次，也不要因为缓存挂了
    导致所有写操作被拒**。幂等失效的风险由调用方的业务逻辑兜底
    （比如上架前先按 spu_code 查一次）。

    ``use_redis=False`` 时只用进程内字典。**写测试时必须开这个开关** ——
    否则测试之间会通过 Redis 悄悄串数据：上一个用例写的缓存键
    会被下一个用例命中，报出看起来毫无道理的失败。
    这类"跨用例污染"很难排查，因为它依赖执行顺序。
    """

    def __init__(
        self,
        *,
        use_redis: bool = True,
        client=None,  # noqa: ANN001
        namespace: str = "fashionops",
    ) -> None:
        self._use_redis = use_redis
        self._client = client
        self._namespace = namespace
        self._memory: dict[str, tuple[float, str]] = {}

    def _get_redis(self):  # noqa: ANN202
        if not self._use_redis:
            return None
        if self._client is None:
            self._client = get_redis(DB_CACHE)
        return self._client

    def _key(self, tool: str, idem_key: str) -> str:
        # 带命名空间：同一台 Redis 上可能同时跑着开发环境和别人的项目
        return f"{self._namespace}:idem:{tool}:{idem_key}"

    def get(self, tool: str, idem_key: str) -> Any | None:
        full = self._key(tool, idem_key)
        client = self._get_redis()
        if client is not None:
            try:
                raw = client.get(full)
                if raw is not None:
                    return json.loads(raw)
            except Exception as exc:  # noqa: BLE001
                logger.debug("幂等读取失败，回退内存：%s", exc)

        hit = self._memory.get(full)
        if hit and hit[0] > time.monotonic():
            return json.loads(hit[1])
        return None

    def put(self, tool: str, idem_key: str, data: Any) -> None:
        full = self._key(tool, idem_key)
        try:
            payload = json.dumps(data, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            # 结果不可序列化就不缓存 —— 不能因为缓存失败把正常调用搞挂
            return

        client = self._get_redis()
        if client is not None:
            try:
                client.set(full, payload, ex=_IDEMPOTENCY_TTL_S)
                return
            except Exception as exc:  # noqa: BLE001
                logger.debug("幂等写入失败，回退内存：%s", exc)

        self._memory[full] = (time.monotonic() + _IDEMPOTENCY_TTL_S, payload)
        # 顺手清掉过期的内存条目，防止长跑进程内存泄漏
        now = time.monotonic()
        for key in [k for k, (exp, _) in self._memory.items() if exp <= now]:
            self._memory.pop(key, None)

    def clear(self) -> None:
        """清空内存部分。测试的 teardown 用。"""
        self._memory.clear()


# ---------------------------------------------------------------------------
# 网关
# ---------------------------------------------------------------------------
class ToolGateway:
    """工具调用网关。"""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        rate_limiter: RateLimiter | None = None,
        breaker: CircuitBreaker | None = None,
        audit_sink: AuditSink | None = None,
        idempotency: IdempotencyStore | None = None,
        rate_limit_per_minute: int | None = None,
        max_retries: int | None = None,
    ) -> None:
        settings = get_settings()

        self.registry = registry
        self.limiter = rate_limiter or get_rate_limiter(settings.cache_backend)
        self.breaker = breaker or get_circuit_breaker(
            settings.cache_backend,
            threshold=settings.gateway_circuit_breaker_threshold,
            cooldown_s=settings.gateway_circuit_breaker_cooldown,
        )
        self.audit = audit_sink or MemoryAuditSink()
        self.idempotency = idempotency or IdempotencyStore(namespace=settings.redis_key_prefix)

        self.rate_limit_per_minute = rate_limit_per_minute or settings.gateway_rate_limit_per_minute
        self.max_retries = max_retries if max_retries is not None else settings.gateway_max_retries

        self._stats: dict[str, ToolStats] = {}

    def close(self) -> None:
        """预留的清理钩子（目前没有需要释放的共享资源，每次调用自带线程）。"""

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------
    def _stats_for(self, tool: str) -> ToolStats:
        return self._stats.setdefault(tool, ToolStats())

    def stats_snapshot(self) -> dict[str, Any]:
        """给 ``/api/v1/gateway/stats`` 用。

        注意这是**单进程内**的统计。多 worker 部署时每个 worker 各算各的，
        要看全局得接 Prometheus 或者把计数推 Redis。
        """
        return {
            "tools": {name: st.to_dict() for name, st in sorted(self._stats.items())},
            "registered": len(self.registry),
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "max_retries": self.max_retries,
        }

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def call(
        self,
        tool_name: str,
        args: dict[str, Any] | BaseModel | None = None,
        *,
        ctx: CallContext,
    ) -> ToolResult[Any]:
        """同步调用。读多写少的工具、脚本、单测用这个。"""
        trace_id = ctx.resolved_trace_id()
        started = time.perf_counter()
        stats = self._stats_for(tool_name)
        stats.calls += 1

        try:
            spec = self.registry.get(tool_name)
        except ToolNotFound:
            return self._reject(
                tool_name, trace_id, started, stats,
                ToolError(
                    ErrorCode.TOOL_NOT_FOUND,
                    f"工具 {tool_name} 未注册",
                    hint=(
                        f"可用工具：{', '.join(self.registry.names()[:12])}"
                        + (" …" if len(self.registry) > 12 else "")
                    ),
                ),
                ctx, action=None,
            )

        # ---- 2. 权限 ----
        if spec.scopes and not (spec.scopes & ctx.scopes):
            return self._reject(
                tool_name, trace_id, started, stats,
                ToolError(
                    ErrorCode.PERMISSION_DENIED,
                    f"角色 {ctx.actor} 缺少调用 {tool_name} 所需权限",
                    hint=(
                        f"该工具需要以下任一权限：{' / '.join(sorted(spec.scopes))}；"
                        f"当前拥有：{' / '.join(sorted(ctx.scopes)) or '（无）'}。"
                        "请转交给具备该权限的 Agent，或改为只读方案。"
                    ),
                    details={"required": sorted(spec.scopes), "granted": sorted(ctx.scopes)},
                ),
                ctx, action="denied",
            )

        # ---- 3. 参数校验 ----
        try:
            payload = self._validate_args(spec, args)
        except ValidationError as exc:
            return self._reject(
                tool_name, trace_id, started, stats,
                ToolError(
                    ErrorCode.INVALID_ARGUMENTS,
                    f"参数校验失败：{exc.error_count()} 处不合法",
                    hint=(
                        "请按 schema 修正参数后重试："
                        + "; ".join(
                            f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}"
                            for e in exc.errors()[:4]
                        )
                    ),
                    details={"schema": spec.args_model.model_json_schema()},
                ),
                ctx, action="invalid",
            )

        # ---- 4. 幂等 ----
        idem_key = self._idempotency_key(spec, payload, ctx)
        if idem_key is not None:
            cached = self.idempotency.get(spec.name, idem_key)
            if cached is not None:
                stats.success += 1
                return ToolResult.success(
                    tool_name, cached,
                    duration_ms=self._elapsed_ms(started),
                    trace_id=trace_id, cached=True,
                )

        # ---- 5. 限流 ----
        guard = self._check_rate_limit(spec, ctx, trace_id, started, stats)
        if guard is not None:
            return guard

        # ---- 6. 熔断 ----
        guard = self._check_circuit(spec, ctx, trace_id, started, stats)
        if guard is not None:
            return guard

        # ---- 7~8. 执行 + 记账 ----
        result, attempts = self._execute_with_retry(spec, payload, trace_id)

        if result.ok:
            self._on_upstream_success(spec)
            stats.success += 1
            if idem_key is not None:
                self.idempotency.put(spec.name, idem_key, result.data)
        else:
            self._on_upstream_failure(spec, result)
            stats.failed += 1

        result = ToolResult(
            ok=result.ok,
            tool=result.tool,
            data=result.data,
            error=result.error,
            duration_ms=self._elapsed_ms(started),
            attempts=attempts,
            trace_id=trace_id,
        )
        stats.total_duration_ms += result.duration_ms
        stats.max_duration_ms = max(stats.max_duration_ms, result.duration_ms)

        self._write_audit(spec, ctx, trace_id, result, attempts, payload)
        return result

    # ------------------------------------------------------------------
    # 内部步骤
    # ------------------------------------------------------------------
    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)

    @staticmethod
    def _idempotency_key(
        spec: ToolSpec, payload: dict[str, Any], ctx: CallContext
    ) -> str | None:
        """算出本次调用的幂等键；返回 ``None`` 表示不做幂等。

        **关键点：键里必须带上入参摘要。**

        调用方给的 ``idempotency_key`` 通常是 ``task_no:节点名`` 这种粒度。
        如果直接拿它做缓存键，同一个节点内用**不同参数**调同一个工具时，
        第二次会命中第一次的缓存、直接返回上一次的结果 ——
        这比"根本没做幂等"危险得多，因为它是静默的错误。

        所以最终键 = 业务键 + 入参 sha256 前 16 位。
        重试时入参完全一致 → 命中缓存；换了参数 → 重新执行。
        """
        if not spec.idempotent or not ctx.idempotency_key:
            return None
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
        return f"{ctx.idempotency_key}:{digest}"

    @staticmethod
    def _validate_args(spec: ToolSpec, args: dict[str, Any] | BaseModel | None) -> dict[str, Any]:
        """校验并规范化入参，返回 ``**kwargs`` 形式。

        注意 ``model_dump()`` 而不是 ``dict()``：``dict()`` 只转换顶层字段，
        嵌套的 Pydantic 模型和 ``Decimal`` 会被原样带出去，
        下游拿到非原生类型很容易出错。
        """
        raw = args.model_dump() if isinstance(args, BaseModel) else (args or {})
        return spec.args_model(**raw).model_dump()

    def _reject(
        self,
        tool_name: str,
        trace_id: str,
        started: float,
        stats: ToolStats,
        error: ToolError,
        ctx: CallContext,
        *,
        action: str | None,
    ) -> ToolResult[Any]:
        """网关层拒绝：统一记账 + 写审计，保证"被拒"也留痕。"""
        if error.code is ErrorCode.RATE_LIMITED:
            stats.rate_limited += 1
        elif error.code is ErrorCode.CIRCUIT_OPEN:
            stats.circuit_open += 1
        elif error.code is ErrorCode.PERMISSION_DENIED:
            stats.denied += 1
        elif error.code is ErrorCode.INVALID_ARGUMENTS:
            stats.invalid_args += 1

        duration_ms = self._elapsed_ms(started)
        stats.total_duration_ms += duration_ms
        stats.max_duration_ms = max(stats.max_duration_ms, duration_ms)

        result: ToolResult[Any] = ToolResult.failure(
            tool_name, error, duration_ms=duration_ms, attempts=1, trace_id=trace_id
        )
        self.audit.record(
            AuditEntry(
                actor=ctx.actor,
                tool_name=tool_name,
                status="blocked",
                trace_id=trace_id,
                task_id=ctx.task_id,
                action=action,
                error_code=str(error.code),
                duration_ms=duration_ms,
                ip=ctx.ip,
            )
        )
        return result

    def _check_rate_limit(
        self,
        spec: ToolSpec,
        ctx: CallContext,
        trace_id: str,
        started: float,
        stats: ToolStats,
    ) -> ToolResult[Any] | None:
        """返回 ``None`` 表示放行。"""
        key = f"rl:{ctx.actor}:{spec.name}"
        # 单工具可以覆盖全局限额：报表导出这种重接口要求严格，
        # 而查库存这种轻接口可以放宽。
        limit = spec.rate_limit_per_minute or self.rate_limit_per_minute
        try:
            decision = self.limiter.hit(key, limit, 60)
        except LimiterUnavailable as exc:
            if spec.read_only:
                # 只读 fail-open：查不到库存比查不了库存更糟
                logger.warning("限流组件不可用，只读工具 %s 放行：%s", spec.name, exc)
                return None
            # 写操作 fail-closed：没有限流保护就不发写请求
            return self._reject(
                spec.name, trace_id, started, stats,
                ToolError(
                    ErrorCode.RATE_LIMITED,
                    "限流组件不可用，写操作已按 fail-closed 策略拒绝",
                    hint=(
                        "请检查 Redis 是否在运行（deploy/scripts/services.ps1 status）。"
                        "在限流恢复前不要重试写操作。"
                    ),
                    details={"reason": "limiter_unavailable", "error": str(exc)},
                ),
                ctx, action="rate_limit_unavailable",
            )

        if decision.allowed:
            return None

        return self._reject(
            spec.name, trace_id, started, stats,
            ToolError(
                ErrorCode.RATE_LIMITED,
                f"工具 {spec.name} 触发限流：{decision.window_s} 秒内已调用 {decision.limit} 次",
                hint=(
                    f"请在 {decision.retry_after_s:.1f} 秒后重试，"
                    "或先做其他步骤（比如先去查别的数据），不要原地重试。"
                ),
                details=decision.to_dict(),
            ),
            ctx, action="rate_limited",
        )

    def _check_circuit(
        self,
        spec: ToolSpec,
        ctx: CallContext,
        trace_id: str,
        started: float,
        stats: ToolStats,
    ) -> ToolResult[Any] | None:
        key = f"tool:{spec.name}"
        try:
            decision: BreakerDecision = self.breaker.before_call(key)
        except LimiterUnavailable as exc:
            if spec.read_only:
                logger.warning("熔断组件不可用，只读工具 %s 放行：%s", spec.name, exc)
                return None
            return self._reject(
                spec.name, trace_id, started, stats,
                ToolError(
                    ErrorCode.CIRCUIT_OPEN,
                    "熔断组件不可用，写操作已按 fail-closed 策略拒绝",
                    hint="请检查 Redis 是否在运行，在熔断状态可查之前不要重试写操作。",
                    details={"reason": "breaker_unavailable", "error": str(exc)},
                ),
                ctx, action="breaker_unavailable",
            )

        if decision.allowed:
            return None

        extra_hint = ""
        if decision.state is BreakerState.OPEN:
            extra_hint = (
                f"该工具连续失败已达 {decision.threshold} 次，"
                f"熔断中，约 {decision.retry_after_s:.0f} 秒后自动放行探测请求。"
            )

        return self._reject(
            spec.name, trace_id, started, stats,
            ToolError(
                ErrorCode.CIRCUIT_OPEN,
                f"工具 {spec.name} 处于熔断状态（{decision.state}）",
                hint=(
                    extra_hint
                    + "请不要重试同一工具，改用其他工具或走降级方案；"
                    "如果这是关键步骤，标记为「需要人工介入」。"
                ),
                details=decision.to_dict(),
            ),
            ctx, action="circuit_open",
        )

    def _on_upstream_success(self, spec: ToolSpec) -> None:
        # 只读工具调用很频繁，每次都删 Redis 键会增加无谓的往返；
        # 只在熔断可能被打开时才去复位。
        try:
            snapshot = self.breaker.snapshot(f"tool:{spec.name}")
            if snapshot["failures"] or snapshot["state"] != "closed":
                self.breaker.record_success(f"tool:{spec.name}")
        except Exception as exc:  # noqa: BLE001
            logger.debug("熔断成功记账失败（忽略）：%s", exc)

    def _on_upstream_failure(self, spec: ToolSpec, result: ToolResult[Any]) -> None:
        # 网关层错误（限流/权限/参数）在 _reject 里就返回了，走不到这里。
        # 到这里的都是真的打到下游了 —— 只有这种失败才该计入熔断。
        try:
            decision = self.breaker.record_failure(f"tool:{spec.name}")
            if not decision.allowed:
                logger.warning(
                    "工具 %s 已熔断（连续失败 %d 次，冷却 %ds）",
                    spec.name, decision.failures, int(decision.retry_after_s),
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("熔断失败记账失败（忽略)：%s", exc)

    def _execute_with_retry(
        self, spec: ToolSpec, payload: dict[str, Any], trace_id: str
    ) -> tuple[ToolResult[Any], int]:
        """执行 + 重试。返回值是 ``(结果, 实际尝试次数)``。"""
        attempts = 0
        last_error: ToolError | None = None

        while attempts <= self.max_retries:
            attempts += 1
            try:
                data = self._invoke(spec, payload)
                return ToolResult.success(spec.name, data, attempts=attempts, trace_id=trace_id), attempts
            except FutureTimeoutError:
                last_error = ToolError(
                    ErrorCode.UPSTREAM_TIMEOUT,
                    f"工具 {spec.name} 超过 {spec.timeout_s}s 未返回",
                    hint=(
                        "该工具响应过慢。可以：调小查询范围（比如缩短时间区间）、"
                        "或者改用聚合类工具拿汇总数据。"
                    ),
                    details={"timeout_s": spec.timeout_s},
                )
            except ValidationError as exc:
                # 下游返回的数据不符合 schema —— 重试没用，是数据契约的问题
                last_error = ToolError(
                    ErrorCode.SCHEMA_VIOLATION,
                    f"工具 {spec.name} 的返回值不符合 schema：{exc.error_count()} 处",
                    hint="这是数据契约问题，重试无效，请上报或走降级分支。",
                    details={"errors": exc.errors()[:4]},
                )
                break
            except Exception as exc:  # noqa: BLE001
                last_error = self._classify_exception(spec, exc)

            if last_error is not None and not last_error.retryable:
                break
            if attempts > self.max_retries:
                break

            # 指数退避 + 抖动。抖动很关键：多个 Agent 同时失败时，
            # 不加抖动它们会在同一时刻一起重试，形成新的尖峰。
            delay = min(_BACKOFF_BASE_S * (2 ** (attempts - 1)), _BACKOFF_CAP_S)
            delay *= 0.5 + random.random()
            logger.info("工具 %s 第 %d 次失败，%.2fs 后重试", spec.name, attempts, delay)
            time.sleep(delay)

        assert last_error is not None
        return (
            ToolResult.failure(spec.name, last_error, attempts=attempts, trace_id=trace_id),
            attempts,
        )

    @staticmethod
    def _invoke(spec: ToolSpec, payload: dict[str, Any]) -> Any:
        """真正执行 handler，并给同步 handler 套上超时。

        两个看似吹毛求疵、实际很关键的点：

        **1. 不用 ``with ThreadPoolExecutor(...)``**
        上下文管理器退出时调的是 ``shutdown(wait=True)`` —— 也就是
        "等线程跑完再返回"。这样一来超时根本不起作用，
        调用方还是在原地等到 handler 自己结束。必须显式 ``wait=False``。

        **2. 每次调用单独开线程，不用共享线程池**
        共享池里如果已经积压了几个卡住的调用，后续请求会先在队列里排队，
        而超时是从 ``submit`` 开始算的 —— 表现就是"没超时也报超时"，很难排查。
        单独开线程的代价是每次多约 50µs，换来行为可预测。

        最后要诚实说明局限：线程池只能让**调用方**停止等待，
        底层线程还在跑（Python 没有安全的线程中断机制）。
        所以对真实外部调用，handler 内部**必须自己设 httpx/socket 超时**，
        网关这层是兜底而不是唯一防线。
        """
        if spec.is_async:
            raise RuntimeError(
                f"工具 {spec.name} 是 async handler，请改用 gateway.acall()"
            )

        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tool-gw")
        try:
            return pool.submit(spec.handler, **payload).result(timeout=spec.timeout_s)
        finally:
            pool.shutdown(wait=False)

    @staticmethod
    def _classify_exception(spec: ToolSpec, exc: Exception) -> ToolError:
        name = type(exc).__name__
        message = str(exc) or name

        # 网络类异常统一算超时，让重试策略生效
        if "timeout" in name.lower() or "timeout" in message.lower():
            return ToolError(
                ErrorCode.UPSTREAM_TIMEOUT,
                f"{spec.name} 调用超时：{message}",
                hint="下游响应慢，重试可能有效；连续失败请改走降级方案。",
            )

        return ToolError(
            ErrorCode.UPSTREAM_ERROR,
            f"{spec.name} 调用失败：{name}: {message}",
            hint=(
                f"下游返回了错误。如果是数据问题请换参数，"
                f"如果是持续性故障会在连续 {get_settings().gateway_circuit_breaker_threshold} "
                "次失败后自动熔断。"
            ),
            details={"exception": name},
        )

    def _write_audit(
        self,
        spec: ToolSpec,
        ctx: CallContext,
        trace_id: str,
        result: ToolResult[Any],
        attempts: int,
        payload: dict[str, Any],
    ) -> None:
        # 审计是**旁路**，不是主路。
        #
        # 这里必须再兜一次异常，即使 SqlAuditSink 自己已经吞了 ——
        # 因为"审计失败不能影响业务"这条不变式应该由**网关**保证，
        # 而不是指望每个 sink 实现都自觉。sink 是可插拔的，
        # 第三方实现里一个没注意的 raise 就能把整个工具调用搞挂。
        try:
            self.audit.record(
                AuditEntry(
                    actor=ctx.actor,
                    tool_name=spec.name,
                    status="success" if result.ok else "error",
                    trace_id=trace_id,
                    task_id=ctx.task_id,
                    action="read" if spec.read_only else "write",
                    resource=spec.tags[0] if spec.tags else None,
                    # 入参走脱敏：工具参数里经常夹着 token / 密钥 / 手机号，
                    # 原样落库等于把敏感信息拷贝到一张保护更弱的表里
                    args_digest=sanitize(payload),
                    result_summary=(
                        f"attempts={attempts}; " + (summarize_result(result.data) or "ok")
                        if result.ok
                        else f"attempts={attempts}; {result.error}"
                    ),
                    error_code=str(result.error.code) if result.error else None,
                    duration_ms=result.duration_ms,
                    ip=ctx.ip,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "审计 sink 异常（已忽略，不影响业务）：tool=%s sink=%s %s: %s",
                spec.name,
                type(self.audit).__name__,
                type(exc).__name__,
                exc,
            )
