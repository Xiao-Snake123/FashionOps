"""内存向量库：单测用，零依赖、毫秒级。

## 为什么单测不用真 Qdrant

项目现有的约定是"单测跑 SQLite 临时库、连不上 Redis 就 skip"。
向量库如果直接写死 Qdrant，每个检索相关的测试都要起一个进程 ——
测试从几十秒变成几分钟，然后大家开始不跑测试。**这是最实际的失败模式**。

代价是"内存实现永远是对的"，所以必须有单独的、连真 Qdrant 的测试
（`tests/test_vectors_qdrant.py`，连不上自动 skip）。两层都写，
才既跑得快又不会漏掉真组件的问题 —— 这个模式在 MySQL 上已经验证过一次了：
`test_api_mysql.py` 当年正是靠它发现了 `NULLS LAST` 的方言问题。

## 为什么不用"假装是 Qdrant"的方式实现

有些做法是在测试里对 Qdrant 客户端打桩。那样测的是"我们有没有按预期调用 SDK"，
而不是"检索结果对不对"。而且 SDK 版本升级后打桩会失效，
测试仍然绿着，但线上已经换了行为。**内存实现是"另一种正确的实现"，
不是"桩"**，这个区别很重要。
"""

from __future__ import annotations

import math
from typing import Any

from fashionops.vectors.base import (
    CollectionInfo,
    Filter,
    SparseVector,
    VectorHit,
    VectorRecord,
    rrf_fuse,
)


class MemoryVectorStore:
    """纯内存实现。暴力扫描 + 精确相似度。

    规模是几百到几千个向量，暴力扫描比 HNSW 还快
    （没有索引构建开销、没有图遍历的随机访问），所以**不做近似**。
    """

    backend = "memory"

    def __init__(self) -> None:
        #: collection 名 -> {point_id: VectorRecord}
        self._data: dict[str, dict[str, VectorRecord]] = {}
        self._dims: dict[str, int] = {}
        self._distances: dict[str, str] = {}

    def ensure_collection(
        self,
        name: str,
        *,
        dimension: int,
        distance: str = "cosine",
        sparse_vector_names: list[str] | None = None,
    ) -> bool:
        # 内存实现不做稀疏向量，sparse_vector_names 直接忽略（签名对齐 Qdrant 适配器）
        del sparse_vector_names
        if name in self._data:
            existing = self._dims[name]
            if existing != dimension:
                raise ValueError(
                    f"collection {name!r} 的维度是 {existing}，但当前配置是 {dimension} 维。"
                )
            return False
        self._data[name] = {}
        self._dims[name] = dimension
        self._distances[name] = distance
        return True

    def upsert(self, name: str, records: list[VectorRecord]) -> int:
        self._require(name)
        for record in records:
            if len(record.vector) != self._dims[name]:
                raise ValueError(
                    f"向量维度不一致：collection {name!r} 是 {self._dims[name]} 维，"
                    f"但传入的是 {len(record.vector)} 维"
                )
            self._data[name][record.id] = record
        return len(records)

    def search(
        self,
        name: str,
        vector: list[float],
        *,
        limit: int = 5,
        filter: Filter | None = None,  # noqa: A002
        using: str | None = None,
        with_payload: bool = True,
    ) -> list[VectorHit]:
        del using  # 内存实现只有一种向量，命名无意义
        self._require(name)
        distance = self._distances[name]

        scored: list[tuple[float, VectorRecord]] = []
        for record in self._data[name].values():
            if filter is not None and not _matches(record.payload, filter):
                continue
            scored.append((_similarity(vector, record.vector, distance), record))

        # 同分时按 id 排序，保证结果是**确定的** ——
        # 否则浮点误差会让测试偶发失败（同一批数据、两次运行顺序不同）
        scored.sort(key=lambda pair: (-pair[0], pair[1].id))

        return [
            VectorHit(
                id=record.id,
                score=score,
                payload=dict(record.payload) if with_payload else {},
            )
            for score, record in scored[:limit]
        ]

    def search_hybrid(
        self,
        name: str,
        dense_vector: list[float],
        sparse_vectors: dict[str, SparseVector],
        *,
        limit: int = 5,
        filter: Filter | None = None,  # noqa: A002
        with_payload: bool = True,
    ) -> list[VectorHit]:
        """混合检索：稠密排名 + 各路稀疏排名，用 RRF 在本地融合。

        内存实现没有 Qdrant 的 ``FusionQuery``，所以自己算 RRF。
        逻辑和 Qdrant 端保持一致（RRF，k=60），保证两层测试结论一致。
        """
        self._require(name)
        distance = self._distances[name]

        dense_ranked: list[tuple[float, VectorRecord]] = []
        sparse_ranked: list[tuple[float, VectorRecord]] = []
        for record in self._data[name].values():
            if filter is not None and not _matches(record.payload, filter):
                continue
            dense_ranked.append((_similarity(dense_vector, record.vector, distance), record))
            sparse_ranked.append((_sparse_score(sparse_vectors, record.sparse), record))

        dense_ids = [r.id for _, r in sorted(dense_ranked, key=lambda p: (-p[0], p[1].id))]

        rank_lists = [dense_ids]
        # 只有某路稀疏向量真的有信号时才纳入融合，避免全 0 排名为所有项
        # 平添一个常数偏移、干扰稠密排序
        if any(score > 0.0 for score, _ in sparse_ranked):
            sparse_ids = [r.id for _, r in sorted(sparse_ranked, key=lambda p: (-p[0], p[1].id))]
            rank_lists.append(sparse_ids)

        fused = rrf_fuse(rank_lists)
        by_id = {r.id: r for _, r in dense_ranked}
        ordered = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))

        return [
            VectorHit(
                id=rid,
                score=round(score, 4),
                payload=dict(by_id[rid].payload) if with_payload else {},
            )
            for rid, score in ordered[:limit]
        ]

    def delete(self, name: str, *, ids: list[str] | None = None) -> int:
        self._require(name)
        if ids is None:
            count = len(self._data[name])
            self._data[name] = {}
            return count
        removed = 0
        for point_id in ids:
            if self._data[name].pop(point_id, None) is not None:
                removed += 1
        return removed

    def drop_collection(self, name: str) -> bool:
        existed = name in self._data
        self._data.pop(name, None)
        self._dims.pop(name, None)
        self._distances.pop(name, None)
        return existed

    def count(self, name: str, *, exact: bool = True) -> int:
        # 内存实现本来就是精确的，exact 参数只为接口一致
        del exact
        return len(self._data.get(name, {}))

    def info(self, name: str, *, exact: bool = True) -> CollectionInfo:
        del exact
        if name not in self._data:
            return CollectionInfo(name=name, exists=False)
        model = None
        for record in self._data[name].values():
            model = record.payload.get("embedding_model")
            if model:
                break
        return CollectionInfo(
            name=name,
            exists=True,
            vector_count=len(self._data[name]),
            dimension=self._dims[name],
            distance=self._distances[name],
            embedding_model=str(model) if model else None,
        )

    def healthy(self) -> tuple[bool, str]:
        total = sum(len(v) for v in self._data.values())
        return True, f"内存模式，{len(self._data)} 个 collection / {total} 个向量"

    def close(self) -> None:
        self._data.clear()

    def _require(self, name: str) -> None:
        if name not in self._data:
            raise KeyError(
                f"collection {name!r} 不存在。先调 ensure_collection() —— "
                f"现有：{', '.join(sorted(self._data)) or '（无）'}"
            )


def _matches(payload: dict[str, Any], flt: Filter) -> bool:
    """过滤条件求值。语义要和 Qdrant 的实现对齐，否则测试和线上会不一致。"""
    for key, value in flt.must.items():
        if payload.get(key) != value:
            return False
    for key, values in flt.any_of.items():
        if payload.get(key) not in values:
            return False
    return all(payload.get(key) != value for key, value in flt.must_not.items())


def _sparse_score(query_sparse: dict[str, SparseVector], record_sparse: dict[str, SparseVector]) -> float:
    """查询稀疏向量与某条记录的稀疏向量之间的点积（按向量名对齐后求和）。

    两边用相同的词项→下标映射（见 ``fashionops.kb.sparse``），所以同名下标可直接对齐。
    """
    total = 0.0
    for name, qvec in query_sparse.items():
        rvec = record_sparse.get(name)
        if not qvec or not rvec:
            continue
        q_map = dict(zip(qvec.indices, qvec.values))
        for idx, val in zip(rvec.indices, rvec.values):
            total += q_map.get(idx, 0.0) * val
    return total


def _similarity(a: list[float], b: list[float], distance: str) -> float:
    """统一返回"越大越相似"，和 Qdrant 适配器的归一化保持一致。"""
    if len(a) != len(b):
        raise ValueError(f"向量维度不一致：{len(a)} vs {len(b)}")

    dot = sum(x * y for x, y in zip(a, b, strict=True))

    if distance == "dot":
        return dot

    if distance == "cosine":
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)

    if distance == "euclid":
        squared = sum((x - y) ** 2 for x, y in zip(a, b, strict=True))
        return 1.0 / (1.0 + math.sqrt(squared))

    if distance == "manhattan":
        total = sum(abs(x - y) for x, y in zip(a, b, strict=True))
        return 1.0 / (1.0 + total)

    raise ValueError(f"不支持的距离度量 {distance!r}")
