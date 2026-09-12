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
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import case, func, select

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


class SalesSummaryArgs(BaseModel):
    """销售汇总查询参数。"""

    days: int = Field(default=30, ge=1, le=180, description="回溯天数")
    category: str | None = Field(default=None, description="只统计某个一级类目，留空表示全部")
    group_by: Literal["category", "day", "channel"] = Field(
        default="category",
        description="按什么维度分组：类目 / 日期 / 渠道",
    )
    top_n: int = Field(default=30, ge=1, le=200, description="最多返回多少组，按 GMV 降序")


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


def _fetch_sales_summary(
    days: int, category: str | None, group_by: str, top_n: int
) -> list[dict[str, Any]]:
    """按维度聚合订单。

    **退货率按订单数算而不是按金额算**。按金额算会被大额订单带偏：
    一单 5000 元的礼服退货，能把整体退货率拉高好几个点，
    但它对"这个款到底好不好卖"几乎没有信息量。
    行业里说的退货率一般也是订单口径。
    """
    since = utcnow() - timedelta(days=days)

    if group_by == "day":
        # func.date 在 SQLite 和 MySQL 上都能用，不用分平台写
        key_expr = func.date(Order.paid_at).label("bucket")
    elif group_by == "category":
        key_expr = Product.category.label("bucket")
    else:
        key_expr = Order.channel.label("bucket")

    stmt = (
        select(
            key_expr,
            func.count().label("order_count"),
            func.sum(Order.actual_amount).label("gmv"),
            func.count(func.distinct(Order.buyer_nick)).label("customer_count"),
            func.sum(
                case((Order.status.in_(("refunded", "refunding")), Order.actual_amount), else_=0)
            ).label("refund_amount"),
            func.sum(
                case((Order.status.in_(("refunded", "refunding")), 1), else_=0)
            ).label("refund_count"),
        )
        .select_from(Order)
        .where(Order.paid_at >= since, Order.status != "cancelled")
        .group_by(key_expr)
        .order_by(func.sum(Order.actual_amount).desc())
        .limit(top_n)
    )

    if group_by == "category":
        # outer join：订单的 sku_id 在商品被删时会置空，inner join 会把这些单子漏掉
        stmt = stmt.outerjoin(SKU, SKU.id == Order.sku_id).outerjoin(
            Product, Product.id == SKU.product_id
        )

    if category and group_by != "category":
        stmt = stmt.outerjoin(SKU, SKU.id == Order.sku_id).outerjoin(
            Product, Product.id == SKU.product_id
        ).where(Product.category == category)
    elif category:
        stmt = stmt.where(Product.category == category)

    rows: list[dict[str, Any]] = []
    with session_scope() as session:
        for bucket, orders, gmv, customers, refund, refund_count in session.execute(stmt).all():
            gmv_d = _to_decimal(gmv) or Decimal("0")
            refund_d = _to_decimal(refund) or Decimal("0")
            orders = int(orders or 0)
            rows.append(
                {
                    "group": str(bucket) if bucket is not None else "（未分类）",
                    "order_count": orders,
                    "customer_count": int(customers or 0),
                    "gmv": float(gmv_d),
                    "refund_amount": float(refund_d),
                    "avg_order_value": float(
                        (gmv_d / orders).quantize(Decimal("0.01")) if orders else Decimal("0")
                    ),
                    "refund_count": int(refund_count or 0),
                    "return_rate": round(int(refund_count or 0) / orders, 4) if orders else 0.0,
                }
            )
    return rows


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

    @registry.tool(
        name="erp.query_sales_summary",
        description=(
            "按类目 / 日期 / 渠道聚合销售数据，返回 GMV、订单数、客单价、"
            "退款金额与退货率。用于经营分析与报表。"
        ),
        args_model=SalesSummaryArgs,
        scopes=(Scope.ERP_READ,),
        read_only=True,
        timeout_s=20.0,
        tags=("analytics",),
        rate_limit_per_minute=30,
    )
    def query_sales_summary(
        days: int = 30,
        category: str | None = None,
        group_by: str = "category",
        top_n: int = 30,
    ) -> dict[str, Any]:
        rows = _fetch_sales_summary(days, category, group_by, top_n)
        total_gmv = sum(r["gmv"] for r in rows)
        total_orders = sum(r["order_count"] for r in rows)
        total_refund = sum(r["refund_amount"] for r in rows)
        return {
            "days": days,
            "group_by": group_by,
            "count": len(rows),
            "totals": {
                "gmv": round(total_gmv, 2),
                "order_count": total_orders,
                "refund_amount": round(total_refund, 2),
                "avg_order_value": round(total_gmv / total_orders, 2) if total_orders else 0.0,
            },
            "groups": rows,
        }
