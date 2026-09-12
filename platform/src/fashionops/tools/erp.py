"""ERP / 商品数据查询工具。

这些工具把多张表拼成**一次决策所需的最小完整数据集**，
而不是把原始表暴露给 Agent 让它自己去 JOIN。
原因：让模型花 token 去拼 SQL 逻辑，既慢又不可靠，
而且每次都得重新推导一遍口径（"可用库存"到底怎么算）。
放在工具里算一次、写清楚口径，才是正确的位置。
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from fashionops.db import session_scope
from fashionops.db.base import utcnow
from fashionops.db.models import SKU, Competitor, Inventory, Order, Product
from fashionops.gateway.registry import Scope, ToolRegistry


# ---------------------------------------------------------------------------
# 入参模型
# ---------------------------------------------------------------------------
class QueryCandidatesArgs(BaseModel):
    """候选商品查询参数。"""

    category: str | None = Field(default=None, description="一级类目，如「女装」。留空表示不限")
    season: str | None = Field(default=None, description="季节，如「秋季」。留空表示不限")
    limit: int = Field(default=50, ge=1, le=200, description="最多返回多少个 SPU")


class QueryInventoryArgs(BaseModel):
    sku_codes: list[str] = Field(min_length=1, max_length=100, description="SKU 编码列表")


# ---------------------------------------------------------------------------
# 内部查询
# ---------------------------------------------------------------------------
def _to_decimal(value: Any) -> Decimal | None:
    """把 avg/sum 的结果统一成 Decimal。

    MySQL 的 ``AVG`` 返回 ``Decimal``，SQLite 返回 ``float`` ——
    两边类型不一致，直接参与后续运算会得到不同的精度表现。
    在这里统一掉，上层就不用关心底层是哪个库。
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _latest_inventory_subquery():  # noqa: ANN202
    """每个 SKU 最新一条库存流水。

    "当前可用库存"的口径：**取最新一条流水的 ``quantity_after``**，
    而不是把流水里所有正负变动加起来。原因：流水里可能有盘点调整，
    直接求和会把"系统记录被纠正过"这件事算错。
    """
    return (
        select(Inventory.sku_id, func.max(Inventory.occurred_at).label("ts"))
        .group_by(Inventory.sku_id)
        .subquery()
    )


def _fetch_candidates(category: str | None, season: str | None, limit: int) -> list[dict[str, Any]]:
    since = utcnow() - timedelta(days=30)
    latest = _latest_inventory_subquery()

    stock_sub = (
        select(SKU.product_id.label("product_id"), func.sum(Inventory.quantity_after).label("stock"))
        .join(latest, (Inventory.sku_id == latest.c.sku_id) & (Inventory.occurred_at == latest.c.ts))
        .join(SKU, SKU.id == Inventory.sku_id)
        .group_by(SKU.product_id)
        .subquery()
    )

    sales_sub = (
        select(SKU.product_id.label("product_id"), func.sum(Order.quantity).label("sold"))
        .join(SKU, SKU.id == Order.sku_id)
        .where(Order.paid_at >= since)
        .group_by(SKU.product_id)
        .subquery()
    )

    # 竞品按类目算平均价 —— 选品要判断"这款的定价相对同类竞品有没有空间"
    comp_sub = (
        select(Competitor.category.label("category"), func.avg(Competitor.price).label("avg_price"))
        .group_by(Competitor.category)
        .subquery()
    )

    stmt = (
        select(
            Product,
            func.coalesce(stock_sub.c.stock, 0).label("stock"),
            func.coalesce(sales_sub.c.sold, 0).label("sold_30d"),
            comp_sub.c.avg_price.label("competitor_avg_price"),
        )
        .outerjoin(stock_sub, stock_sub.c.product_id == Product.id)
        .outerjoin(sales_sub, sales_sub.c.product_id == Product.id)
        .outerjoin(comp_sub, comp_sub.c.category == Product.category)
        .order_by(Product.id)
        .limit(limit)
    )
    if category:
        stmt = stmt.where(Product.category == category)
    if season:
        stmt = stmt.where(Product.season == season)

    rows: list[dict[str, Any]] = []
    with session_scope() as session:
        for product, stock, sold_30d, competitor_avg in session.execute(stmt).all():
            cost = _to_decimal(product.cost_price)
            price = _to_decimal(product.list_price)
            margin_rate = (
                float((price - cost) / price) if price and cost and price > 0 else None
            )
            rows.append(
                {
                    "product_id": product.id,
                    "spu_code": product.spu_code,
                    "title": product.title,
                    "category": product.category,
                    "sub_category": product.sub_category,
                    "season": product.season,
                    "style_tags": product.style_tags or [],
                    "status": product.status,
                    "cost_price": float(cost) if cost is not None else None,
                    "list_price": float(price) if price is not None else None,
                    "margin_rate": margin_rate,
                    "rating": float(product.rating) if product.rating is not None else None,
                    "available_stock": int(stock or 0),
                    "sold_30d": int(sold_30d or 0),
                    "competitor_avg_price": (
                        float(v) if (v := _to_decimal(competitor_avg)) is not None else None
                    ),
                }
            )
    return rows


def _fetch_inventory(sku_codes: list[str]) -> list[dict[str, Any]]:
    latest = _latest_inventory_subquery()
    stmt = (
        select(SKU.sku_code, SKU.color, SKU.size, Product.title, Inventory.quantity_after)
        .join(latest, (Inventory.sku_id == latest.c.sku_id) & (Inventory.occurred_at == latest.c.ts))
        .join(SKU, SKU.id == Inventory.sku_id)
        .join(Product, Product.id == SKU.product_id)
        .where(SKU.sku_code.in_(sku_codes))
    )
    with session_scope() as session:
        return [
            {
                "sku_code": sku_code,
                "color": color,
                "size": size,
                "product_title": title,
                "available": int(available or 0),
            }
            for sku_code, color, size, title, available in session.execute(stmt).all()
        ]


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------
def register(registry: ToolRegistry) -> None:
    """把本模块的工具注册进给定注册表。"""

    @registry.tool(
        name="erp.query_candidates",
        description=(
            "查询候选商品池，一次返回选品决策所需的全部字段："
            "成本、吊牌价、毛利率、站内评分、当前可用库存、近 30 天销量、"
            "同类目竞品平均价。用于选品打分。"
        ),
        args_model=QueryCandidatesArgs,
        scopes=(Scope.ERP_READ,),
        read_only=True,
        timeout_s=15.0,
        tags=("products",),
        rate_limit_per_minute=60,
    )
    def query_candidates(
        category: str | None = None, season: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        rows = _fetch_candidates(category, season, limit)
        return {"count": len(rows), "candidates": rows}

    @registry.tool(
        name="erp.query_inventory",
        description="按 SKU 编码查询当前可用库存（颜色 / 尺码维度）。",
        args_model=QueryInventoryArgs,
        scopes=(Scope.ERP_READ,),
        read_only=True,
        timeout_s=10.0,
        tags=("inventory",),
    )
    def query_inventory(sku_codes: list[str]) -> dict[str, Any]:
        rows = _fetch_inventory(sku_codes)
        return {"count": len(rows), "items": rows}
