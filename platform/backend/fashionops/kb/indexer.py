"""知识库索引：把 ``kb_articles`` 切分、向量化、写进向量库。

## 设计要点

### 1. 索引可以随时重跑，而且必须是幂等的
chunk id 用 ``{article_no}#{chunk_index}`` 这种**确定性**的字符串，
再映射成确定的 point id（Qdrant 适配器里用 uuid5 做）。
所以重跑会覆盖自己，不会写出重复点。

不这么做的常见后果：改一次切分参数重跑，库里就多一份，
检索时同一段内容被召回三次，top-k 里全是重复。

### 2. 换 embedding 模型后必须重建，代码要能发现这件事
向量库本身不知道"这批向量是哪个模型生成的"。换了模型之后
新旧向量混在一起检索，会给出**看似正常但实际无意义**的相似度
（因为两个模型的向量空间完全不同）。

所以每条记录的 payload 里都存 ``embedding_model``，
索引时比对并**在模型变了的情况下默认要求重建**（除非显式 --keep）。

### 3. 只索引 ``is_active=True`` 的文章
下架的文章不该被召回。同时把 ``is_active`` 写进 payload，
检索时还能再加一道过滤 —— 索引和查询两侧都防，
因为"删索引"和"改数据"之间有窗口期。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select

from fashionops.config import Settings, get_settings
from fashionops.db import session_scope
from fashionops.db.models import KBArticle
from fashionops.kb.chunking import build_chunks, normalize
from fashionops.kb.sparse import build_idf, encode, load_idf, save_idf
from fashionops.llm.embeddings import EmbeddingClient, get_embedding_client
from fashionops.vectors import (
    PAYLOAD_MODEL,
    PAYLOAD_SOURCE_ID,
    PAYLOAD_TEXT,
    Filter,
    VectorRecord,
    VectorStore,
)

logger = logging.getLogger("fashionops.kb.indexer")


@dataclass
class IndexStats:
    """一次建索引的结果。CLI 直接打印这个。"""

    collection: str
    articles: int = 0
    chunks: int = 0
    skipped_articles: int = 0
    embedding_model: str = ""
    dimension: int = 0
    #: 建索引前 collection 里已有的向量数（用来判断是新建还是追加）
    before_count: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"collection      : {self.collection}",
            f"文章数          : {self.articles}",
            f"切分块数        : {self.chunks}",
            f"embedding 模型  : {self.embedding_model}（{self.dimension} 维）",
            f"索引前已有向量  : {self.before_count}",
        ]
        if self.skipped_articles:
            lines.append(f"跳过文章        : {self.skipped_articles}（内容为空或已下架）")
        for warning in self.warnings:
            lines.append(f"⚠ {warning}")
        return "\n".join(lines)


def chunk_id(article_no: str, index: int) -> str:
    """确定性的 chunk id。

    格式里带上 article_no 而不是只用自增 id —— 这样在向量库里
    看一条记录的 id 就知道它来自哪篇文章，排查时省一次查询。
    """
    return f"{article_no}#{index}"


def article_chunks(article: KBArticle) -> list[str]:
    """决定"把什么文本喂给 embedding"。

    ``retrieval_text`` 优先：模型（造数据时）已经把它写成"标题 + 关键词 +
    正文摘要"，比正文裸文本检索效果好 —— 正文里有大量寒暄废话，
    会把向量往"客服用语"的方向带偏。
    """
    primary = (article.retrieval_text or "").strip()
    if primary:
        return [c.text for c in build_chunks(primary)]

    # 没有 retrieval_text 时退回"标题 + 正文"。
    # 标题一定要带上：只索引正文的话，搜"退货政策"可能召不回标题叫
    # 「退换货政策说明」但正文没出现这几个字的文章。
    combined = normalize(f"{article.title}\n\n{article.content or ''}")
    return [c.text for c in build_chunks(combined)]


def build_index(
    *,
    store: VectorStore | None = None,
    embedder: EmbeddingClient | None = None,
    settings: Settings | None = None,
    collection: str | None = None,
    rebuild: bool = False,
    batch_size: int = 32,
) -> IndexStats:
    """把知识库灌进向量库。

    ``rebuild=True`` 时先清空 collection —— 改了切分参数就该重建，
    否则新旧切分会同时存在，检索结果里出现重叠内容。
    """
    from fashionops.vectors import build_vector_store

    settings = settings or get_settings()
    collection = collection or settings.qdrant_collection_kb
    store = store or build_vector_store(settings)
    embedder = embedder or get_embedding_client(settings)

    # 混合检索开关。开时，KB collection 建成"稠密(dense) + 稀疏(bm25)"命名向量，
    # 并额外写一份 IDF 到 data_dir 供检索侧复用。
    use_sparse = bool(settings.retrieval_hybrid)
    sparse_name = settings.kb_sparse_vector_name

    store.ensure_collection(
        collection,
        dimension=embedder.dimension,
        sparse_vector_names=[sparse_name] if use_sparse else None,
    )

    stats = IndexStats(
        collection=collection,
        embedding_model=embedder.model_name,
        dimension=embedder.dimension,
        before_count=store.count(collection),
    )

    # ---- 模型一致性检查 ----
    existing_model = store.info(collection).embedding_model
    if existing_model and existing_model != embedder.model_name:
        message = (
            f"collection 里已有的向量是用 {existing_model!r} 生成的，"
            f"当前配置是 {embedder.model_name!r}。"
            f"两个模型的向量空间不兼容，混在一起检索的相似度没有意义。"
        )
        if not rebuild:
            raise RuntimeError(f"{message}\n建议重建：fashionops-kb index --rebuild")
        stats.warnings.append(f"{message}已按 --rebuild 清空重建。")

    if rebuild and stats.before_count:
        store.delete(collection)
        logger.info("已清空 collection %s（原有 %d 个向量）", collection, stats.before_count)

    # ---- 读文章 ----
    with session_scope() as session:
        articles = list(
            session.scalars(select(KBArticle).where(KBArticle.is_active.is_(True)).order_by(KBArticle.article_no))
        )

    if not articles:
        stats.warnings.append("kb_articles 里没有启用的文章，索引是空的")
        return stats

    # ---- 逐篇切分、批量向量化 ----
    pending_texts: list[str] = []
    pending_meta: list[tuple[str, int, KBArticle]] = []

    for article in articles:
        pieces = article_chunks(article)
        if not pieces:
            stats.skipped_articles += 1
            continue
        stats.articles += 1
        for index, text in enumerate(pieces):
            pending_texts.append(text)
            pending_meta.append((article.article_no, index, article))

    if not pending_texts:
        stats.warnings.append("所有文章切分后都是空的，检查 content 字段")
        return stats

    vectors = embedder.embed(pending_texts, batch_size=batch_size)

    # 稀疏（词法）向量：用全量 chunk 文本算 IDF 并落盘，检索侧复用同一套权重，
    # 这样 Qdrant 里稀疏向量的点积才近似"词项重合度"。关掉混合检索就不算、不写。
    idf: dict[str, float] = {}
    if use_sparse:
        idf = build_idf(pending_texts)
        save_idf(settings.data_dir, idf)

    records = []
    for text, (article_no, index, article), vector in zip(pending_texts, pending_meta, vectors, strict=True):
        payload = {
            PAYLOAD_TEXT: text,
            PAYLOAD_SOURCE_ID: article_no,
            "title": article.title,
            "category": article.category,
            "chunk_index": index,
            "is_active": True,
            PAYLOAD_MODEL: embedder.model_name,
            "version": article.version,
            **({"tags": list(article.tags)} if article.tags else {}),
        }
        rec_sparse = {sparse_name: encode(text, idf)} if use_sparse else {}
        records.append(
            VectorRecord(
                id=chunk_id(article_no, index),
                vector=vector,
                payload=payload,
                sparse=rec_sparse,
            )
        )

    stats.chunks = store.upsert(collection, records)
    logger.info(
        "索引完成：%d 篇文章 → %d 个 chunk → %s",
        stats.articles,
        stats.chunks,
        collection,
    )
    return stats


def search_kb(
    query: str,
    *,
    store: VectorStore | None = None,
    embedder: EmbeddingClient | None = None,
    settings: Settings | None = None,
    top_k: int = 5,
    category: str | None = None,
    collection: str | None = None,
) -> list[dict]:
    """检索知识库。返回可直接给模型看的 dict 列表。

    返回 dict 而不是 ``VectorHit`` 对象：调用方主要是"塞进 prompt"，
    而 ``VectorHit`` 带着 score、payload 这些内部结构，
    序列化进去会浪费 token，还会让模型困惑。

    真正给模型的只有 ``title`` / ``content`` / ``score`` 三项。
    """
    from fashionops.vectors import build_vector_store

    settings = settings or get_settings()
    collection = collection or settings.qdrant_collection_kb
    store = store or build_vector_store(settings)
    embedder = embedder or get_embedding_client(settings)

    if not query.strip():
        return []

    info = store.info(collection)
    if not info.exists or info.vector_count == 0:
        # 明确区分"库里没数据"和"检索没命中"。
        # 混为一谈的话，没建索引会被误判成"知识库里没有相关文章"，
        # 而这个结论会被当成事实写进回复
        raise RuntimeError(f"collection {collection!r} 不存在或为空 —— 知识库还没建索引。\n先跑：fashionops-kb index")

    # 只在需要时构造过滤条件。空的 filter 传下去会让某些后端生成
    # 一个恒真的条件，白白多一次求值
    flt = Filter(must={"category": category, "is_active": True}) if category else Filter(must={"is_active": True})

    query_vec = embedder.embed_one(query)

    # 混合检索：稠密走语义向量，稀疏走词法向量，两路在库内/本地做 RRF 融合。
    # 关掉 retrieval_hybrid（或向量后端不支持 search_hybrid）时退回纯稠密检索。
    # 查询侧复用建索引时落盘的 IDF，保证两路权重口径一致。
    use_sparse = bool(settings.retrieval_hybrid)
    if use_sparse and hasattr(store, "search_hybrid"):
        idf = load_idf(settings.data_dir)
        sparse_vec = encode(query, idf)
        if sparse_vec:
            hits = store.search_hybrid(
                collection,
                query_vec,
                {settings.kb_sparse_vector_name: sparse_vec},
                filter=flt,
                limit=top_k,
            )
        else:
            # 查询没有任何可编码的词项（全标点/空白）时，稀疏路无意义，退回稠密
            hits = store.search(collection, query_vec, limit=top_k, filter=flt)
    else:
        hits = store.search(collection, query_vec, limit=top_k, filter=flt)

    return [
        {
            "article_no": hit.source_id,
            "title": hit.payload.get("title", ""),
            "category": hit.payload.get("category", ""),
            "content": hit.text,
            "score": round(hit.score, 4),
        }
        for hit in hits
    ]


__all__ = [
    "IndexStats",
    "article_chunks",
    "build_index",
    "chunk_id",
    "search_kb",
]
