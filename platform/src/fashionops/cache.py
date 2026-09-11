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
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit

import redis

from fashionops.config import Settings, get_settings

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
