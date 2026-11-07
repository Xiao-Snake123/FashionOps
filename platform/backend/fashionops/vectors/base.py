"""向量库的抽象接口。

这个类是规划文档 §9.3 里承诺过的东西（"把向量后端抽象成 ``VectorStore`` 接口，
开发期用嵌入式零依赖，生产切换只改一个适配器类，检索与重排逻辑完全复用"）。
当时写在了文档里，**代码里一直不存在** —— 现在补上。

## 为什么值得抽这一层

不是为了"将来可能换库"这种假想需求 —— 而是有三个**当下就成立**的理由：

1. **单测不能依赖外部服务**。项目现有的约定是"单测跑 SQLite 临时库、
   Redis 连不上就 skip"。向量库如果直接写死 Qdrant，每个检索测试都要起一个
   Qdrant 进程，测试会从 50 秒变成几分钟，然后大家开始不跑测试。
   有了这层抽象，单测用内存实现，**毫秒级、零依赖**。

2. **"测试通过 ≠ 线上能用"要能验证**。内存实现永远是对的，
   所以必须有单独的、连真 Qdrant 的测试（同 `test_gateway_redis.py` /
   `test_api_mysql.py` 的模式，连不上自动 skip）。
   两层都写，才既跑得快又不会漏掉真组件的问题。

3. **检索逻辑要被复用**。切分、BM25、RRF 融合、重排这些是**与存储无关**的，
   它们应该只依赖这层接口。如果检索代码里到处 `client.search(...)`，
   换后端就等于重写检索。

## 接口刻意保持小

只有 7 个方法，而且**不做 ORM 式封装**：不提供"文档-段落-字段"的模型层，
就是 id + vector + payload 三元组。

理由是向量库的强项恰恰在于 **payload 过滤**（Qdrant 的 filter 能下推到 HNSW 遍历里，
不是"检索完再筛"）。如果为了"面向对象好看"把 payload 包进领域模型，
过滤条件就没法下推了，等于主动放弃向量库最重要的能力之一。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

#: payload 里用来存原文的键。约定成常量，因为切分/检索/重排三处都要用到，
#: 各写各的字符串字面量迟早拼错。
PAYLOAD_TEXT = "text"
#: 存原始记录的业务主键（如 kb_articles.article_no）—— 用来回溯到源数据
PAYLOAD_SOURCE_ID = "source_id"
#: 存生成这批向量的模型名。换 embedding 模型后能一眼看出哪些向量是旧的
PAYLOAD_MODEL = "embedding_model"
#: 存调用方给的**逻辑 id**。
#:
#: 为什么需要它，而不是直接用存储层的 point id：
#: Qdrant 的 point id 只接受无符号整数或 UUID，所以适配器会把
#: ``kb-0001#3`` 这种可读 id 转成确定的 UUID。如果直接把存储层的 id 当结果返回，
#: 调用方拿到的就是一个它从没见过的 UUID —— 既无法反推来源，
#: 两个后端（内存 / Qdrant）给出的 id 也会不一样。
#:
#: 契约测试正是靠这个发现问题的：同一组断言跑两个后端，
#: 内存返回 ``"near"``、Qdrant 返回一串 UUID。只看一个后端永远发现不了。
PAYLOAD_ID = "_id"

#: 知识库混合检索用的向量名。Qdrant 里 KB collection 用**命名向量**：
#: 稠密语义向量叫 ``dense``，词法/稀疏向量叫 ``bm25``（默认，可由配置覆盖）。
#: 用命名向量是因为 Qdrant 的 ``FusionQuery``（RRF 融合）只能按名字引用各路向量。
KB_DENSE_VECTOR_NAME = "dense"
KB_SPARSE_VECTOR_NAME = "bm25"


@dataclass(frozen=True)
class SparseVector:
    """一条稀疏向量（词法检索用）。

    只存非零项：``indices`` 是词项哈希后的整数下标，``values`` 是对应的权重
    （BM25 风格的 TF-IDF）。空稀疏向量（没有任何词项）用 ``empty()`` 表示，
    在融合时直接跳过，不会贡献分数。

    下标的取法见 ``fashionops.kb.sparse``：用 blake2b 哈希词项再取模，
    得到一个稳定且跨进程一致的整数空间，这样建索引和检索两侧算出来的
    稀疏向量能对得上。
    """

    indices: tuple[int, ...] = ()
    values: tuple[float, ...] = ()

    @classmethod
    def empty(cls) -> SparseVector:
        return cls((), ())

    def __bool__(self) -> bool:
        return bool(self.indices)


@dataclass(frozen=True)
class VectorRecord:
    """一条待写入的记录。

    ``id`` 由调用方给定而不是自动生成：检索结果的 id 要能反推回源数据，
    自动生成的话就得再查一次 payload 才知道命中的是哪篇文章。

    ``sparse`` 是可选的稀疏向量集合，键是向量名（如 ``"bm25"``）。
    建混合检索索引时才填；普通稠密检索（如历史工单）留空即可。
    """

    id: str
    vector: list[float]
    payload: dict[str, Any] = field(default_factory=dict)
    sparse: dict[str, SparseVector] = field(default_factory=dict)


@dataclass(frozen=True)
class VectorHit:
    """一条检索结果。

    ``id`` 是**调用方写入时的那个逻辑 id**，不是存储层的 point id ——
    存储层怎么编码 id 是实现细节，不该泄到接口上（详见 ``PAYLOAD_ID``）。
    ``score`` 的含义统一为"越大越相似"。
    """

    id: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """命中的原文。取不到时返回空串而不是抛异常 —— 检索链路不该因为缺字段崩掉。"""
        return str(self.payload.get(PAYLOAD_TEXT) or "")

    @property
    def source_id(self) -> str:
        return str(self.payload.get(PAYLOAD_SOURCE_ID) or "")


@dataclass
class CollectionInfo:
    """collection 的现状。给健康检查和 CLI 用。"""

    name: str
    exists: bool
    vector_count: int = 0
    dimension: int | None = None
    distance: str = "cosine"
    #: collection 里向量所用的 embedding 模型名（从已写入的记录里抽样得到）
    embedding_model: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Filter:
    """payload 过滤条件。

    刻意做成"每项都是 AND + 等值/任一匹配"这种最小集合，
    而不是暴露某个库的完整过滤 DSL —— 那样抽象就漏了，
    换后端时调用方要改。需要更复杂的过滤时再往这里加方法。

    * ``must``   —— 全部相等
    * ``any_of`` —— 键相同、值在给定集合里（用于"类目是 A 或 B"）
    * ``must_not`` —— 全部不等
    """

    must: dict[str, Any] = field(default_factory=dict)
    any_of: dict[str, list[Any]] = field(default_factory=dict)
    must_not: dict[str, Any] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.must or self.any_of or self.must_not)


def rrf_fuse(
    rank_lists: list[list[str]],
    *,
    k: int = 60,
    weights: list[float] | None = None,
) -> dict[str, float]:
    """Reciprocal Rank Fusion，把多路排名融合成一个分数表。

    每个列表里排第 ``rank``（从 1 开始）的 id 贡献 ``weight / (k + rank)``，
    各列表累加。``k`` 默认 60（标准值），越大越偏向"各列表头部一致"的项。

    用 id 字符串而不是对象：融合与具体存储后端解耦，内存 / Qdrant 都能复用。
    Qdrant 后端其实在库内做融合（``FusionQuery``），这个函数主要给
    内存后端用，也方便单测直接验证融合逻辑。
    """
    if not rank_lists:
        return {}
    if weights is None:
        weights = [1.0] * len(rank_lists)
    scores: dict[str, float] = {}
    for weight, rank_list in zip(weights, rank_lists, strict=True):
        for rank, item in enumerate(rank_list, start=1):
            scores[item] = scores.get(item, 0.0) + weight / (k + rank)
    return scores


@runtime_checkable
class VectorStore(Protocol):
    """向量库适配器协议。"""

    #: 后端名字（qdrant / memory），写进日志和健康检查
    backend: str

    def ensure_collection(
        self, name: str, *, dimension: int, distance: str = "cosine", sparse_vector_names: list[str] | None = None
    ) -> bool:
        """确保 collection 存在且维度正确。已存在且维度一致时返回 False（没动它）。

        **维度不一致必须报错，不能静默重建** —— 重建会丢掉全部已有向量，
        而调用方（比如一次普通的服务启动）根本没想到会触发数据丢失。

        ``sparse_vector_names`` 非空时，collection 会建成**命名向量**
        （稠密默认叫 ``dense``，外加列出的稀疏向量名），用于混合检索。
        内存后端直接忽略这个参数。
        """
        ...

    def upsert(self, name: str, records: list[VectorRecord]) -> int:
        """写入或覆盖。返回写入条数。

        用 upsert 而不是 insert：建索引天然要支持重跑，
        insert 会在第二次运行时因主键冲突失败。
        """
        ...

    def search(
        self,
        name: str,
        vector: list[float],
        *,
        limit: int = 5,
        filter: Filter | None = None,  # noqa: A002 —— 这里就是"过滤条件"的意思
        using: str | None = None,
        with_payload: bool = True,
    ) -> list[VectorHit]:
        """相似度检索。结果按相关度**从高到低**排（不管底层距离方向）。

        ``using`` 指定命名向量（KB 的稠密向量叫 ``dense``）；
        留空用默认（未命名的）向量，历史工单集合走这条。
        """
        ...

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
        """混合检索：多路向量（稠密 + 若干稀疏）按 RRF 融合后返回。

        具体怎么融由后端决定：内存实现在本地做 RRF，
        Qdrant 用 ``FusionQuery`` 在库内做。调用方不关心，拿到融合结果即可。

        ``sparse_vectors`` 的键必须和建索引时 ``VectorRecord.sparse`` 的键一致
        （默认 ``"bm25"``），否则对应那路检索会失配。
        """
        ...

    def delete(self, name: str, *, ids: list[str] | None = None) -> int:
        """删除点。``ids=None`` 表示**清空数据但保留 collection**。

        “清空”和“拆掉结构”是两件事：前者不动维度/索引这些配置，
        后者要显式调 ``drop_collection``（换 embedding 模型时才需要）。
        """
        ...

    def drop_collection(self, name: str) -> bool:
        """删掉整个 collection（连同维度、索引等配置）。返回它原先是否存在。"""
        ...

    def count(self, name: str, *, exact: bool = True) -> int:
        """向量数。

        ``exact=False`` 时允许返回近似值 —— **健康检查必须走这条**。
        精确计数要扫全量，数据量上万后会让 /health 变成慢请求，
        而健康检查被负载均衡器高频调用，它一慢整个服务的可用性判定都会受影响。
        """
        ...

    def info(self, name: str, *, exact: bool = True) -> CollectionInfo: ...

    def healthy(self) -> tuple[bool, str]:
        """连通性探活。返回 ``(是否可用, 说明)``。

        返回说明字符串而不是只给 bool：`/health` 上"Qdrant 不可用"这句话
        对排查毫无帮助，需要带上"连的是哪个地址、报了什么错"。
        """
        ...

    def close(self) -> None: ...
