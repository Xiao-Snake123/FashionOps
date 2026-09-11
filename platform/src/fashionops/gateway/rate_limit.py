"""限流器：滑动窗口日志（sliding window log）。

## 为什么不用"固定窗口计数"

最简单的限流是固定窗口：``INCR rl:{key}:{当前分钟}`` + ``EXPIRE``。
一行搞定，但它有个绕不过去的缺陷 —— **窗口边界会放过两倍流量**：

    限流 100 次/分钟。
    第 59 秒打满 100 次，第 61 秒（新窗口）又能打满 100 次。
    也就是说 3 秒内实际放过了 200 次。

对"保护下游 Mock 电商 API"这种诉求来说，这个尖峰是致命的。

## 滑动窗口日志怎么解决

把**每次请求的时间戳**都记进一个有序集合，判断时先剔掉窗口外的，
再数剩下的：

    ZREMRANGEBYSCORE key 0 (now - window)   # 丢弃窗口外的
    ZCARD key                                # 数窗口内的
    如果 < limit → ZADD key now

窗口是"跟着现在这个时刻往前推"的，不存在边界，任何 60 秒区间内都不会超过 100 次。

**代价**：每个被限流的 key 要存 limit 个时间戳。我们单工具 120 次/分钟，
内存开销可以忽略。真要上百万 QPS 再考虑 Redis Cell 的 GCRA 或令牌桶。

## 为什么必须用 Lua

"先查数量、再决定写不写"是典型的 check-then-act。两个请求同时进来会
双双读到 99、双双写入 —— 限流直接失效。放进 Lua 脚本后 Redis 单线程执行，
天然原子。**用 `pipeline` 解决不了这个问题** —— 事务只保证命令连续执行，
不保证中间没有别的客户端插进来。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Protocol

from fashionops.cache import DB_GATEWAY, get_redis


class LimiterUnavailable(RuntimeError):
    """限流后端不可用（比如 Redis 挂了）。

    刻意**不在这里决定放行还是拒绝** —— 这是业务策略，应该由网关按
    "只读工具 fail-open / 写工具 fail-closed" 来决定。限流器只负责报告事实。
    """


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    window_s: int
    #: 被拒时，距离最早那次请求滑出窗口（也就是下一个名额可用）还有多少秒
    retry_after_s: float = 0.0
    backend: str = ""

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "limit": self.limit,
            "remaining": self.remaining,
            "window_s": self.window_s,
            "retry_after_s": round(self.retry_after_s, 2),
            "backend": self.backend,
        }


class RateLimiter(Protocol):
    def hit(self, key: str, limit: int, window_s: int) -> RateLimitDecision: ...
    def reset(self, key: str) -> None: ...


# ---------------------------------------------------------------------------
# Redis 实现
# ---------------------------------------------------------------------------
# KEYS[1] = 有序集合键
# ARGV[1] = 当前时间（毫秒）
# ARGV[2] = 窗口长度（毫秒）
# ARGV[3] = 上限
# ARGV[4] = 本次请求的唯一成员名
#
# 返回 {是否放行, 窗口内计数, 最早一次请求的时间戳}
_REDIS_SLIDING_WINDOW_LUA = """
local key      = KEYS[1]
local now_ms   = tonumber(ARGV[1])
local window   = tonumber(ARGV[2])
local limit    = tonumber(ARGV[3])
local member   = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, 0, now_ms - window)
local count = redis.call('ZCARD', key)

if count >= limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    return {0, count, tonumber(oldest[2]) or now_ms}
end

redis.call('ZADD', key, now_ms, member)
redis.call('PEXPIRE', key, window)
return {1, count + 1, now_ms}
"""


class RedisRateLimiter:
    """基于 Redis 有序集合的滑动窗口限流。

    用 Lua 脚本保证原子性；时间戳用**毫秒**，因为 Redis 的 Lua 把数字
    统一截断成整数 —— 用秒做单位的话同一个窗口内的请求会拿到相同分数，
    有序集合会当成同一个成员去重，限流直接失效。
    """

    backend = "redis"

    def __init__(self, client=None) -> None:  # noqa: ANN001
        self._client = client
        self._script = None

    def _get_client(self):  # noqa: ANN202
        if self._client is None:
            self._client = get_redis(DB_GATEWAY)
        return self._client

    def _get_script(self):  # noqa: ANN202
        if self._script is None:
            # register_script 走 EVALSHA，脚本内容只在首次传输，之后只传 40 字节的 sha1
            self._script = self._get_client().register_script(_REDIS_SLIDING_WINDOW_LUA)
        return self._script

    def hit(self, key: str, limit: int, window_s: int) -> RateLimitDecision:
        if limit <= 0:
            return RateLimitDecision(False, 0, 0, window_s, float(window_s), self.backend)

        now_ms = int(time.time() * 1000)
        # 成员名必须唯一：同一毫秒内的并发请求如果分数和成员都相同，
        # ZADD 会去重，等于少算一次请求（限流偏松，比偏紧更危险）
        member = f"{now_ms}-{uuid.uuid4().hex[:8]}"

        try:
            allowed, count, oldest_ms = self._get_script()(
                keys=[key],
                args=[now_ms, window_s * 1000, limit, member],
            )
        except Exception as exc:  # noqa: BLE001
            raise LimiterUnavailable(f"Redis 限流不可用：{type(exc).__name__}: {exc}") from exc

        allowed = bool(int(allowed))
        count = int(count)
        retry_after = (
            0.0
            if allowed
            else max(0.0, window_s - (now_ms - int(oldest_ms)) / 1000)
        )

        return RateLimitDecision(
            allowed=allowed,
            limit=limit,
            remaining=max(0, limit - count),
            window_s=window_s,
            retry_after_s=retry_after,
            backend=self.backend,
        )

    def reset(self, key: str) -> None:
        self._get_client().delete(key)


# ---------------------------------------------------------------------------
# 进程内实现
# ---------------------------------------------------------------------------
class MemoryRateLimiter:
    """进程内滑动窗口，用于单测和"没装 Redis"的降级场景。

    只在本进程内有效 —— 多 worker 部署时每个 worker 各限各的，
    实际通过量会变成 limit × worker 数。生产必须用 Redis 版。
    """

    backend = "memory"

    def __init__(self) -> None:
        self._log: dict[str, deque[float]] = {}
        # 请求线程会并发访问，dict/deque 的复合操作不是原子的
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window_s: int) -> RateLimitDecision:
        if limit <= 0:
            return RateLimitDecision(False, 0, 0, window_s, float(window_s), self.backend)

        # 用单调时钟：系统时间被 NTP 回拨时，wall clock 会让窗口算错
        now = time.monotonic()
        cutoff = now - window_s

        with self._lock:
            log = self._log.setdefault(key, deque())
            while log and log[0] <= cutoff:
                log.popleft()

            if len(log) >= limit:
                retry_after = max(0.0, window_s - (now - log[0]))
                return RateLimitDecision(
                    allowed=False,
                    limit=limit,
                    remaining=0,
                    window_s=window_s,
                    retry_after_s=retry_after,
                    backend=self.backend,
                )

            log.append(now)
            return RateLimitDecision(
                allowed=True,
                limit=limit,
                remaining=limit - len(log),
                window_s=window_s,
                backend=self.backend,
            )

    def reset(self, key: str) -> None:
        with self._lock:
            self._log.pop(key, None)


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
_memory_limiter = MemoryRateLimiter()


def get_rate_limiter(backend: str = "redis") -> RateLimiter:
    """按配置返回限流器。

    ``sqlite`` 也走内存实现 —— 限流数据是纯易失的，落盘没有意义，
    每次筛条件还要加锁，得不偿失。
    """
    if backend == "redis":
        return RedisRateLimiter()
    return _memory_limiter
