"""Qdrant 适配器。

## 为什么选 Qdrant（而不是 Milvus / pgvector / Chroma）

完整论证见 `docs/10-向量库选型与RAG实现.md`，这里只放结论性的几条：

**为什么不是 Milvus** —— 它是"分布式优先"的架构：
标准部署要 etcd + MinIO + Pulsar（standalone 模式下也至少要 etcd + MinIO），
最少三个容器起步。而它的优势场景是**亿级向量**（段合并、分布式查询、存算分离）。
我们的知识库是 17 篇文章切出几百个 chunk，用 Milvus 是拿高射炮打蚊子 ——
而且它带来的复杂度会成为真实负担：etcd 挂了、MinIO 桶权限错了，
都会让 RAG 整个挂掉，**而这些和"检索质量好不好"毫无关系**。

Qdrant 是 Rust 单二进制、单容器、零外部依赖，`qdrant.exe` 解压即用。

**为什么不是 pgvector** —— 我们已经有 MySQL 了，再加 PostgreSQL 只为了向量检索，
等于为了一个功能多养一套数据库。而且 pgvector 的过滤就是普通 SQL `WHERE`，
**没有 Qdrant 那种"过滤下推到 HNSW 遍历"的能力**：数据量大时
"先按 HNSW 取 top-k 再过滤"会把结果筛没，这个坑很经典。

**为什么不是 Chroma** —— 嵌入式、零依赖，但定位是"原型友好"：
过滤能力弱（不支持我们没有的复杂条件组合）、没有量化、没有 named vectors，
且历史上接口变动较大。适合 demo，不适合当生产存储。

**取舍的诚实说明**：这不是"Qdrant 全面更好"，而是
**"在这个规模 + 这个部署条件（Windows 单机、portable 安装、无 Docker）下性价比最高"**。
在 Qdrant 的筛选逻辑见 `..base.Filter`，换后端只需新增一个适配器类。

## 两个必须注意的实现细节

### 1. 距离度量决定 score 的方向
``cosine`` / ``dot``：**越大越相似**（Qdrant 返回的是相似度）。
``euclid`` / ``manhattan``：**越小越相似**（返回的是距离）。
不做转换的话，用欧氏距离检索出来的"最相似"会是最不相似的那条。
本适配器统一转成"越大越相似"，调用方不用关心用的是什么距离。

### 2. 过滤是在 HNSW 遍历里做的
Qdrant 的 filter 是**倒排索引 + HNSW 遍历时剪枝**，不是"取完 top-k 再筛"。
所以带过滤的检索不需要把 ``limit`` 放大来"补偿被筛掉的"，
传真实想要的数量即可。（pgvector 就需要，这是它的经典坑。）
"""

from __future__ import annotations

import logging
from typing import Any

from fashionops.vectors.base import (
    KB_DENSE_VECTOR_NAME,
    PAYLOAD_ID,
    CollectionInfo,
    Filter,
    SparseVector,
    VectorHit,
    VectorRecord,
)

logger = logging.getLogger("fashionops.vectors.qdrant")

#: Qdrant 里 payload 字段要建索引才能高效过滤。
#: 不建索引也能过滤（全表扫），但数据量上来后会慢得莫名其妙。
_INDEXED_PAYLOAD_FIELDS = ("source_id", "category", "is_active", "kind")


class QdrantVectorStore:
    """Qdrant 适配器。"""

    backend = "qdrant"

    def __init__(
        self,
        *,
        url: str,
        api_key: str | None = None,
        timeout: float = 10.0,
        prefer_grpc: bool = False,
    ) -> None:
        # 延迟 import：没装 qdrant-client 的极简环境（比如只跑 CLI 工具）
        # 不该因为 import 这个模块就崩
        from qdrant_client import QdrantClient

        self.url = url
        self._client = QdrantClient(
            url=url,
            api_key=api_key or None,
            timeout=timeout,
            prefer_grpc=prefer_grpc,
        )
        #: 记住每个 collection 的维度，避免每次 upsert 都去问一次服务端
        self._dims: dict[str, int] = {}
        self._distances: dict[str, str] = {}

    # ------------------------------------------------------------------
    # collection 管理
    # ------------------------------------------------------------------
    def ensure_collection(
        self,
        name: str,
        *,
        dimension: int,
        distance: str = "cosine",
        sparse_vector_names: list[str] | None = None,
    ) -> bool:
        from qdrant_client import models

        if self._client.collection_exists(name):
            info = self._client.get_collection(name)
            existing_dim = _extract_dimension(info)
            if existing_dim is not None and existing_dim != dimension:
                raise ValueError(
                    f"collection {name!r} 的维度是 {existing_dim}，"
                    f"但当前配置是 {dimension} 维。\n"
                    f"维度在**建索引时就固定了**，不能就地改。选一个：\n"
                    f"  1. 把 FASHIONOPS_EMBEDDING_DIM / 模型换回 {existing_dim} 维的\n"
                    f"  2. 删掉重建（会丢掉全部已有向量，需重新建索引）：\n"
                    f"     fashionops-kb drop --yes && fashionops-kb index"
                )
            # 混合检索要用命名向量 + 稀疏向量。旧版 KB 集合是"无命名向量"建的，
            # 无法原地升级（Qdrant 不允许 collection 在命名/未命名向量间切换），
            # 必须删了重建 —— 给出明确可执行的报错，而不是静默建出错的索引。
            if sparse_vector_names:
                self._require_named_vectors(info, name)
                self._ensure_sparse_config(name, sparse_vector_names)
            self._dims[name] = existing_dim or dimension
            self._distances[name] = _extract_distance(info)
            return False

        if sparse_vector_names:
            # 命名向量：稠密叫 "dense"，外加列出的稀疏向量（默认 "bm25"）。
            vectors_config = {
                KB_DENSE_VECTOR_NAME: models.VectorParams(
                    size=dimension,
                    distance=_to_qdrant_distance(distance),
                    on_disk=False,
                )
            }
            sparse_cfg = {sname: models.SparseVectorParams() for sname in sparse_vector_names}
            self._client.create_collection(
                collection_name=name,
                vectors_config=vectors_config,
                sparse_vectors_config=sparse_cfg,
            )
        else:
            self._client.create_collection(
                collection_name=name,
                vectors_config=models.VectorParams(
                    size=dimension,
                    distance=_to_qdrant_distance(distance),
                    # 显式建 payload 索引：过滤条件在这些字段上时
                    # 会走索引而不是全表扫
                    on_disk=False,
                ),
            )
        self._ensure_payload_indexes(name)
        self._dims[name] = dimension
        self._distances[name] = distance
        logger.info("已创建 Qdrant collection %s（%d 维，距离=%s）", name, dimension, distance)
        return True

    def _require_named_vectors(self, info, name: str) -> None:
        """旧版（无命名向量）的 collection 不能升级成混合检索，必须重建。"""
        vectors = getattr(getattr(info, "config", None), "params", None)
        vecs = getattr(vectors, "vectors", None)
        if not isinstance(vecs, dict):
            raise RuntimeError(
                f"collection {name!r} 是用旧版（无命名向量）建的，无法原地升级为混合检索。\n"
                f"请先删除再重建索引：fashionops-kb drop --yes && fashionops-kb index"
            )

    def _ensure_sparse_config(self, name: str, names: list[str]) -> None:
        """给已存在的 collection 补上稀疏向量配置（幂等，已存在则忽略）。"""
        from qdrant_client import models

        for sname in names:
            try:
                self._client.create_sparse_vector_config(
                    name, {sname: models.SparseVectorParams()}
                )
            except Exception as exc:  # noqa: BLE001
                # 已存在会报冲突，忽略；其它异常降级为告警，不让建索引整体失败
                logger.warning("给 %s 补稀疏向量 %s 失败（若已存在可忽略）：%s", name, sname, exc)

    def _ensure_payload_indexes(self, name: str) -> None:
        """给常用过滤字段建 keyword 索引。

        失败只告警不抛：**建索引失败不该让整个流程起不来** ——
        过滤仍然能用（退化成扫描），只是慢。为了一个性能优化把服务弄挂不值得。
        """
        from qdrant_client import models

        for field_name in _INDEXED_PAYLOAD_FIELDS:
            try:
                self._client.create_payload_index(
                    collection_name=name,
                    field_name=field_name,
                    field_schema=models.PayloadSchemaType.KEYWORD,
                    wait=True,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "给 %s.%s 建 payload 索引失败（不影响功能，只是过滤会慢）：%s",
                    name,
                    field_name,
                    exc,
                )

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def upsert(self, name: str, records: list[VectorRecord]) -> int:
        if not records:
            return 0

        from qdrant_client import models

        expected = self._dims.get(name)
        if expected is not None:
            actual = len(records[0].vector)
            if actual != expected:
                raise ValueError(
                    f"向量维度不一致：collection {name!r} 是 {expected} 维，"
                    f"但传入的向量是 {actual} 维。检查 embedding 模型和 "
                    f"FASHIONOPS_EMBEDDING_DIM 是否匹配。"
                )

        points = []
        for r in records:
            # 有稀疏向量时，稠密向量走命名 "dense"，稀疏向量各自按名挂上；
            # 没有（如历史工单）则保持旧的未命名稠密向量写法，零影响。
            if r.sparse:
                vector: Any = {KB_DENSE_VECTOR_NAME: r.vector}
                for sname, svec in r.sparse.items():
                    vector[sname] = models.SparseVector(
                        indices=list(svec.indices), values=list(svec.values)
                    )
            else:
                vector = r.vector
            points.append(
                models.PointStruct(
                    id=_to_point_id(r.id),
                    vector=vector,
                    # 把调用方给的逻辑 id 也写进 payload —— 检索时要原样还回去。
                    # 不存的话，返回的就只能是转换后的 UUID（见 PAYLOAD_ID 的说明）
                    payload={**r.payload, PAYLOAD_ID: r.id},
                )
            )

        # 分批提交：一次几千个点会让单次请求体积过大而超时。
        # 批量大小取 256 是个经验值 —— 再大收益不明显，失败时重试代价更高。
        batch = 256
        for start in range(0, len(points), batch):
            self._client.upsert(
                collection_name=name,
                points=points[start : start + batch],
                wait=True,
            )
        return len(points)

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
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

        query_filter = _to_qdrant_filter(filter)

        # 用 query_points 而不是老的 search —— search 在新版已被标记为废弃
        response = self._client.query_points(
            collection_name=name,
            query=vector,
            query_filter=query_filter,
            using=using,
            limit=limit,
            with_payload=with_payload,
        )

        distance = self._distances.get(name, "cosine")
        hits: list[VectorHit] = []
        for point in response.points:
            payload = dict(point.payload or {})
            # 取出写入时存的逻辑 id，并**从 payload 里摘掉** ——
            # 它是我们的内部约定，露给调用方只会造成困惑（为什么多了个下划线字段）
            logical_id = payload.pop(PAYLOAD_ID, None)
            hits.append(
                VectorHit(
                    id=str(logical_id) if logical_id is not None else str(point.id),
                    score=_normalize_score(point.score, distance),
                    payload=payload,
                )
            )
        return hits

    # ------------------------------------------------------------------
    # 混合检索：稠密 + 稀疏，库内 RRF 融合
    # ------------------------------------------------------------------
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
        """用 Qdrant 的 ``FusionQuery(RRF)`` 在库内把多路向量融合。

        每路检索写成一个 ``Prefetch``：稠密走命名 ``dense``，各稀疏向量走自己的名字
        （默认 ``bm25``）。融合交给 Qdrant，返回的分数已经是 RRF 融合分（越大越相似）。
        """
        from qdrant_client import models

        prefetches = [
            models.Prefetch(query=dense_vector, using=KB_DENSE_VECTOR_NAME, limit=limit)
        ]
        for sname, svec in sparse_vectors.items():
            prefetches.append(
                models.Prefetch(
                    query=models.SparseVector(
                        indices=list(svec.indices), values=list(svec.values)
                    ),
                    using=sname,
                    limit=limit,
                )
            )

        response = self._client.query_points(
            collection_name=name,
            query=models.FusionQuery(fusion=models.Fusion.RRF, query=prefetches),
            query_filter=_to_qdrant_filter(filter),
            limit=limit,
            with_payload=with_payload,
        )

        distance = self._distances.get(name, "cosine")
        hits: list[VectorHit] = []
        for point in response.points:
            payload = dict(point.payload or {})
            # 取出写入时存的逻辑 id，并**从 payload 里摘掉**
            logical_id = payload.pop(PAYLOAD_ID, None)
            hits.append(
                VectorHit(
                    id=str(logical_id) if logical_id is not None else str(point.id),
                    score=_normalize_score(point.score, distance),
                    payload=payload,
                )
            )
        return hits

    # ------------------------------------------------------------------
    # 删除 / 统计
    # ------------------------------------------------------------------
    def delete(self, name: str, *, ids: list[str] | None = None) -> int:
        from qdrant_client import models

        if ids is None:
            # 清空 = **只删点，保留 collection**。
            #
            # 一开始的实现是 "删掉 collection 再重建"，更“干净”但很慢：
            # 重建要重新为几个字段建 payload 索引（每个都是带 wait=True 的往返），
            # 于是测试里每个用例都多花十几秒。
            #
            # 保留 collection 反而是更对的语义：“清空数据”本来就不该
            # 顺带把 schema（维度/距离/索引）也扔掉 —— 那些是配置，不是数据。
            before = self.count(name)
            if before:
                self._client.delete(
                    collection_name=name,
                    points_selector=models.FilterSelector(filter=models.Filter()),
                    wait=True,
                )
            return before

        self._client.delete(
            collection_name=name,
            points_selector=models.PointIdsList(points=[_to_point_id(i) for i in ids]),
            wait=True,
        )
        return len(ids)

    def drop_collection(self, name: str) -> bool:
        """真正删掉 collection（连同维度/索引这些配置）。

        和 ``delete(ids=None)`` 的区别：那个是“清空数据”，这个是“拆掉结构”。
        换 embedding 模型（维度变了）时才需要后者。
        """
        if not self._client.collection_exists(name):
            return False
        self._client.delete_collection(name)
        self._dims.pop(name, None)
        self._distances.pop(name, None)
        return True

    def count(self, name: str, *, exact: bool = True) -> int:
        if not self._client.collection_exists(name):
            return 0
        return self._client.count(name, exact=exact).count

    def info(self, name: str, *, exact: bool = True) -> CollectionInfo:
        if not self._client.collection_exists(name):
            return CollectionInfo(name=name, exists=False)

        raw = self._client.get_collection(name)
        return CollectionInfo(
            name=name,
            exists=True,
            vector_count=self._client.count(name, exact=exact).count,
            dimension=_extract_dimension(raw),
            distance=_extract_distance(raw),
            embedding_model=self._sample_embedding_model(name),
        )

    def _sample_embedding_model(self, name: str) -> str | None:
        """从已有的点里抽一条看它用的是什么 embedding 模型。

        为什么要这个：换 embedding 模型后，旧向量还在库里，
        **新旧向量混在一起检索会给出莫名其妙的低分**。把模型名放进 payload
        并在这里显示出来，是让"库里是旧向量"这件事变得可见的最低成本做法。
        """
        try:
            points, _ = self._client.scroll(
                collection_name=name, limit=1, with_payload=True, with_vectors=False
            )
        except Exception:  # noqa: BLE001
            return None
        if not points:
            return None
        payload = points[0].payload or {}
        model = payload.get("embedding_model")
        return str(model) if model else None

    # ------------------------------------------------------------------
    def healthy(self) -> tuple[bool, str]:
        try:
            collections = self._client.get_collections().collections
        except Exception as exc:  # noqa: BLE001
            # 把地址一起带上。只报 "Qdrant 不可用" 对排查毫无帮助 ——
            # 我们真踩过一次：系统代理把请求拦成 502，而错误信息里没有地址，
            # 第一反应是"Qdrant 挂了"，实际是代理问题
            return False, f"{type(exc).__name__}: {exc}（地址 {self.url}）"
        names = [c.name for c in collections]
        return True, f"{self.url}，{len(names)} 个 collection：{', '.join(names) or '（空）'}"

    def close(self) -> None:
        self._client.close()


# ---------------------------------------------------------------------------
# 转换
# ---------------------------------------------------------------------------
def _to_qdrant_distance(distance: str) -> Any:  # noqa: ANN401
    from qdrant_client import models

    mapping = {
        "cosine": models.Distance.COSINE,
        "dot": models.Distance.DOT,
        "euclid": models.Distance.EUCLID,
        "manhattan": models.Distance.MANHATTAN,
    }
    if distance not in mapping:
        raise ValueError(f"不支持的距离度量 {distance!r}，可选：{', '.join(mapping)}")
    return mapping[distance]


def _normalize_score(score: float, distance: str) -> float:
    """统一成"**越大越相似**"。

    Qdrant 对 cosine/dot 返回相似度（越大越好），对 euclid/manhattan 返回距离
    （越小越好）。不转换的话，用欧氏距离时调用方按"降序取第一条"拿到的
    会是最不相似的结果 —— 而且不会报错，只是结果莫名其妙。
    """
    if distance in ("euclid", "manhattan"):
        # 转成单调递减的相似度。用 1/(1+d) 而不是 -d：
        # 保证落在 (0, 1]，和余弦相似度量纲接近，方便混合检索时做分数融合
        return 1.0 / (1.0 + max(score, 0.0))
    return score


def _to_point_id(raw: str) -> Any:  # noqa: ANN401
    """Qdrant 的 point id 只接受无符号整数或 UUID。

    我们的 id 是 ``kb-0001#3`` 这种可读字符串，不能直接用。
    用 uuid5 从字符串确定性派生 —— **确定性**很关键：
    同一个 chunk 每次都要落到同一个 point id，否则重跑建索引会写出重复点。
    """
    import uuid

    return str(uuid.uuid5(uuid.NAMESPACE_URL, raw))


def _to_qdrant_filter(flt: Filter | None) -> Any:  # noqa: ANN401
    from qdrant_client import models

    if flt is None or flt.empty:
        return None

    conditions: list[Any] = []
    for key, value in flt.must.items():
        conditions.append(models.FieldCondition(key=key, match=models.MatchValue(value=value)))
    for key, values in flt.any_of.items():
        conditions.append(models.FieldCondition(key=key, match=models.MatchAny(any=list(values))))
    for key, value in flt.must_not.items():
        conditions.append(
            models.FieldCondition(key=key, match=models.MatchValue(value=value))
        )

    must = conditions[: len(flt.must) + len(flt.any_of)]
    must_not = conditions[len(must) :]

    return models.Filter(
        must=must or None,
        must_not=must_not or None,
    )


def _extract_dimension(info: Any) -> int | None:  # noqa: ANN401
    """从 Qdrant 的 collection 信息里取向量维度。

    不同版本的返回结构不一样：单向量时是 ``VectorParams``，多向量（named vectors）
    时是 ``dict[str, VectorParams]``。这里都处理，取第一个向量的维度。
    """
    vectors = getattr(getattr(info, "config", None), "params", None)
    vectors = getattr(vectors, "vectors", None)
    if vectors is None:
        return None
    if hasattr(vectors, "size"):
        return int(vectors.size)
    if isinstance(vectors, dict):
        for params in vectors.values():
            if hasattr(params, "size"):
                return int(params.size)
    return None


def _extract_distance(info: Any) -> str:  # noqa: ANN401
    vectors = getattr(getattr(info, "config", None), "params", None)
    vectors = getattr(vectors, "vectors", None)
    target = vectors
    if isinstance(vectors, dict):
        target = next(iter(vectors.values()), None)
    distance = getattr(target, "distance", None)
    if distance is None:
        return "cosine"
    # models.Distance.COSINE -> "Cosine"
    return str(getattr(distance, "value", distance)).lower()
