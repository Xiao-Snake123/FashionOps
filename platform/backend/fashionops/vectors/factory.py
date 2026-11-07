"""向量库后端选择与单例。

## 降级的取舍

``vector_backend=qdrant`` 但 Qdrant 连不上时，**不自动降级到内存**，
而是让调用方拿到明确的失败。

为什么和别的组件不一样（Redis 挂了会自动降级到内存限流器）：
限流降级只影响"拦不拦得住请求"，是安全边际的问题；
向量库降级到内存意味着**索引是空的** —— 检索会返回"没有相关内容"，
而客服 Agent 会把这句话当成事实告诉用户。**静默变空比报错危险得多。**

所以这里只做"探测 + 明确报错"，把降级决定权交给调用方。
"""

from __future__ import annotations

import logging

from fashionops.config import Settings, get_settings
from fashionops.vectors.base import VectorStore
from fashionops.vectors.memory_store import MemoryVectorStore
from fashionops.vectors.qdrant_store import QdrantVectorStore

logger = logging.getLogger("fashionops.vectors")

_store: VectorStore | None = None


def build_vector_store(settings: Settings | None = None) -> VectorStore:
    """按配置**新建**一个后端（不走单例）。

    测试和 CLI 用这个 —— 它们需要在同一进程里拿到干净、独立的实例。
    """
    settings = settings or get_settings()
    backend = (settings.vector_backend or "qdrant").lower()

    if backend in ("memory", "none"):
        return MemoryVectorStore()

    if backend == "qdrant":
        return QdrantVectorStore(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key,
            timeout=settings.qdrant_timeout,
        )

    if backend == "chroma":
        raise NotImplementedError(
            "Chroma 后端尚未实现。它的过滤能力不足以支撑按类目/状态过滤的检索场景，"
            "规划里已改用 Qdrant（见 docs/07 §10.1）。"
            "要临时离线跑，把 FASHIONOPS_VECTOR_BACKEND 设为 memory。"
        )

    raise ValueError(f"未知的向量后端 {backend!r}。可选：qdrant（默认）/ memory（测试用）")


def get_vector_store(settings: Settings | None = None) -> VectorStore:
    """进程内单例。

    单例的理由：Qdrant 客户端内部维护连接池，每次检索新建一个会反复握手。
    """
    global _store
    if _store is None:
        _store = build_vector_store(settings)
    return _store


def reset_vector_store() -> None:
    """丢掉单例。测试里改完配置必须调，否则会一直用旧实例。"""
    global _store
    if _store is not None:
        _store.close()
    _store = None


__all__ = [
    "MemoryVectorStore",
    "QdrantVectorStore",
    "VectorStore",
    "build_vector_store",
    "get_vector_store",
    "reset_vector_store",
]
