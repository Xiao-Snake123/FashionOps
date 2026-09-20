"""业务缓存（Redis db2）的单元测试。

## 为什么必须有这个文件

`conftest.py` 里有一个 autouse 夹具 `business_cache_off`，它**全局关闭**业务缓存 ——
不关的话测试会跨用例串数据：每个用例用的是自己那份临时 SQLite 库，
而 Redis 是全局共享的一份，用例 A 的缓存会被用例 B 读到。

但这也带来一个盲区：**所有其他用例永远走不到缓存那条分支**。
缓存代码写错了，574 个用例照样全绿 —— 它们只证明了"关掉缓存时行为不变"，
没证明"开着缓存时是对的"。

这个文件补上这块：把 Redis 换成替身，在同一个用例里把
**命中 / 穿透 / TTL / 版本号失效 / Redis 挂掉 / 开关关闭**逐一钉死。

## 为什么不用 fakeredis

要测的其中一条是"**Redis 不可用时要静默回源**"。用真的替身客户端，
这条只能靠停服务或断网来模拟，不可控也不该出现在单测里。
手写一个只实现 `get` / `setex` / `incr` 的替身，能把"挂掉"变成
一个可以精确触发的开关。
"""

from __future__ import annotations

import pytest
from redis.exceptions import RedisError
from sqlalchemy import event
from sqlalchemy.orm import Session


class _FakeRedis:
    """业务缓存用到的三个命令的替身。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.broken = False

    def _check(self) -> None:
        if self.broken:
            raise RedisError("模拟 Redis 不可用")

    def get(self, key: str):  # noqa: ANN201
        self._check()
        return self.store.get(key)

    def setex(self, key: str, ttl: int, value: str) -> None:
        self._check()
        self.store[key] = value
        self.ttls[key] = ttl

    def incr(self, key: str) -> int:
        self._check()
        current = int(self.store.get(key, "0")) + 1
        self.store[key] = str(current)
        return current


@pytest.fixture
def session(db):  # noqa: ANN001, ANN201
    with Session(db) as s:
        yield s


@pytest.fixture
def cache_env(monkeypatch):
    """打开业务缓存，并把 `get_redis` 换成替身。

    注意这里要**覆盖** conftest 里那个 autouse 的 `business_cache_off` ——
    autouse 夹具先跑，这里的 setattr 后跑，最终生效的是"打开"这份配置。
    """
    import fashionops.cache as cache_mod
    from fashionops.config import Settings

    fake = _FakeRedis()
    enabled = Settings(business_cache_enabled=True, business_cache_ttl=30)
    monkeypatch.setattr(cache_mod, "get_settings", lambda: enabled)
    monkeypatch.setattr(cache_mod, "get_redis", lambda db=None: fake)
    return fake


# ===========================================================================
# 读写基本语义
# ===========================================================================
def test_read_missing_key_returns_none(cache_env):  # noqa: ANN001
    from fashionops.cache import business_cache_key, read_cached_json

    assert read_cached_json(business_cache_key("nope")) is None


def test_write_then_read_roundtrip(cache_env):  # noqa: ANN001
    from fashionops.cache import business_cache_key, read_cached_json, write_cached_json

    key = business_cache_key("summary")
    write_cached_json(key, {"products": 3, "标题": "中文"})
    assert read_cached_json(key) == {"products": 3, "标题": "中文"}
    # 顺带确认真的写进了 Redis（而不是被静默吞掉）
    assert key in cache_env.store


def test_ttl_default_comes_from_settings(cache_env):  # noqa: ANN001
    from fashionops.cache import business_cache_key, write_cached_json

    key = business_cache_key("summary")
    write_cached_json(key, {"a": 1})
    assert cache_env.ttls[key] == 30, "不传 ttl 时应该落配置里的默认 30 秒"

    write_cached_json(key, {"a": 1}, ttl=7)
    assert cache_env.ttls[key] == 7


def test_key_must_differ_by_query_params(cache_env):  # noqa: ANN001
    """查询参数必须进 key：否则 threshold=5 的结果会被 threshold=50 命中。"""
    from fashionops.cache import business_cache_key

    assert business_cache_key("low_stock", 5) != business_cache_key("low_stock", 50)


def test_unparsable_payload_is_treated_as_miss(cache_env):  # noqa: ANN001
    from fashionops.cache import business_cache_key, read_cached_json

    key = business_cache_key("summary")
    cache_env.store[key] = "这不是 JSON"
    assert read_cached_json(key) is None


# ===========================================================================
# 失效
# ===========================================================================
def test_invalidate_bumps_version_and_orphans_old_key(cache_env):  # noqa: ANN001
    """失效靠版本号 +1，不是逐个删 key —— 旧 key 自然读不到，等 TTL 过期即可。"""
    from fashionops.cache import (
        business_cache_key,
        invalidate_namespace,
        read_cached_json,
        write_cached_json,
    )

    before = business_cache_key("summary")
    write_cached_json(before, {"products": 3})

    invalidate_namespace("summary")

    after = business_cache_key("summary")
    assert after != before, "失效后拼出来的 key 必须落在新版本上"
    assert read_cached_json(after) is None, "新 key 不该读到旧数据"


# ===========================================================================
# 降级：Redis 挂了 / 开关关了，都不能影响业务
# ===========================================================================
def test_redis_failure_is_silent(cache_env):  # noqa: ANN001
    """缓存是加速器不是依赖：Redis 挂掉时读要回源、写和失效要被吞掉。"""
    from fashionops.cache import (
        business_cache_key,
        invalidate_namespace,
        read_cached_json,
        write_cached_json,
    )

    cache_env.broken = True
    key = business_cache_key("summary")

    assert read_cached_json(key) is None, "读不到就该回源，不能抛"
    write_cached_json(key, {"a": 1})  # 不该抛
    invalidate_namespace("summary")  # 不该抛


def test_disabled_setting_means_zero_redis_traffic(monkeypatch):  # noqa: ANN001
    import fashionops.cache as cache_mod
    from fashionops.config import Settings

    fake = _FakeRedis()
    disabled = Settings(business_cache_enabled=False)
    monkeypatch.setattr(cache_mod, "get_settings", lambda: disabled)
    monkeypatch.setattr(cache_mod, "get_redis", lambda db=None: fake)

    key = cache_mod.business_cache_key("summary")
    cache_mod.write_cached_json(key, {"a": 1})
    assert cache_mod.read_cached_json(key) is None
    assert fake.store == {}, "开关关掉时一个字节都不该写进 Redis"


# ===========================================================================
# 接口真的命中了缓存
# ===========================================================================
def test_summary_second_call_hits_cache(cache_env, db, session):  # noqa: ANN001
    """首页概览第二次调用应该**一次库都不查**。

    用 SQL 执行次数来证明，而不是比返回值 —— 空库上比数字是恒真的断言，
    证明不了任何东西。
    """
    from fashionops.api.routers.system import summary

    hits = {"n": 0}

    def _count(*_args, **_kwargs):  # noqa: ANN002, ANN003
        hits["n"] += 1

    event.listen(db, "before_cursor_execute", _count)
    try:
        summary(session=session)
        assert hits["n"] > 0, "第一次应该真的查库"

        hits["n"] = 0
        summary(session=session)
        missed = hits["n"]
    finally:
        event.remove(db, "before_cursor_execute", _count)

    assert missed == 0, f"第二次应该完全命中缓存，结果又查了 {missed} 次库"
