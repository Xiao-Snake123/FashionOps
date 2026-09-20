"""Redis 客户端工厂与键名约定。

## 数据库分工（对应 ``deploy/redis/redis.conf`` 里的 ``databases 16``）

| db | 用途 | 数据丢了会怎样 |
| --- | --- | --- |
| 0 | LangGraph 任务图 checkpoint | **严重** —— 任务无法从中断处恢复 |
| 1 | 网关的限流计数与熔断状态 | 无所谓 —— 重建即可，最多放过去几个请求 |
| 2 | 业务缓存（列表页、聚合结果） | 无所谓 —— 回源查库 |

分开的理由：db0 的数据必须保住（所以配了 AOF），
db1/db2 是纯易失数据，不需要持久化。混在一个 db 里就没法分别对待。

## 为什么要自己拼 db

``redis://127.0.0.1:6379/0`` 里的 ``/0`` 是路径，不能用字符串替换拼 ——
密码里也可能有 ``/``。这里用 ``urlsplit`` 只改 path 部分。
"""

from __future__ import annotations

import contextlib
import json
import logging
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import redis
from redis.exceptions import RedisError

from fashionops.config import Settings, get_settings

logger = logging.getLogger("fashionops.cache")

DB_STATE = 0  # 任务图 checkpoint
DB_GATEWAY = 1  # 限流 / 熔断
DB_CACHE = 2  # 业务缓存


def redis_url_for_db(url: str, db: int) -> str:
    """把连接串的数据库编号换成 ``db``。"""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{db}", parts.query, parts.fragment))


def build_redis_client(url: str, *, timeout: float = 5.0, decode: bool = True) -> redis.Redis:
    """建一个 Redis 客户端。

    三个参数都是"防卡死"用的：

    * ``socket_connect_timeout`` —— Redis 没起来时快速失败，而不是挂 30 秒
    * ``socket_timeout`` —— 命令执行超时，防止慢查询拖垮请求线程
    * ``health_check_interval`` —— 连接空闲太久后被服务端掐断、客户端却不知道，
      下次用时报 "Connection reset by peer"。定期探活能规避掉。
    """
    return redis.Redis.from_url(
        url,
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
        health_check_interval=30,
        decode_responses=decode,
    )


@lru_cache(maxsize=4)
def get_redis(db: int = DB_GATEWAY) -> redis.Redis:
    """按 db 号取客户端（进程内缓存，避免每次调用都新建连接池）。"""
    settings: Settings = get_settings()
    return build_redis_client(
        redis_url_for_db(settings.redis_url, db),
        timeout=settings.redis_socket_timeout,
    )


def reset_redis_clients() -> None:
    """清掉缓存的客户端。测试里改完配置需要调用。"""
    for db in (DB_STATE, DB_GATEWAY, DB_CACHE):
        # 关连接失败（比如本来就没连上）不影响后续流程，直接忽略
        with contextlib.suppress(Exception):
            get_redis(db).close()
    get_redis.cache_clear()


def redis_available() -> tuple[bool, str]:
    """探活，返回 ``(是否可用, 描述)``。给 ``/health`` 用。"""
    try:
        client = get_redis(DB_GATEWAY)
        info = client.info("server")
        return True, f"Redis {info.get('redis_version', '?')}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# 业务缓存（db2）
# ---------------------------------------------------------------------------
# 这一层是给"读多写少"的只读接口用的：首页概览、日度指标、类目/季节可选值、
# 知识库列表、库存告急。它们的共同点是**每次请求都要跑一遍聚合 SQL**，
# 而数据几分钟内变一次都无所谓。
#
# 三条设计约束：
#
# 1. **Redis 挂了不能连累业务**。所有 Redis 操作都吞掉异常：读不到就回源查库，
#    写不进就算了。缓存是加速器，不是依赖 —— /health 已经负责报 Redis 状态了，
#    这里不该再抛错打断请求。
# 2. **缓存必须能被整体关掉**。开关在 ``Settings.business_cache_enabled``。
#    测试里**必须关**：测试用临时 SQLite 库（每个用例一份），而 Redis 是共享的，
#    开着会让用例 A 的结果被用例 B 读到。conftest.py 里有 autouse 夹具负责关。
# 3. **失效靠版本号而不是逐个删 key**。``invalidate_namespace`` 只把一个计数器 +1，
#    后续拼出来的 key 自然落在新版本上，旧 key 等 TTL 到期自动消失。
#    比"找出所有相关 key 再 DEL"可靠得多 —— 后者一旦漏拼一个前缀就永久脏。

_VERSION_KEY = "cachever"
_CACHE_KEY = "cache"


def cache_enabled() -> bool:
    """业务缓存总开关（读配置，配置本身坏了就当关闭）。"""
    try:
        return bool(get_settings().business_cache_enabled)
    except Exception:  # noqa: BLE001
        return False


def _version(namespace: str) -> int:
    """取命名空间当前的版本号，读不到（Redis 挂了/未初始化）就当 0。"""
    try:
        client = get_redis(DB_CACHE)
        raw = client.get(f"{get_settings().redis_key_prefix}:{_VERSION_KEY}:{namespace}")
    except Exception:  # noqa: BLE001
        return 0
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


def business_cache_key(namespace: str, *parts: object) -> str:
    """拼缓存 key：``前缀:cache:命名空间:v版本号:参数``。

    ``parts`` 是区分不同查询条件的部分（类目、天数、阈值……），
    **必须把所有影响结果的参数都放进来**，否则不同筛选会互相返回对方的缓存。
    """
    suffix = "|".join(str(p) for p in parts)
    return f"{get_settings().redis_key_prefix}:{_CACHE_KEY}:{namespace}:v{_version(namespace)}:{suffix}"


def read_cached_json(key: str) -> Any | None:
    """读缓存，命中返回反序列化后的对象，未命中/不可用返回 ``None``。"""
    if not cache_enabled():
        return None
    try:
        client = get_redis(DB_CACHE)
        raw = client.get(key)
    except Exception as exc:  # noqa: BLE001
        logger.debug("业务缓存读失败，回源查库：%s", exc)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        # 缓存里存了不可解析的东西（比如别人手改过），当未命中处理
        logger.debug("业务缓存内容不可解析，回源查库：%s", exc)
        return None


def write_cached_json(key: str, payload: Any, ttl: int | None = None) -> None:
    """写缓存。写失败静默忽略 —— 缓存写不进去不影响正确性。"""
    if not cache_enabled():
        return
    seconds = ttl if ttl is not None else get_settings().business_cache_ttl
    try:
        client = get_redis(DB_CACHE)
        # default=str：Decimal / date / datetime 这类 JSON 原生不支持的类型兜底成字符串
        client.setex(key, int(seconds), json.dumps(payload, ensure_ascii=False, default=str))
    except (TypeError, ValueError, RedisError) as exc:
        logger.debug("业务缓存写失败，忽略：%s", exc)


def invalidate_namespace(namespace: str) -> None:
    """让某个命名空间的缓存整体失效（版本号 +1）。

    数据发生变更（比如重建知识库索引、改了商品类目）时调一下即可，
    不需要知道里面到底有哪些 key。
    """
    if not cache_enabled():
        return
    try:
        client = get_redis(DB_CACHE)
        client.incr(f"{get_settings().redis_key_prefix}:{_VERSION_KEY}:{namespace}")
    except Exception as exc:  # noqa: BLE001
        logger.debug("业务缓存失效失败，忽略：%s", exc)
