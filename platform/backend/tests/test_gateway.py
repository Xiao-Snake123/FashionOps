"""工具网关测试。

刻意**不依赖 Redis**：全部用内存实现，保证 CI 上不装中间件也能跑。
Redis 版的实现由 ``test_gateway_redis.py`` 单独验证（没有 Redis 时自动跳过）。

测试重点不是"代码能跑"，而是把几个容易写错的地方钉死：

* 限流的窗口边界（滑动窗口不能放过两倍流量）
* 熔断的半开态只能放**一个**探测请求
* 探测失败要**立即**重新熔断，而不是等计数器慢慢涨
* 只读 fail-open / 写操作 fail-closed 的区分
* 幂等：重复调用返回首次结果，且下游只被真正调用一次
* 被拒绝的调用也要留审计
"""

from __future__ import annotations

import time

import pytest
from pydantic import BaseModel

from fashionops.gateway import (
    CallContext,
    ErrorCode,
    IdempotencyStore,
    MemoryAuditSink,
    MemoryCircuitBreaker,
    MemoryRateLimiter,
    Scope,
    ToolGateway,
    ToolRegistry,
)
from fashionops.gateway.audit import sanitize
from fashionops.gateway.registry import from_llm_name, to_llm_name

# ---------------------------------------------------------------------------
# 测试用工具
# ---------------------------------------------------------------------------


class EchoArgs(BaseModel):
    text: str
    count: int = 1


class WriteArgs(BaseModel):
    spu_code: str


class BigArgs(BaseModel):
    """故意带一个敏感字段和一个超长字段，用来验证审计脱敏。"""

    title: str
    api_key: str = "should-be-masked"


#: 记录每个工具被真正调用了多少次（用来验证幂等和重试次数）
call_counter: dict[str, int] = {}


def _reset_counter() -> None:
    call_counter.clear()


def build_registry() -> ToolRegistry:
    reg = ToolRegistry()

    @reg.tool(
        name="demo.echo",
        description="回显文本",
        args_model=EchoArgs,
        scopes=(Scope.ERP_READ,),
        read_only=True,
    )
    def echo(text: str, count: int = 1) -> dict:
        call_counter["demo.echo"] = call_counter.get("demo.echo", 0) + 1
        return {"echo": text * count}

    @reg.tool(
        name="demo.list_items",
        description="列出一批条目",
        args_model=EchoArgs,
        read_only=True,
    )
    def list_items(text: str, count: int = 1) -> dict:
        return {"items": [{"n": i, "text": text} for i in range(count)]}

    @reg.tool(
        name="demo.create_listing",
        description="上架商品（幂等）",
        args_model=WriteArgs,
        scopes=(Scope.ECOM_WRITE,),
        read_only=False,
        idempotent=True,
    )
    def create_listing(spu_code: str) -> dict:
        call_counter["demo.create_listing"] = call_counter.get("demo.create_listing", 0) + 1
        return {"listing_id": f"L-{spu_code}", "spu_code": spu_code}

    @reg.tool(
        name="demo.save_secret",
        description="带敏感字段的写操作",
        args_model=BigArgs,
        read_only=False,
    )
    def save_secret(title: str, api_key: str = "") -> dict:
        return {"saved": title}

    @reg.tool(
        name="demo.always_fail",
        description="总是失败",
        args_model=EchoArgs,
        read_only=True,
    )
    def always_fail(text: str, count: int = 1) -> dict:
        call_counter["demo.always_fail"] = call_counter.get("demo.always_fail", 0) + 1
        raise RuntimeError("上游炸了")

    @reg.tool(
        name="demo.sleep",
        description="慢工具",
        args_model=EchoArgs,
        read_only=True,
        timeout_s=0.2,
    )
    def sleep_tool(text: str, count: int = 1) -> dict:
        time.sleep(1.0)
        return {"slept": True}

    return reg


def build_gateway(
    *,
    rate_limit_per_minute: int = 5,
    max_retries: int = 1,
    breaker_threshold: int = 3,
    breaker_cooldown: int = 1,
) -> tuple[ToolGateway, MemoryAuditSink, MemoryCircuitBreaker]:
    audit = MemoryAuditSink()
    breaker = MemoryCircuitBreaker(threshold=breaker_threshold, cooldown_s=breaker_cooldown)
    gw = ToolGateway(
        build_registry(),
        rate_limiter=MemoryRateLimiter(),
        breaker=breaker,
        audit_sink=audit,
        # 幂等存储必须关掉 Redis：测试之间会通过 Redis 悄悄串数据，
        # 上一个用例写的缓存键被下一个用例命中，报出看起来毫无道理的失败。
        # 这类跨用例污染依赖执行顺序，最难排查。
        idempotency=IdempotencyStore(use_redis=False),
        rate_limit_per_minute=rate_limit_per_minute,
        max_retries=max_retries,
    )
    return gw, audit, breaker


@pytest.fixture(autouse=True)
def _clean_counter():
    _reset_counter()
    yield
    _reset_counter()


def ctx(*scopes: str, **kwargs) -> CallContext:  # noqa: ANN003
    return CallContext(actor="test_agent", scopes=frozenset(scopes), **kwargs)


# ===========================================================================
# 注册表
# ===========================================================================
class TestRegistry:
    def test_tool_names_are_dotted(self):
        reg = build_registry()
        assert "demo.echo" in reg
        assert len(reg) == 6

    def test_duplicate_registration_rejected(self):
        reg = ToolRegistry()

        class A(BaseModel):
            x: int = 1

        reg.tool(name="x.y", description="d", args_model=A)(lambda x=1: x)
        with pytest.raises(ValueError, match="工具重名"):
            reg.tool(name="x.y", description="d", args_model=A)(lambda x=1: x)

    def test_llm_name_mapping(self):
        """OpenAI 的 function name 不允许点号，必须转成双下划线。"""
        assert to_llm_name("erp.query_inventory") == "erp__query_inventory"
        assert from_llm_name("erp__query_inventory") == "erp.query_inventory"
        # 往返一圈要回到原点
        for original in ("ad.update_budget", "kb.search", "a.b.c"):
            assert from_llm_name(to_llm_name(original)) == original

    def test_openai_schema_uses_llm_name(self):
        reg = build_registry()
        schemas = {s["function"]["name"] for s in reg.openai_schemas()}
        assert "demo__echo" in schemas
        assert all("." not in name for name in schemas)

    def test_openai_schema_scope_filtering(self):
        """没权限的工具不要暴露给模型 —— 模型看见就会去调，调了被拒纯浪费 token。"""
        reg = build_registry()

        # 没有任何权限 → 只能看到不需要权限的那 4 个（demo.echo 需要 ERP_READ）
        names = {s["function"]["name"] for s in reg.openai_schemas(frozenset())}
        assert "demo__echo" not in names
        assert names == {
            "demo__list_items",
            "demo__save_secret",
            "demo__always_fail",
            "demo__sleep",
        }

        # 拿到 ERP_READ → 多看到 demo.echo
        names = {s["function"]["name"] for s in reg.openai_schemas(frozenset({Scope.ERP_READ}))}
        assert "demo__echo" in names
        assert "demo__create_listing" not in names

        # 拿到 ECOM_WRITE → 能看上架工具
        names = {s["function"]["name"] for s in reg.openai_schemas(frozenset({Scope.ECOM_WRITE}))}
        assert "demo__create_listing" in names

    def test_openai_schema_none_means_no_filtering(self):
        """不传 scopes 表示不做过滤（给运维/调试看的全量清单）。"""
        reg = build_registry()
        assert len(reg.openai_schemas()) == 6

    def test_find_by_llm_name(self):
        reg = build_registry()
        assert reg.find_by_llm_name("demo__echo").name == "demo.echo"

    def test_unknown_tool_raises(self):
        reg = build_registry()
        with pytest.raises(KeyError):
            reg.get("nope.nope")


# ===========================================================================
# 限流
# ===========================================================================
class TestRateLimiter:
    def test_allows_up_to_limit_then_rejects(self):
        rl = MemoryRateLimiter()
        for i in range(3):
            decision = rl.hit("k", limit=3, window_s=60)
            assert decision.allowed, f"第 {i + 1} 次应该放行"
            assert decision.remaining == 3 - (i + 1)

        rejected = rl.hit("k", limit=3, window_s=60)
        assert not rejected.allowed
        assert rejected.remaining == 0
        assert rejected.retry_after_s > 0

    def test_different_keys_are_independent(self):
        rl = MemoryRateLimiter()
        for _ in range(3):
            rl.hit("a", limit=3, window_s=60)
        assert not rl.hit("a", limit=3, window_s=60).allowed
        assert rl.hit("b", limit=3, window_s=60).allowed

    def test_window_slides(self):
        """窗口是真的在滑动，不是到点清零 —— 隔 0.3s 后应该只放出 1 个名额。"""
        rl = MemoryRateLimiter()
        rl.hit("k", limit=2, window_s=1)
        time.sleep(0.6)
        rl.hit("k", limit=2, window_s=1)
        assert not rl.hit("k", limit=2, window_s=1).allowed

        time.sleep(0.5)  # 第一次请求已滑出 1 秒窗口
        assert rl.hit("k", limit=2, window_s=1).allowed

    def test_zero_limit_rejects_everything(self):
        rl = MemoryRateLimiter()
        assert not rl.hit("k", limit=0, window_s=60).allowed


# ===========================================================================
# 熔断
# ===========================================================================
class TestCircuitBreaker:
    def test_opens_after_threshold(self):
        cb = MemoryCircuitBreaker(threshold=3, cooldown_s=10)
        assert cb.before_call("t").allowed

        for i in range(2):
            cb.record_failure("t")
            assert cb.before_call("t").allowed, f"第 {i + 1} 次失败还不该熔断"

        cb.record_failure("t")
        decision = cb.before_call("t")
        assert not decision.allowed
        assert decision.retry_after_s > 0

    def test_success_resets_failures(self):
        cb = MemoryCircuitBreaker(threshold=3, cooldown_s=10)
        cb.record_failure("t")
        cb.record_failure("t")
        cb.record_success("t")  # 中间成功一次 → 计数清零
        cb.record_failure("t")
        cb.record_failure("t")
        assert cb.before_call("t").allowed, "成功之后计数应该清零，不该熔断"

    def test_half_open_allows_exactly_one_probe(self):
        """这是熔断器最容易写错的地方：冷却结束不能把闸门全开。"""
        cb = MemoryCircuitBreaker(threshold=2, cooldown_s=1, probe_timeout_s=10)
        cb.record_failure("t")
        cb.record_failure("t")
        assert not cb.before_call("t").allowed

        time.sleep(1.05)  # 等冷却期结束

        first = cb.before_call("t")
        assert first.allowed
        assert first.state == "half_open"

        # 第二个请求必须继续被挡 —— 否则就是一波流量把刚恢复的下游再打死
        second = cb.before_call("t")
        assert not second.allowed

    def test_probe_failure_reopens_immediately(self):
        """探测失败要立即重新熔断，不能等失败计数慢慢涨到阈值。"""
        cb = MemoryCircuitBreaker(threshold=5, cooldown_s=5, probe_timeout_s=10)
        for _ in range(5):
            cb.record_failure("t")
        assert not cb.before_call("t").allowed

        time.sleep(5.05)
        assert cb.before_call("t").state == "half_open"

        decision = cb.record_failure("t")  # 探测失败
        assert not decision.allowed, "探测失败必须立即重新熔断"
        assert decision.retry_after_s > 0

    def test_probe_success_closes(self):
        cb = MemoryCircuitBreaker(threshold=2, cooldown_s=1, probe_timeout_s=10)
        cb.record_failure("t")
        cb.record_failure("t")
        time.sleep(1.05)
        assert cb.before_call("t").state == "half_open"

        cb.record_success("t")
        assert cb.before_call("t").state == "closed"
        assert cb.snapshot("t")["failures"] == 0


# ===========================================================================
# 审计脱敏
# ===========================================================================
class TestSanitize:
    def test_masks_sensitive_keys(self):
        result = sanitize({"api_key": "sk-real-secret", "title": "秋季连衣裙"})
        assert result["api_key"] == "***"
        assert result["title"] == "秋季连衣裙"

    def test_masks_nested(self):
        result = sanitize({"outer": {"password": "p", "token": "t", "keep": 1}})
        assert result["outer"]["password"] == "***"
        assert result["outer"]["token"] == "***"
        assert result["outer"]["keep"] == 1

    def test_masks_inside_lists(self):
        result = sanitize({"users": [{"mobile": "13800000000", "name": "张三"}]})
        assert result["users"][0]["mobile"] == "***"
        assert result["users"][0]["name"] == "张三"

    def test_truncates_long_strings(self):
        result = sanitize({"body": "x" * 1000})
        assert len(result["body"]) < 1000
        assert "截断" in result["body"]

    def test_non_json_types_become_strings(self):
        from decimal import Decimal

        result = sanitize({"amount": Decimal("12.34")})
        assert isinstance(result["amount"], str)


# ===========================================================================
# 网关端到端
# ===========================================================================
class TestGatewayHappyPath:
    def test_success(self):
        gw, audit, _ = build_gateway()
        result = gw.call("demo.echo", {"text": "hi", "count": 2}, ctx=ctx(Scope.ERP_READ))
        assert result.ok
        assert result.data == {"echo": "hihi"}
        assert result.trace_id
        assert len(audit.entries) == 1
        assert audit.entries[0].status == "success"

    def test_args_model_accepted(self):
        gw, _, _ = build_gateway()
        result = gw.call("demo.echo", EchoArgs(text="x"), ctx=ctx(Scope.ERP_READ))
        assert result.ok
        assert result.data == {"echo": "x"}

    def test_defaults_are_applied(self):
        gw, _, _ = build_gateway()
        result = gw.call("demo.echo", {"text": "a"}, ctx=ctx(Scope.ERP_READ))
        assert result.data == {"echo": "a"}


class TestGatewayGuardrails:
    def test_tool_not_found(self):
        gw, audit, _ = build_gateway()
        result = gw.call("nope.nope", {}, ctx=ctx())
        assert not result.ok
        assert result.error.code is ErrorCode.TOOL_NOT_FOUND
        assert "demo.echo" in result.error.hint  # hint 里要给出可用工具
        assert audit.entries[-1].status == "blocked"

    def test_permission_denied(self):
        gw, _, _ = build_gateway()
        result = gw.call("demo.create_listing", {"spu_code": "SPU1"}, ctx=ctx())
        assert not result.ok
        assert result.error.code is ErrorCode.PERMISSION_DENIED
        assert Scope.ECOM_WRITE in result.error.hint
        assert call_counter.get("demo.create_listing", 0) == 0, "没权限就不该真的执行"

    def test_invalid_arguments(self):
        gw, _, _ = build_gateway()
        result = gw.call("demo.echo", {"count": 3}, ctx=ctx(Scope.ERP_READ))  # 缺 text
        assert not result.ok
        assert result.error.code is ErrorCode.INVALID_ARGUMENTS
        assert "text" in result.error.hint

    def test_rate_limited(self):
        gw, audit, _ = build_gateway(rate_limit_per_minute=2)
        for _ in range(2):
            assert gw.call("demo.echo", {"text": "x"}, ctx=ctx(Scope.ERP_READ)).ok

        result = gw.call("demo.echo", {"text": "x"}, ctx=ctx(Scope.ERP_READ))
        assert not result.ok
        assert result.error.code is ErrorCode.RATE_LIMITED
        assert "秒后重试" in result.error.hint
        assert audit.entries[-1].status == "blocked"
        assert audit.entries[-1].error_code == str(ErrorCode.RATE_LIMITED)

    def test_rate_limit_is_per_actor(self):
        gw, _, _ = build_gateway(rate_limit_per_minute=1)
        assert gw.call("demo.echo", {"text": "x"}, ctx=ctx(Scope.ERP_READ)).ok
        assert not gw.call("demo.echo", {"text": "x"}, ctx=ctx(Scope.ERP_READ)).ok

        other = CallContext(actor="另一个_agent", scopes=frozenset({Scope.ERP_READ}))
        assert gw.call("demo.echo", {"text": "x"}, ctx=other).ok

    def test_circuit_opens_and_blocks(self):
        gw, _, _ = build_gateway(breaker_threshold=3, max_retries=0)
        for _ in range(3):
            result = gw.call("demo.always_fail", {"text": "x"}, ctx=ctx())
            assert not result.ok

        result = gw.call("demo.always_fail", {"text": "x"}, ctx=ctx())
        assert result.error.code is ErrorCode.CIRCUIT_OPEN
        assert "熔断" in result.error.message
        # 关键：熔断后下游一次都不该被调用
        assert call_counter["demo.always_fail"] == 3


class TestRetry:
    def test_retries_retryable_failure(self):
        gw, _, _ = build_gateway(max_retries=2)
        result = gw.call("demo.always_fail", {"text": "x"}, ctx=ctx())
        assert not result.ok
        assert result.attempts == 3, "max_retries=2 应该总共尝试 3 次"
        assert call_counter["demo.always_fail"] == 3

    def test_timeout(self):
        gw, _, _ = build_gateway(max_retries=0)
        started = time.perf_counter()
        result = gw.call("demo.sleep", {"text": "x"}, ctx=ctx())
        elapsed = time.perf_counter() - started

        assert not result.ok
        assert result.error.code is ErrorCode.UPSTREAM_TIMEOUT
        # 这个断言是重点：如果 _invoke 用了 `with ThreadPoolExecutor(...)`，
        # 退出时会 shutdown(wait=True)，就会一直等到 sleep 结束（≥1s），
        # 超时形同虚设。这里必须明显小于 1 秒。
        assert elapsed < 0.8, f"超时没生效，实际等了 {elapsed:.2f}s"


class TestIdempotency:
    @staticmethod
    def _ctx(key: str) -> CallContext:
        # idempotency_key 挂在 CallContext 上而不是 call() 的参数：
        # 一个 Agent 节点通常复用同一个 context 发多次调用，写在这里更自然
        return CallContext(
            actor="listing_agent",
            scopes=frozenset({Scope.ECOM_WRITE}),
            idempotency_key=key,
        )

    def test_repeat_call_returns_first_result(self):
        gw, _, _ = build_gateway()
        first = gw.call("demo.create_listing", {"spu_code": "SPU9"}, ctx=self._ctx("task-1"))
        same = gw.call("demo.create_listing", {"spu_code": "SPU9"}, ctx=self._ctx("task-1"))

        assert first.ok and same.ok
        assert first.data == same.data
        assert not first.cached
        assert same.cached
        assert call_counter["demo.create_listing"] == 1, "幂等命中的那次不该再打下游"

    def test_different_keys_execute_twice(self):
        gw, _, _ = build_gateway()
        gw.call("demo.create_listing", {"spu_code": "SPU9"}, ctx=self._ctx("task-1"))
        gw.call("demo.create_listing", {"spu_code": "SPU9"}, ctx=self._ctx("task-2"))
        assert call_counter["demo.create_listing"] == 2

    def test_same_key_but_different_args_is_not_deduped(self):
        """这个用例盯的是一个很容易写错、且错了很难发现的地方。

        同一个节点里用**不同参数**调同一个工具时，如果幂等键只用了业务键
        （task-1），第二次会直接返回第一次的缓存 —— 也就是上架 A 商品时
        却拿到 B 商品的结果。静默的错误比报错危险得多。

        所以幂等键必须带上入参摘要。
        """
        gw, _, _ = build_gateway()
        ctx = self._ctx("same-task")
        a = gw.call("demo.create_listing", {"spu_code": "SPU-A"}, ctx=ctx)
        b = gw.call("demo.create_listing", {"spu_code": "SPU-B"}, ctx=ctx)

        assert a.data["spu_code"] == "SPU-A"
        assert b.data["spu_code"] == "SPU-B", "参数不同却被去重了，返回了上一个商品的结果"
        assert not b.cached
        assert call_counter["demo.create_listing"] == 2

    def test_write_without_key_is_not_idempotent(self):
        gw, _, _ = build_gateway()
        scopes = ctx(Scope.ECOM_WRITE)
        gw.call("demo.create_listing", {"spu_code": "SPU9"}, ctx=scopes)
        gw.call("demo.create_listing", {"spu_code": "SPU9"}, ctx=scopes)
        assert call_counter["demo.create_listing"] == 2


class TestAuditTrail:
    def test_audit_masks_sensitive_args(self):
        gw, audit, _ = build_gateway()
        gw.call(
            "demo.save_secret",
            {"title": "国庆活动", "api_key": "sk-live-abcdef"},
            ctx=ctx(),
        )
        entry = audit.entries[-1]
        assert entry.args_digest["api_key"] == "***"
        assert entry.args_digest["title"] == "国庆活动"
        # 整个条目里都不该出现明文密钥
        assert "sk-live-abcdef" not in str(entry.to_row())

    def test_audit_records_every_outcome(self):
        gw, audit, _ = build_gateway(rate_limit_per_minute=1)
        gw.call("demo.echo", {"text": "x"}, ctx=ctx(Scope.ERP_READ))  # success
        gw.call("nope.nope", {}, ctx=ctx())  # blocked
        gw.call("demo.echo", {"text": "x"}, ctx=ctx(Scope.ERP_READ))  # rate limited

        statuses = [e.status for e in audit.entries]
        assert statuses == ["success", "blocked", "blocked"]

    def test_audit_failure_does_not_break_call(self):
        """审计是旁路。写审计失败绝不能让工具调用失败。

        这个不变式必须由**网关**保证，而不是指望每个 sink 自己吞异常 ——
        sink 是可插拔的，第三方实现里一个没注意的 raise 就能把业务搞挂。
        这里故意用一个"连吞都不吞"的 sink，确认网关兜住了。
        """

        class ExplodingSink:
            def record(self, entry):  # noqa: ANN001, ANN201
                raise RuntimeError("审计库挂了")

        gw = ToolGateway(
            build_registry(),
            rate_limiter=MemoryRateLimiter(),
            breaker=MemoryCircuitBreaker(),
            audit_sink=ExplodingSink(),
        )
        result = gw.call("demo.echo", {"text": "x"}, ctx=ctx(Scope.ERP_READ))
        assert result.ok, "审计 sink 抛异常不应该让工具调用失败"
        assert result.data == {"echo": "x"}

    def test_fanout_sink_isolates_failures(self):
        """扇出的多个 sink 之间不能互相影响。"""
        from fashionops.gateway import FanoutAuditSink

        class Bad:
            def record(self, entry):  # noqa: ANN001, ANN201
                raise RuntimeError("boom")

        good = MemoryAuditSink()
        fanout = FanoutAuditSink(Bad(), good)
        gw = ToolGateway(
            build_registry(),
            rate_limiter=MemoryRateLimiter(),
            breaker=MemoryCircuitBreaker(),
            audit_sink=fanout,
        )
        assert gw.call("demo.echo", {"text": "x"}, ctx=ctx(Scope.ERP_READ)).ok
        assert len(good.entries) == 1, "坏 sink 不应该影响好 sink"

    def test_audit_has_trace_id_and_task_id(self):
        gw, audit, _ = build_gateway()
        gw.call("demo.echo", {"text": "x"}, ctx=ctx(Scope.ERP_READ, task_id=42, trace_id="tr-1"))
        entry = audit.entries[-1]
        assert entry.trace_id == "tr-1"
        assert entry.task_id == 42

    def test_default_sink_is_durable_not_memory(self):
        """**不传 ``audit_sink`` 时的默认值必须是落库的。**

        ## 这条守的是什么

        这里原来是 ``MemoryAuditSink()``，也就是"忘记传 audit_sink = 审计重启即丢"。
        后果是实打实的：只有 MCP 入口显式传了落库 sink，平台自己的三条链路
        （``agents/loop.py`` / ``service/agent.py`` / ``chat/agent.py``）全落在内存里
        —— **"Agent 调了哪些工具"在生产上查不到，而代码看起来治理齐全**。

        默认值必须落在"漏配更保守"的那一侧：漏配的代价是"多写一行审计"，
        而不是"出了事无据可查"。谁把默认改回内存实现，这条会红。

        （conftest 的 ``audit_writes_off`` 只把 ``record`` 改成空操作，
        保留对象身份，所以这里断言类型仍然有效。）
        """
        from fashionops.gateway.audit import SqlAuditSink

        gw = ToolGateway(
            build_registry(),
            rate_limiter=MemoryRateLimiter(),
            breaker=MemoryCircuitBreaker(),
        )
        assert isinstance(gw.audit, SqlAuditSink), "默认审计 sink 不能是内存实现"

    def test_memory_sink_still_available_when_asked(self):
        """想用内存 sink（单测 / 纯计算脚本）必须仍然能**显式**指定。

        默认从严，但不能把另一条路堵死 —— 否则那些不该碰数据库的场景
        （比如不连库的纯计算脚本）就没法用网关了，人就会去绕过网关，
        那就是比"审计缺失"更糟的结果。
        """
        audit = MemoryAuditSink()
        gw = ToolGateway(
            build_registry(),
            rate_limiter=MemoryRateLimiter(),
            breaker=MemoryCircuitBreaker(),
            audit_sink=audit,
        )
        assert gw.audit is audit


class TestFailOpenFailClosed:
    """限流/熔断组件挂掉时的降级策略 —— 这是设计里最需要想清楚的一块。"""

    class BrokenLimiter:
        def hit(self, key, limit, window_s):  # noqa: ANN001, ANN201
            from fashionops.gateway import LimiterUnavailable

            raise LimiterUnavailable("Redis 连接被拒绝")

        def reset(self, key):  # noqa: ANN001, ANN201
            pass

    def _gw(self, audit: MemoryAuditSink | None = None) -> ToolGateway:
        return ToolGateway(
            build_registry(),
            rate_limiter=self.BrokenLimiter(),
            breaker=MemoryCircuitBreaker(),
            audit_sink=audit or MemoryAuditSink(),
            idempotency=None,
        )

    def test_read_only_fails_open(self):
        """只读工具在限流组件挂掉时放行 —— 查不到库存比查不了库存更糟。"""
        gw = self._gw()
        result = gw.call("demo.echo", {"text": "x"}, ctx=ctx(Scope.ERP_READ))
        assert result.ok, "只读工具应该 fail-open"

    def test_write_fails_closed(self):
        """写操作在限流组件挂掉时拒绝 —— 没有限流保护就不该发写请求。"""
        gw = self._gw()
        result = gw.call("demo.create_listing", {"spu_code": "SPU1"}, ctx=ctx(Scope.ECOM_WRITE))
        assert not result.ok
        assert result.error.code is ErrorCode.RATE_LIMITED
        assert "fail-closed" in result.error.message
        assert call_counter.get("demo.create_listing", 0) == 0


class TestStats:
    def test_stats_track_outcomes(self):
        gw, _, _ = build_gateway(rate_limit_per_minute=1)
        gw.call("demo.echo", {"text": "x"}, ctx=ctx(Scope.ERP_READ))
        gw.call("demo.echo", {"text": "x"}, ctx=ctx(Scope.ERP_READ))

        stats = gw.stats_snapshot()["tools"]["demo.echo"]
        assert stats["calls"] == 2
        assert stats["success"] == 1
        assert stats["rate_limited"] == 1
        assert stats["success_rate"] == 0.5


class TestErrorContract:
    """错误契约本身也是被测对象 —— 它是给模型看的接口。"""

    def test_every_gateway_error_has_hint(self):
        gw, _, _ = build_gateway(rate_limit_per_minute=1)
        cases = [
            gw.call("nope.nope", {}, ctx=ctx()),  # TOOL_NOT_FOUND
            gw.call("demo.create_listing", {"spu_code": "S"}, ctx=ctx()),  # PERMISSION_DENIED
            gw.call("demo.echo", {"count": 1}, ctx=ctx(Scope.ERP_READ)),  # INVALID_ARGUMENTS
            gw.call("demo.echo", {"text": "x"}, ctx=ctx(Scope.ERP_READ)),  # 第 1 次：成功
            gw.call("demo.echo", {"text": "x"}, ctx=ctx(Scope.ERP_READ)),  # 第 2 次：超限
        ]
        failures = [c for c in cases if not c.ok]
        assert len(failures) == 4, "前三个 + 最后一个应该是失败"
        for result in failures:
            assert result.error is not None
            assert result.error.hint, f"{result.error.code} 缺少 hint"

    def test_error_serializes_to_plain_json(self):
        import json

        gw, _, _ = build_gateway()
        result = gw.call("nope.nope", {}, ctx=ctx())
        payload = result.to_dict()
        text = json.dumps(payload, ensure_ascii=False)
        assert "TOOL_NOT_FOUND" in text
        assert "hint" in payload["error"]

    def test_unwrap_raises_on_failure(self):
        from fashionops.gateway import ToolCallFailed

        gw, _, _ = build_gateway()
        result = gw.call("nope.nope", {}, ctx=ctx())
        with pytest.raises(ToolCallFailed):
            result.unwrap()
