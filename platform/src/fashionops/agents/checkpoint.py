"""LangGraph checkpointer：把任务状态持久化，实现"中断可恢复"。

## 为什么这件事重要

没有 checkpointer 的 Agent 是这样的：跑 8 个节点，第 7 个超时挂了，
**整条链路从头再来** —— 前面 6 个节点消耗的 token 全部白费。

有了 checkpointer，状态在每个节点结束时落盘。恢复时从最后一个成功的
节点继续，前 6 个节点的结果直接读回来。对长链路任务来说，
这不只是省时间，而是"能不能跑完"的区别。

它还是 HITL（human-in-the-loop）的**存储基础**：
``interrupt()`` 中断时把状态存下来，人确认之后用同一个 ``thread_id``
恢复，图从断点接着走。

## 后端选择

| 后端 | 适用 | 说明 |
| --- | --- | --- |
| Redis | 开发 / 生产 | 跨进程共享（多个 uvicorn worker 都能读到同一个任务）；配了 AOF，进程崩了也不丢 |
| SQLite | 没装 Redis 时的降级 | 单进程可用；多 worker 下每个进程看到的状态不一致 |
| Memory | 单测 | 进程重启即丢，但测试跑得快、无外部依赖 |

**注意 Redis 和 SQLite 用的是不同的 db/文件**：
任务状态在 Redis db0（``DB_STATE``），限流和熔断在 db1。
分开是因为 db0 必须持久化，db1 是易失数据 —— 混在一起就没法分别设置策略。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from fashionops.cache import DB_STATE, redis_url_for_db
from fashionops.config import Settings, get_settings

logger = logging.getLogger("fashionops.agents.checkpoint")

#: 进程内缓存，避免每次构图都新建一个连接。
#:
#: **键必须带上存储位置**。一开始我只用了 backend 做键，结果是：
#: 只要后端还是 sqlite，换一个数据目录也会拿回旧的那个 saver ——
#: 图往 A 库写、查从 A 库读，看着自洽，但配置改了完全没生效。
#: 在生产上这比报错更危险：以为换了存储，实际一直在写旧地方。
_saver_cache: dict[str, BaseCheckpointSaver] = {}


def _cache_key(backend: str, settings: Settings) -> str:
    if backend == "redis":
        return f"redis::{redis_url_for_db(settings.redis_url, DB_STATE)}"
    if backend == "sqlite":
        return f"sqlite::{settings.checkpoint_db}"
    return f"{backend}::"


def get_checkpointer(
    settings: Settings | None = None,
    *,
    backend: str | None = None,
) -> BaseCheckpointSaver:
    """按配置返回 checkpointer。

    实现 ``setup()`` 的后端会在首次调用时建好索引/表 —— 每次都建会有开销，
    所以结果缓存在进程内。
    """
    settings = settings or get_settings()
    backend = backend or settings.cache_backend

    key = _cache_key(backend, settings)
    if key in _saver_cache:
        return _saver_cache[key]

    saver = _build(backend, settings)
    _saver_cache[key] = saver
    logger.info("checkpointer 后端 = %s", backend)
    return saver


def _build(backend: str, settings: Settings) -> BaseCheckpointSaver:
    if backend == "redis":
        try:
            from langgraph.checkpoint.redis import RedisSaver

            saver = RedisSaver(
                redis_url=redis_url_for_db(settings.redis_url, DB_STATE)
            )
            saver.setup()
            return saver
        except Exception as exc:  # noqa: BLE001
            # Redis 挂了不应该让整个应用起不来 —— 降级到 SQLite，
            # 功能上会退化成"单进程可恢复"，但比完全不能用强。
            logger.warning(
                "Redis checkpointer 初始化失败（%s: %s），降级到 SQLite",
                type(exc).__name__,
                exc,
            )
            return _build_sqlite(settings)

    if backend == "sqlite":
        return _build_sqlite(settings)

    from langgraph.checkpoint.memory import InMemorySaver

    return InMemorySaver()


def _build_sqlite(settings: Settings) -> BaseCheckpointSaver:
    from langgraph.checkpoint.sqlite import SqliteSaver

    path = settings.checkpoint_db
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        # 图可能在 FastAPI 的线程池里执行，连接要允许跨线程
        check_same_thread=False,
    )
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


def reset_checkpointer_cache() -> None:
    """清掉缓存的 saver。测试里换配置后要调用。"""
    _saver_cache.clear()


# ---------------------------------------------------------------------------
# 配置构造
# ---------------------------------------------------------------------------
def thread_config(
    task_no: str,
    ctx: Any | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """构造 LangGraph 的 ``RunnableConfig``。

    ``thread_id`` 用 ``task_no``：一个任务一个线程，
    这样"按任务号查历史状态"和"重跑某个任务"都很自然。

    ``ctx`` 是 ``RunContext``，不会进 checkpoint（它不是状态的一部分），
    只是随 config 传给节点。
    """
    configurable: dict[str, Any] = {"thread_id": task_no}
    if ctx is not None:
        configurable["ctx"] = ctx
    configurable.update(extra)
    return {"configurable": configurable}


def _as_checkpoint_dict(item: Any) -> dict[str, Any]:
    """把不同版本 checkpointer 的返回统一成 checkpoint 字典。

    ``langgraph-checkpoint`` 大版本间改过这里的形状：

    * 3.x 的 ``SqliteSaver.get()`` **直接返回 checkpoint 字典**，
      ``channel_values`` 就在顶层
    * 更早的版本返回 ``CheckpointTuple``，``channel_values`` 在 ``.checkpoint`` 里

    两种都兜住。不兜的话，依赖升级后这个函数会**静默返回 None** ——
    表现为"任务状态查不到"，而不是报错，最难查。
    """
    if isinstance(item, dict):
        return item
    inner = getattr(item, "checkpoint", None)
    if isinstance(inner, dict):
        return inner
    return {}


def _metadata_of(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item.get("metadata") or {}
    return getattr(item, "metadata", None) or {}


def latest_state(checkpointer: BaseCheckpointSaver, task_no: str) -> dict[str, Any] | None:
    """读回某个任务的最新状态快照。

    用途：让 API 能在**不重新跑图**的情况下展示任务进度 ——
    前端刷新页面时不需要触发任何计算。
    """
    result = checkpointer.get(thread_config(task_no))
    if result is None:
        return None
    values = _as_checkpoint_dict(result).get("channel_values")
    # 注意：这里可能返回 {}（状态存在但没写任何 channel）。
    # 调用方要区分"没有状态"（None）和"状态为空"（{}）。
    return values if isinstance(values, dict) else None


def pending_nodes(graph: Any, task_no: str) -> list[str]:
    """任务停在哪些节点上（即下一步要执行谁）。

    ``graph`` 是 ``builder.compile(checkpointer=...)`` 返回的编译后图。

    ## 为什么不能靠 ``state["current_node"]`` 判断

    节点里调用 ``interrupt()`` 时是**抛异常**中断的，节点函数末尾那句
    ``return {"current_node": "gate"}`` **根本不会执行**。
    所以中断之后 ``current_node`` 仍然是上一个已完成的节点（比如 ``select``），
    而不是停在的那个闸口节点。

    ## 为什么用 ``graph.get_state()`` 而不是自己解析 checkpoint

    "下一步是谁"这个信息在各版本里搬过家：早期在 checkpoint 的顶层
    ``next`` 字段，后来挪进了 pending_writes。
    而 ``StateSnapshot.next`` 是 LangGraph 公开且稳定的接口，
    不会因为内部结构调整就静默失效 —— 自己解析内部字段的话，
    升级后会表现为"查出来是空列表"，不报错，最难排查。
    """
    state = graph.get_state(thread_config(task_no))
    return list(state.next or [])


def snapshot(graph: Any, task_no: str) -> dict[str, Any]:
    """给 API 层用的任务状态快照。"""
    state = graph.get_state(thread_config(task_no))
    return {
        "next": list(state.next or []),
        "values": dict(state.values or {}),
        "created_at": getattr(state, "created_at", None),
        "step": (state.metadata or {}).get("step"),
        "source": (state.metadata or {}).get("source"),
    }


def history(checkpointer: BaseCheckpointSaver, task_no: str, limit: int = 20) -> list[dict]:
    """列出某个任务的历史检查点（从新到旧）。

    "可回放"这个能力的接口层：有了它才能回答
    "第 3 步的时候状态是什么样"。
    """
    items: list[dict] = []
    for entry in checkpointer.list(thread_config(task_no), limit=limit):
        checkpoint = _as_checkpoint_dict(entry)
        metadata = _metadata_of(entry)
        items.append(
            {
                "checkpoint_id": checkpoint.get("id"),
                "ts": checkpoint.get("ts"),
                "step": metadata.get("step"),
                "source": metadata.get("source"),
                "next": list(checkpoint.get("next") or []),
                "values": checkpoint.get("channel_values"),
            }
        )
    return items
