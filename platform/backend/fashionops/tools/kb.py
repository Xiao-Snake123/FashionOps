"""知识库检索工具 —— 客服 Agent 的 RAG 召回入口。

## 为什么 RAG 必须走工具而不是节点内部直接查

看起来"检索知识库"是客服 Agent 的内部逻辑，直接 `store.search(...)` 更简单。
但它是一**次对外的、可能失败的、需要被计量和治理的调用**：

* **限流**：检索会打 embedding API（花钱、有 QPS 限制）。
  模型如果陷入循环反复检索，直接调用就会一直烧钱。
* **审计**：需要记录"哪次对话召回了哪些文章" ——
  出了错误回复要能复盘是"没召回"还是"召回了但模型没用好"。
  这是 RAG 系统最常见的归因分叉。
* **超时与熔断**：embedding 服务抖动时不能让整个对话卡死。

所以它和 `erp.query_*` 一样是工具，只是数据和算力都在我们自己这边。

## 为什么返回"文本片段"而不是"文章"

客服的回答需要**具体到某一句话**（"7 天内"），而不是"有一篇叫《退换货政策》
的文章"。返回整篇会让模型拿到大量无关内容，反而更容易答偏。
所以工具只回 top-k 个片段 + 来源，让模型基于片段作答。

同时带上 ``article_no``：答案里可以引用"依据 KB-0003"，用户能去核对。
**可溯源**是 RAG 相对"模型自由发挥"的核心价值之一。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from fashionops.gateway.registry import Scope, ToolRegistry, ToolSpec

logger = logging.getLogger("fashionops.tools.kb")

#: 一次最多回几段。上限压到 8：再多 token 成本明显上升，
#: 而模型的注意力会被分散，效果反而下降（这是 RAG 里的常见反直觉现象）
MAX_TOP_K = 8


class KBSearchArgs(BaseModel):
    query: str = Field(
        min_length=1,
        max_length=500,
        description="检索问题，用自然语言。例如「退货需要几天内申请」",
    )
    top_k: int = Field(
        default=3,
        ge=1,
        le=MAX_TOP_K,
        description="返回几段最相关的内容。一般 3 段够用；问题涉及多个方面时可以调到 5",
    )
    category: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "只在某个类目里检索，可选值：尺码/退换货/物流/材质/发票/优惠/支付。"
            "只有明确知道问题属于哪一类时才传 —— 传错了会把正确答案过滤掉"
        ),
    )


class KBCategoriesArgs(BaseModel):
    """无参数。用空模型而不是 ``None``：网关会调 ``model_json_schema()``，
    ``None`` 会直接 `TypeError`。"""


def _search(query: str, top_k: int, category: str | None, *, settings: Any = None) -> dict[str, Any]:
    from fashionops.kb import search_kb

    try:
        hits = search_kb(query, top_k=top_k, category=category, settings=settings)
    except RuntimeError as exc:
        # "索引为空"是**配置问题**，不是"没找到答案"。
        # 混为一谈的话，客服会对用户说"知识库里没有这个问题的答案"，
        # 而真实原因是运维忘了建索引 —— 用户得到的是一个假结论。
        return {
            "found": False,
            "reason": "index_not_ready",
            "message": str(exc),
            "hint": "这是运维问题：知识库索引没建。先跑 fashionops-kb index",
            "items": [],
        }

    if not hits:
        return {
            "found": False,
            "reason": "no_match",
            "message": "知识库里没有与这个问题相关的内容",
            "hint": "不要凭常识编造答案，应转人工处理",
            "items": [],
        }

    return {
        "found": True,
        "count": len(hits),
        "items": [
            {
                "article_no": hit["article_no"],
                "title": hit["title"],
                "category": hit["category"],
                "content": hit["content"],
                # 相似度给出来是让模型知道"这几段谁更可信"。
                # 但它**只是参考** —— 0.55 和 0.57 之间没有实质差别，
                # 所以不要在 prompt 里写"低于 X 分就不要用"
                "score": hit["score"],
            }
            for hit in hits
        ],
        "note": "回答时请引用 article_no 作为依据；如果这些片段不足以回答，明确说明并建议转人工",
    }


def _categories(*, settings: Any = None) -> dict[str, Any]:
    """列出知识库里有哪些类目及各自文章数。

    存在的理由：Agent 想按类目过滤时，得先知道有哪些合法值。
    没有这个工具，它只能靠猜（猜错的后果是**把正确答案过滤掉**，
    而且不会报错，只是检索不到）。
    """
    from sqlalchemy import func, select

    from fashionops.db import session_scope
    from fashionops.db.models import KBArticle

    with session_scope() as session:
        rows = session.execute(
            select(KBArticle.category, func.count())
            .where(KBArticle.is_active.is_(True))
            .group_by(KBArticle.category)
            .order_by(func.count().desc())
        ).all()

    return {
        "categories": [{"category": name, "articles": count} for name, count in rows],
        "total_articles": sum(count for _, count in rows),
    }


def register(registry: ToolRegistry) -> None:
    """把知识库工具注册进注册表。"""
    registry.register(
        ToolSpec(
            name="kb.search",
            description=(
                "在客服知识库里检索相关的内容片段。"
                "回答任何关于尺码/退换货/物流/材质/发票/优惠/支付的问题之前，"
                "都应该先调这个工具，不要凭常识回答。"
                "返回的每段内容都带 article_no，回答时要引用它。"
            ),
            args_model=KBSearchArgs,
            handler=_search,
            scopes=frozenset({Scope.KB_READ}),
            read_only=True,
            # 检索是纯读 + 可重复，不需要幂等键；但也不该因为
            # 限流组件抖动就整个失败 —— 只读工具 fail-open
            idempotent=False,
            timeout_s=15.0,
            # 单次检索要打一次 embedding API，比本地 SQL 贵得多，
            # 所以限流收得比读库工具紧
            rate_limit_per_minute=60,
            source="qdrant",
            tags=("rag", "kb"),
        )
    )

    registry.register(
        ToolSpec(
            name="kb.categories",
            description=(
                "列出知识库的所有类目和每类的文章数。"
                "当你想用 category 参数过滤检索范围时，先调它拿到合法的类目名 —— "
                "猜错类目名会静默地过滤掉正确答案。"
            ),
            args_model=KBCategoriesArgs,
            handler=_categories,
            scopes=frozenset({Scope.KB_READ}),
            read_only=True,
            idempotent=False,
            timeout_s=5.0,
            source="mysql",
            tags=("rag", "kb"),
        )
    )


__all__ = ["MAX_TOP_K", "KBCategoriesArgs", "KBSearchArgs", "register"]
