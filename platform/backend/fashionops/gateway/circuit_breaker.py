"""熔断器：closed / open / half_open 三态。

## 为什么需要它

限流是防「我们打爆下游」，熔断是防「下游挂了拖死我们」。

下游连续失败时，继续调用有三个代价：
1. 每个请求都要等超时（10 秒 × 并发数，线程池很快被占满）
2. 失败请求会重试，给已经难受的下游再加压力
3. Agent 拿到一堆超时错误，token 全浪费在无意义的循环上

熔断就是：连续挂 N 次之后，直接不发了，快速失败。

## 三态怎么走

```
                连续失败 >= threshold
    ┌────────┐ ────────────────────────► ┌────────┐
    │ closed │                           │  open  │
    └────────┘ ◄──────────────────────── └────────┘
        ▲              探测成功              │ 冷却期结束
        │                                    ▼
        │                            ┌───────────┐
        └────────────────────────────│ half_open │
                    探测成功          └───────────┘
                                          │ 探测失败
                                          ▼
                                      （回到 open）
```

## 半开态的关键：**只放一个请求过去**

如果冷却期一结束就把闸门全开，那正好是下游刚恢复（或者更糟，还没恢复）的时刻，
一波流量打过去会立刻把它再打死，然后无限循环。

所以半开态用 ``SET NX`` 抢一个"探测名额"：抢到的那个请求去试探，
其他的继续快速失败。探测成功 → 回到 closed；探测失败 → 立刻重新熔断。

这也是为什么 ``record_failure`` 要先检查有没有探测在飞 ——
探测失败意味着下游还是不行，必须**立即**重新打开，
而不是傻等失败的计数器慢慢涨到阈值。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from fashionops.cache import DB_GATEWAY, get_redis


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class BreakerDecision:
    allowed: bool
    state: BreakerState
    failures: int = 0
    threshold: int = 0
    #: 被拒时距离可以再试还有多少秒
    retry_after_s: float = 0.0
    backend: str = ""

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "state": str(self.state),
            "failures": self.failures,
            "threshold": self.threshold,
            "retry_after_s": round(self.retry_after_s, 2),
            "backend": self.backend,
        }


class CircuitBreaker(Protocol):
    def before_call(self, key: str) -> BreakerDecision: ...
    def record_success(self, key: str) -> None: ...
    def record_failure(self, key: str) -> BreakerDecision: ...
    def snapshot(self, key: str) -> dict: ...
    def reset(self, key: str) -> None: ...


# ---------------------------------------------------------------------------
# Redis 实现
# ---------------------------------------------------------------------------
class RedisCircuitBreaker:
    """熔断状态存 Redis，好处是**跨进程共享**。

    如果用进程内变量，4 个 uvicorn worker 就是 4 个独立的熔断器，
    每个都要各自踩一遍坑才会打开 —— 熔断的意义就打了折扣。
    而且进程重启后冷却期归零，下游明明还在挂，我们又开始猛打。
    """

    backend = "redis"

    def __init__(
        self,
        client=None,  # noqa: ANN001
        *,
        threshold: int = 5,
        cooldown_s: int = 60,
        failure_window_s: int = 120,
        probe_timeout_s: int = 30,
    ) -> None:
        self._client = client
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self.failure_window_s = failure_window_s
        self.probe_timeout_s = probe_timeout_s

    def _get_client(self):  # noqa: ANN202
        if self._client is None:
            self._client = get_redis(DB_GATEWAY)
        return self._client

    # ---- 键名 ----
    @staticmethod
    def _k_open_until(key: str) -> str:
        return f"cb:{key}:open_until"

    @staticmethod
    def _k_fails(key: str) -> str:
        return f"cb:{key}:fails"

    @staticmethod
    def _k_probe(key: str) -> str:
        return f"cb:{key}:probe"

    # ---- 主流程 ----
    def before_call(self, key: str) -> BreakerDecision:
        client = self._get_client()
        now_ms = int(time.time() * 1000)

        try:
            # open_until 存活时间要明显长于 cooldown：
            # 我们靠**时间戳比较**判断冷却是否结束，而不是靠键是否存在。
            # 如果键在冷却结束的瞬间就过期了，就分不清"从没熔断过"和"刚冷却完"。
            raw = client.get(self._k_open_until(key))
            open_until = int(raw) if raw else 0
            fails = int(client.get(self._k_fails(key)) or 0)
        except Exception as exc:  # noqa: BLE001
            from fashionops.gateway.rate_limit import LimiterUnavailable

            raise LimiterUnavailable(f"Redis 熔断器不可用：{type(exc).__name__}: {exc}") from exc

        if open_until == 0:
            return BreakerDecision(True, BreakerState.CLOSED, fails, self.threshold, backend=self.backend)

        if now_ms < open_until:
            return BreakerDecision(
                False,
                BreakerState.OPEN,
                fails,
                self.threshold,
                retry_after_s=(open_until - now_ms) / 1000,
                backend=self.backend,
            )

        # 冷却期已过 —— 抢探测名额。抢不到说明已经有一个探测请求在飞了，
        # 继续快速失败，不要一窝蜂涌上去。
        got_probe = client.set(self._k_probe(key), str(now_ms), nx=True, px=self.probe_timeout_s * 1000)
        if got_probe:
            return BreakerDecision(True, BreakerState.HALF_OPEN, fails, self.threshold, backend=self.backend)

        probe_ttl_ms = int(client.pttl(self._k_probe(key)) or 0)
        return BreakerDecision(
            False,
            BreakerState.OPEN,
            fails,
            self.threshold,
            retry_after_s=max(0.0, probe_ttl_ms / 1000),
            backend=self.backend,
        )

    def record_success(self, key: str) -> None:
        """一次成功就彻底复位。

        为什么不"成功也要攒够 N 次才恢复"：熔断的目的是尽快停止无效调用，
        已经确认下游恢复就该立刻放量。半开态的探测命中即为证据。
        """
        client = self._get_client()
        client.delete(self._k_open_until(key), self._k_fails(key), self._k_probe(key))

    def record_failure(self, key: str) -> BreakerDecision:
        client = self._get_client()
        now_ms = int(time.time() * 1000)

        # 探测请求失败 → 下游还是不行，立即重新熔断，不用等计数器涨上去
        if client.exists(self._k_probe(key)):
            client.delete(self._k_probe(key))
            client.set(
                self._k_open_until(key),
                str(now_ms + self.cooldown_s * 1000),
                px=(self.cooldown_s * 2 + 60) * 1000,
            )
            return BreakerDecision(
                False,
                BreakerState.OPEN,
                self.threshold,
                self.threshold,
                retry_after_s=float(self.cooldown_s),
                backend=self.backend,
            )

        fails = client.incr(self._k_fails(key))
        if fails == 1:
            client.expire(self._k_fails(key), self.failure_window_s)

        if fails >= self.threshold:
            client.set(
                self._k_open_until(key),
                str(now_ms + self.cooldown_s * 1000),
                px=(self.cooldown_s * 2 + 60) * 1000,
            )
            return BreakerDecision(
                False,
                BreakerState.OPEN,
                fails,
                self.threshold,
                retry_after_s=float(self.cooldown_s),
                backend=self.backend,
            )

        return BreakerDecision(True, BreakerState.CLOSED, fails, self.threshold, backend=self.backend)

    def snapshot(self, key: str) -> dict:
        """给 ``/api/v1/gateway/stats`` 和前端看的当前状态。"""
        client = self._get_client()
        now_ms = int(time.time() * 1000)
        raw = client.get(self._k_open_until(key))
        open_until = int(raw) if raw else 0
        state = BreakerState.CLOSED
        if open_until and now_ms < open_until:
            state = BreakerState.OPEN
        elif client.exists(self._k_probe(key)):
            state = BreakerState.HALF_OPEN
        return {
            "key": key,
            "state": str(state),
            "failures": int(client.get(self._k_fails(key)) or 0),
            "threshold": self.threshold,
            "retry_after_s": max(0.0, (open_until - now_ms) / 1000) if open_until else 0.0,
        }

    def reset(self, key: str) -> None:
        self._get_client().delete(self._k_open_until(key), self._k_fails(key), self._k_probe(key))


# ---------------------------------------------------------------------------
# 进程内实现
# ---------------------------------------------------------------------------
@dataclass
class _MemoryState:
    failures: int = 0
    fails_expire_at: float = 0.0
    open_until: float = 0.0
    probe_until: float = 0.0


class MemoryCircuitBreaker:
    """进程内熔断器。单测用；生产多 worker 下不要用（每个 worker 各熔各的）。"""

    backend = "memory"

    def __init__(
        self,
        *,
        threshold: int = 5,
        cooldown_s: int = 60,
        failure_window_s: int = 120,
        probe_timeout_s: int = 30,
    ) -> None:
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self.failure_window_s = failure_window_s
        self.probe_timeout_s = probe_timeout_s
        self._states: dict[str, _MemoryState] = {}
        self._lock = threading.Lock()

    def before_call(self, key: str) -> BreakerDecision:
        now = time.monotonic()
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return BreakerDecision(True, BreakerState.CLOSED, 0, self.threshold, backend=self.backend)

            if state.fails_expire_at and now >= state.fails_expire_at:
                state.failures = 0
                state.fails_expire_at = 0.0

            if state.open_until == 0.0:
                return BreakerDecision(True, BreakerState.CLOSED, state.failures, self.threshold, backend=self.backend)

            if now < state.open_until:
                return BreakerDecision(
                    False,
                    BreakerState.OPEN,
                    state.failures,
                    self.threshold,
                    retry_after_s=state.open_until - now,
                    backend=self.backend,
                )

            if state.probe_until <= now:
                state.probe_until = now + self.probe_timeout_s
                return BreakerDecision(
                    True, BreakerState.HALF_OPEN, state.failures, self.threshold, backend=self.backend
                )

            return BreakerDecision(
                False,
                BreakerState.OPEN,
                state.failures,
                self.threshold,
                retry_after_s=state.probe_until - now,
                backend=self.backend,
            )

    def record_success(self, key: str) -> None:
        with self._lock:
            self._states.pop(key, None)

    def record_failure(self, key: str) -> BreakerDecision:
        now = time.monotonic()
        with self._lock:
            state = self._states.setdefault(key, _MemoryState())

            if state.probe_until > now:
                state.probe_until = 0.0
                state.open_until = now + self.cooldown_s
                return BreakerDecision(
                    False,
                    BreakerState.OPEN,
                    state.failures,
                    self.threshold,
                    retry_after_s=float(self.cooldown_s),
                    backend=self.backend,
                )

            if state.fails_expire_at and now >= state.fails_expire_at:
                state.failures = 0
            state.failures += 1
            state.fails_expire_at = now + self.failure_window_s

            if state.failures >= self.threshold:
                state.open_until = now + self.cooldown_s
                return BreakerDecision(
                    False,
                    BreakerState.OPEN,
                    state.failures,
                    self.threshold,
                    retry_after_s=float(self.cooldown_s),
                    backend=self.backend,
                )

            return BreakerDecision(True, BreakerState.CLOSED, state.failures, self.threshold, backend=self.backend)

    def snapshot(self, key: str) -> dict:
        now = time.monotonic()
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return {"key": key, "state": "closed", "failures": 0, "threshold": self.threshold, "retry_after_s": 0.0}
            if state.open_until > now:
                st = BreakerState.OPEN
            elif state.probe_until > now:
                st = BreakerState.HALF_OPEN
            else:
                st = BreakerState.CLOSED
            return {
                "key": key,
                "state": str(st),
                "failures": state.failures,
                "threshold": self.threshold,
                "retry_after_s": max(0.0, state.open_until - now),
            }

    def reset(self, key: str) -> None:
        with self._lock:
            self._states.pop(key, None)


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
def get_circuit_breaker(
    backend: str = "redis",
    *,
    threshold: int = 5,
    cooldown_s: int = 60,
    failure_window_s: int = 120,
    probe_timeout_s: int = 30,
) -> CircuitBreaker:
    if backend == "redis":
        return RedisCircuitBreaker(
            threshold=threshold,
            cooldown_s=cooldown_s,
            failure_window_s=failure_window_s,
            probe_timeout_s=probe_timeout_s,
        )
    return MemoryCircuitBreaker(
        threshold=threshold,
        cooldown_s=cooldown_s,
        failure_window_s=failure_window_s,
        probe_timeout_s=probe_timeout_s,
    )
