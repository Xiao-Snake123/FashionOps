"""Redis 版限流器与熔断器的测试。

为什么要单独一个文件：内存实现全绿**不能说明 Redis 实现是对的**。
两者差着一段 Lua 脚本、键的 TTL、以及毫秒/秒单位换算 ——
这些都是内存版永远不会碰到的代码路径。

没有 Redis 时整个文件自动跳过，所以 CI 上不装中间件也能跑。
测试用的键统一带 ``testgw:`` 前缀，结束后清干净，不污染开发环境的数据。
"""

from __future__ import annotations

import time

import pytest

from fashionops.cache import DB_GATEWAY, get_redis
from fashionops.gateway import RedisCircuitBreaker, RedisRateLimiter

PREFIX = "testgw"


@pytest.fixture(scope="module")
def redis_client():
    try:
        client = get_redis(DB_GATEWAY)
        client.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"本机没有可用的 Redis，跳过：{type(exc).__name__}: {exc}")
    return client


@pytest.fixture
def clean_keys(redis_client):
    """每个用例前后清掉自己造的键。"""
    _purge(redis_client)
    yield
    _purge(redis_client)


def _purge(client) -> None:  # noqa: ANN001
    for key in client.scan_iter(match=f"{PREFIX}:*", count=200):
        client.delete(key)


def _key(name: str) -> str:
    return f"{PREFIX}:{name}:{time.time_ns()}"


# ===========================================================================
# 限流
# ===========================================================================
class TestRedisRateLimiter:
    def test_allows_then_rejects(self, redis_client, clean_keys):
        rl = RedisRateLimiter(client=redis_client)
        key = _key("rl")

        for i in range(3):
            decision = rl.hit(key, limit=3, window_s=60)
            assert decision.allowed, f"第 {i + 1} 次应该放行"
            assert decision.remaining == 3 - (i + 1)

        rejected = rl.hit(key, limit=3, window_s=60)
        assert not rejected.allowed
        assert rejected.retry_after_s > 0
        assert rejected.backend == "redis"

    def test_sliding_window_does_not_reset_at_boundary(self, redis_client, clean_keys):
        """这个用例盯的是"为什么不用固定窗口"。

        固定窗口在跨窗口的瞬间会清空计数，放过两倍流量。
        滑动窗口应该是"每个请求各自计时"。

        构造方式很关键：两个请求必须**错开**时间，否则它们会一起滑出窗口，
        验证不出任何区别。这里让它们相隔 0.6 秒（窗口 1 秒）：

            t=0.0  请求 A
            t=0.6  请求 B        → 窗口满
            t=1.1  窗口里只剩 B   → 应该只放出 1 个名额
                   （固定窗口在这里会把计数清零，直接放行 2 个）
        """
        rl = RedisRateLimiter(client=redis_client)
        key = _key("rl-window")

        assert rl.hit(key, limit=2, window_s=1).allowed  # A @ t=0.0
        time.sleep(0.6)
        assert rl.hit(key, limit=2, window_s=1).allowed  # B @ t=0.6
        assert not rl.hit(key, limit=2, window_s=1).allowed, "窗口已满"

        time.sleep(0.5)  # 现在 t≈1.1，A 已滑出，B 还在

        first = rl.hit(key, limit=2, window_s=1)
        assert first.allowed, "A 已滑出 1 秒窗口，应该腾出 1 个名额"
        assert first.remaining == 0
        assert not rl.hit(key, limit=2, window_s=1).allowed, (
            "B 还占着名额。固定窗口在这里会误放行 —— 这正是要避免的边界突发"
        )

    def test_sub_second_requests_are_not_deduplicated(self, redis_client, clean_keys):
        """同一毫秒内的请求不能被有序集合去重。

        如果成员名只用时间戳，同一毫秒的多次请求分数相同、成员也相同，
        ZADD 会当成同一个成员去重 —— 限流直接变松。所以成员名里必须带随机后缀。
        """
        rl = RedisRateLimiter(client=redis_client)
        key = _key("rl-burst")

        allowed = sum(1 for _ in range(20) if rl.hit(key, limit=20, window_s=60).allowed)
        assert allowed == 20, "同一毫秒内的并发请求被去重了，限流偏松"

    def test_keys_are_namespaced_by_caller(self, redis_client, clean_keys):
        """不同调用方各限各的。

        注意键**必须带 testgw: 前缀**，否则 clean_keys 清不掉，
        上一次运行留下的计数会污染这次 —— 这个坑我在写这个文件时又踩了一次。
        """
        rl = RedisRateLimiter(client=redis_client)
        key_a = _key("ns-a")
        key_b = _key("ns-b")

        assert rl.hit(key_a, 1, 60).allowed
        assert not rl.hit(key_a, 1, 60).allowed
        assert rl.hit(key_b, 1, 60).allowed, "另一个 key 不该受影响"


# ===========================================================================
# 熔断
# ===========================================================================
class TestRedisCircuitBreaker:
    def test_opens_after_threshold(self, redis_client, clean_keys):
        cb = RedisCircuitBreaker(client=redis_client, threshold=3, cooldown_s=30)
        key = _key("cb")

        assert cb.before_call(key).state == "closed"
        cb.record_failure(key)
        cb.record_failure(key)
        assert cb.before_call(key).allowed, "还没到阈值"

        cb.record_failure(key)
        decision = cb.before_call(key)
        assert not decision.allowed
        assert decision.retry_after_s > 0

    def test_half_open_allows_exactly_one_probe(self, redis_client, clean_keys):
        cb = RedisCircuitBreaker(client=redis_client, threshold=2, cooldown_s=1, probe_timeout_s=10)
        key = _key("cb-half")
        cb.record_failure(key)
        cb.record_failure(key)
        assert not cb.before_call(key).allowed

        time.sleep(1.05)

        assert cb.before_call(key).state == "half_open"
        # 第二个请求必须继续被挡：`SET NX` 抢不到探测名额
        assert not cb.before_call(key).allowed

    def test_probe_failure_reopens_immediately(self, redis_client, clean_keys):
        cb = RedisCircuitBreaker(client=redis_client, threshold=5, cooldown_s=5, probe_timeout_s=10)
        key = _key("cb-probe-fail")
        for _ in range(5):
            cb.record_failure(key)

        time.sleep(5.05)
        assert cb.before_call(key).state == "half_open"

        decision = cb.record_failure(key)
        assert not decision.allowed
        assert decision.retry_after_s > 0

    def test_probe_success_closes(self, redis_client, clean_keys):
        cb = RedisCircuitBreaker(client=redis_client, threshold=2, cooldown_s=1, probe_timeout_s=10)
        key = _key("cb-recover")
        cb.record_failure(key)
        cb.record_failure(key)
        time.sleep(1.05)
        assert cb.before_call(key).state == "half_open"

        cb.record_success(key)
        assert cb.before_call(key).state == "closed"
        assert cb.snapshot(key)["failures"] == 0

    def test_state_survives_new_client(self, redis_client, clean_keys):
        """熔断状态存 Redis 的意义就在这里：**跨进程/跨重启共享**。

        如果用进程内变量，重启之后冷却期归零，下游明明还在挂，
        我们又从头猛打一遍。
        """
        key = _key("cb-shared")
        cb1 = RedisCircuitBreaker(client=redis_client, threshold=2, cooldown_s=30)
        cb1.record_failure(key)
        cb1.record_failure(key)

        # 模拟另一个 worker（甚至另一个进程）拿到同一个键
        cb2 = RedisCircuitBreaker(client=redis_client, threshold=2, cooldown_s=30)
        assert not cb2.before_call(key).allowed, "熔断状态没有跨实例共享"
