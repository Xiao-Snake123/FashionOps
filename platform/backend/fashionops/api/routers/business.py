"""业务查询接口（全部只读）。

这些接口的作用是**让前端有真实数据可渲染**。
写入侧的接口（提交任务、人工确认、改预算）走工具网关，
带上限流、审计、熔断 —— 不会像这里这样直连 Session。

## 鉴权：路由级"要登录" + 端点级"要权限"

先在整个 router 上挂 `Depends(get_principal)`，保证**任何**业务数据都要求登录 ——
这样新增端点时"忘了加鉴权"不会发生（默认就是关着的）。

然后按资源加具体权限。为什么不全用路由级一刀切：
`/emails` 会返回**收件人地址和邮件正文**（对外通信的内容），
而 `/products` 只是商品列表。两者的敏感度差一个数量级，
用同一个门槛意味着"能看商品的人也能看所有人的对外邮件"。
**权限粒度按数据敏感度定，不按代码方便定。**
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from fashionops.api.schemas import (
    AdCampaignOut,
    AssetOut,
    BIMetricOut,
    EmailOut,
    KBArticleOut,
    Page,
    ProductOut,
    TicketOut,
)
from fashionops.cache import (
    business_cache_key,
    read_cached_json,
    write_cached_json,
)
from fashionops.db import get_db
from fashionops.db.models import (
    SKU,
    AdCampaign,
    Asset,
    BIMetric,
    EmailMessage,
    Inventory,
    KBArticle,
    Product,
    Ticket,
)
from fashionops.security.deps import get_principal, require_permission
from fashionops.security.permissions import (
    PERM_AD_READ,
    PERM_ERP_READ,
    PERM_KB_READ,
    PERM_MAIL_SEND,
    PERM_TASK_READ,
)
from fashionops.security.service import Principal
from fashionops.storage import PathEscapeError, get_storage

#: 路由级：**所有**业务接口都要求登录。
#: 放在 router 上而不是每个函数签名里，是为了让"新加的端点忘了加鉴权"
#: 这件事在结构上不可能发生 —— 依赖注入默认就是开着的。
router = APIRouter(
    prefix="/api/v1",
    tags=["business"],
    dependencies=[Depends(get_principal)],
)


def _paginate(session: Session, stmt, page: int, page_size: int):  # noqa: ANN001
    """统一分页：先数总数，再切片。

    用 ``count(*) over ()`` 也行，但 SQLite 老版本不支持窗口函数，
    保持两段查询更省心（数据量在这个规模下差异可以忽略）。
    """
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(stmt.offset((page - 1) * page_size).limit(page_size)).all()
    return rows, total


# ---------------------------------------------------------------------------
# 商品
# ---------------------------------------------------------------------------
@router.get("/products", response_model=Page[ProductOut], summary="商品列表")
def list_products(
    _: Principal = Depends(require_permission(PERM_ERP_READ)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    category: str | None = None,
    season: str | None = None,
    status: str | None = None,
    q: str | None = Query(None, description="标题模糊搜索"),
) -> Page[ProductOut]:
    stmt = select(Product)
    if category:
        stmt = stmt.where(Product.category == category)
    if season:
        stmt = stmt.where(Product.season == season)
    if status:
        stmt = stmt.where(Product.status == status)
    if q:
        stmt = stmt.where(Product.title.like(f"%{q}%"))
    stmt = stmt.order_by(Product.id)

    rows, total = _paginate(session, stmt, page, page_size)
    return Page[ProductOut](
        items=[ProductOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/products/categories", summary="类目与季节的可选值（给前端筛选器用）")
def product_facets(
    _: Principal = Depends(require_permission(PERM_ERP_READ)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> dict:
    # 前端筛选器的可选值：商品表全表 GROUP BY 两次，而类目/季节几乎不变。
    key = business_cache_key("product_facets")
    hit = read_cached_json(key)
    if hit is not None:
        return hit

    cats = session.execute(
        select(Product.category, func.count()).group_by(Product.category)
    ).all()
    seasons = session.execute(
        select(Product.season, func.count()).group_by(Product.season)
    ).all()
    payload = {
        "categories": [{"value": c, "count": n} for c, n in cats if c],
        "seasons": [{"value": s, "count": n} for s, n in seasons if s],
    }
    write_cached_json(key, payload)
    return payload


# ---------------------------------------------------------------------------
# 库存（选品 Agent 的硬约束数据源）
# ---------------------------------------------------------------------------
@router.get("/inventory/low-stock", summary="库存告急 SKU（选品会被硬约束过滤掉的）")
def low_stock(
    _: Principal = Depends(require_permission(PERM_ERP_READ)),  # noqa: B008
    session: Session = Depends(get_db),
    threshold: int = Query(5, ge=0, le=1000),
) -> list[dict]:
    # threshold 是查询参数，**必须进 key** —— 否则用 threshold=5 查一次之后，
    # threshold=50 的请求会拿到 5 的结果。
    key = business_cache_key("low_stock", threshold)
    hit = read_cached_json(key)
    if hit is not None:
        return hit

    latest = (
        select(Inventory.sku_id, func.max(Inventory.occurred_at).label("ts"))
        .group_by(Inventory.sku_id)
        .subquery()
    )
    rows = session.execute(
        select(SKU.sku_code, SKU.color, SKU.size, Product.title, Inventory.quantity_after)
        .join(latest, (Inventory.sku_id == latest.c.sku_id) & (Inventory.occurred_at == latest.c.ts))
        .join(SKU, SKU.id == Inventory.sku_id)
        .join(Product, Product.id == SKU.product_id)
        .where(Inventory.quantity_after <= threshold)
        .order_by(Inventory.quantity_after)
        .limit(200)
    ).all()
    payload = [
        {
            "sku_code": sku_code,
            "color": color,
            "size": size,
            "product_title": title,
            "available": available,
        }
        for sku_code, color, size, title, available in rows
    ]
    write_cached_json(key, payload)
    return payload


# ---------------------------------------------------------------------------
# 工单 / 广告 / 指标 / 素材 / 知识库
# ---------------------------------------------------------------------------
# 注：``/tasks*`` 那一组已经搬到 ``routers/tasks.py`` ——
# 它现在是**写接口**（提交任务 / 人工确认 / SSE），和这里的只读查询
# 不是同一张责任表。**一个资源只能有一个所有者**：
# 两边都定义 ``/tasks/{no}`` 的话，FastAPI 会静默使用先注册的那个，
# 表现为"改了代码没生效"，极难排查。
# ---------------------------------------------------------------------------
@router.get("/tickets", response_model=Page[TicketOut], summary="客服工单列表")
def list_tickets(
    _: Principal = Depends(require_permission(PERM_KB_READ)),  # noqa: B008
    session: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    status: str | None = None,
    only_escalated: bool = False,
) -> Page[TicketOut]:
    stmt = select(Ticket)
    if status:
        stmt = stmt.where(Ticket.status == status)
    if only_escalated:
        stmt = stmt.where(Ticket.escalated.is_(True))
    stmt = stmt.order_by(Ticket.created_at.desc())

    rows, total = _paginate(session, stmt, page, page_size)
    return Page[TicketOut](
        items=[TicketOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/campaigns", response_model=Page[AdCampaignOut], summary="广告投放计划")
def list_campaigns(
    _: Principal = Depends(require_permission(PERM_AD_READ)),  # noqa: B008
    session: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    only_losing: bool = Query(False, description="只看 ROI < 1.0 的亏损计划"),
) -> Page[AdCampaignOut]:
    stmt = select(AdCampaign)
    if only_losing:
        stmt = stmt.where(AdCampaign.roi < Decimal("1.0"))

    # ROI 可能为 NULL（计划刚建、还没回传数据），这些要排在**最后**。
    #
    # 原本写的是 ``AdCampaign.roi.asc().nulls_last()``，在 SQLite 上一直正常 ——
    # SQLite 3.30+ 支持 ``ORDER BY x NULLS LAST``。
    # 但 **MySQL 8.0 不支持这个语法**，会直接报：
    #     1064 ... near 'NULLS LAST'
    # 这个 bug 只在切到真 MySQL 时才暴露，是典型的“开发库选型掩盖了方言差异”。
    #
    # 用 ``case`` 显式排序键来兼容两边：NULL 记 1、非 NULL 记 0，
    # 升序排序时非 NULL 自然排在前面。语义一目了然，也不依赖任何方言特性。
    null_rank = case((AdCampaign.roi.is_(None), 1), else_=0)
    stmt = stmt.order_by(null_rank, AdCampaign.roi.asc())

    rows, total = _paginate(session, stmt, page, page_size)
    return Page[AdCampaignOut](
        items=[AdCampaignOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/metrics/daily", response_model=list[BIMetricOut], summary="日度经营指标")
def daily_metrics(
    _: Principal = Depends(require_permission(PERM_AD_READ)),  # noqa: B008
    session: Session = Depends(get_db),
    category: str = Query("全部"),
    days: int = Query(30, ge=1, le=180),
) -> list[BIMetricOut]:
    # category / days 都影响结果，全部进 key。
    key = business_cache_key("daily_metrics", category, days)
    hit = read_cached_json(key)
    if hit is not None:
        return [BIMetricOut.model_validate(i) for i in hit]

    latest_day = session.scalar(select(func.max(BIMetric.metric_date)))
    if latest_day is None:
        return []
    start: date = latest_day.fromordinal(latest_day.toordinal() - days + 1)
    rows = session.scalars(
        select(BIMetric)
        .where(BIMetric.category == category, BIMetric.metric_date >= start)
        .order_by(BIMetric.metric_date)
    ).all()
    items = [BIMetricOut.model_validate(r) for r in rows]
    write_cached_json(key, [i.model_dump(mode="json") for i in items])
    return items


@router.get("/assets", response_model=Page[AssetOut], summary="素材列表")
def list_assets(
    # 产物（报表/图片/视频）是任务的产出，所以用 task:read 把关 ——
    # 能看到任务的人就能看到它产出了什么，这个粒度是自然的
    _: Principal = Depends(require_permission(PERM_TASK_READ)),  # noqa: B008
    session: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    asset_type: str | None = None,
    review_status: str | None = None,
) -> Page[AssetOut]:
    stmt = select(Asset)
    if asset_type:
        stmt = stmt.where(Asset.asset_type == asset_type)
    if review_status:
        stmt = stmt.where(Asset.review_status == review_status)
    stmt = stmt.order_by(Asset.created_at.desc())

    rows, total = _paginate(session, stmt, page, page_size)
    return Page[AssetOut](
        items=[AssetOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/kb", response_model=Page[KBArticleOut], summary="客服知识库文章（RAG 的召回源）")
def list_kb(
    _: Principal = Depends(require_permission(PERM_KB_READ)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    category: str | None = None,
) -> Page[KBArticleOut]:
    # 原来是**全表返回**：文章数涨上去之后每次都把整张表搬一遍。
    # 改成统一分页后，缓存 key 里也必须带上 page / page_size。
    key = business_cache_key("kb", category, page, page_size)
    hit = read_cached_json(key)
    if hit is not None:
        return Page[KBArticleOut](**hit)

    stmt = select(KBArticle).where(KBArticle.is_active.is_(True))
    if category:
        stmt = stmt.where(KBArticle.category == category)
    rows, total = _paginate(session, stmt.order_by(KBArticle.article_no), page, page_size)
    payload = Page[KBArticleOut](
        items=[KBArticleOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )
    write_cached_json(key, payload.model_dump(mode="json"))
    return payload


@router.get("/emails", response_model=Page[EmailOut], summary="邮件记录（含被闸门拦下的）")
def list_emails(
    # **邮件记录里含收件人地址和正文**，是对外通信的内容。
    # 门槛设成 mail:send（而不是 erp:read 那种普适读权限）：
    # “能不能对外发消息”和“能不能看到别人对外发了什么”是同一档敏感度。
    _: Principal = Depends(require_permission(PERM_MAIL_SEND)),  # noqa: B008
    session: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    status: str | None = None,
    gate_decision: str | None = Query(None, description="精确匹配某个闸门决策码"),
    only_blocked: bool = Query(False, description="只看被策略拦下的"),
) -> Page[EmailOut]:
    """邮件列表。

    **默认同时返回放行和被拦的**。只给"发成功的"会让被拦的完全不可见 ——
    运维看到"这周只发了 12 封"，却不知道还有 8 封被策略挡住、
    而业务方以为已经通知了。静默拦截比报错更危险，因为没人会去查。
    """
    stmt = select(EmailMessage)
    if status:
        stmt = stmt.where(EmailMessage.status == status)
    if gate_decision:
        stmt = stmt.where(EmailMessage.gate_decision == gate_decision)
    if only_blocked:
        stmt = stmt.where(EmailMessage.gate_decision.like("blocked%"))
    stmt = stmt.order_by(EmailMessage.created_at.desc())

    rows, total = _paginate(session, stmt, page, page_size)
    return Page[EmailOut](
        items=[EmailOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/emails/stats", summary="邮件闸门统计（被拦截原因分布）")
def email_gate_stats(
    _: Principal = Depends(require_permission(PERM_MAIL_SEND)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> dict:
    """按 ``gate_decision`` 分组计数。界面上直接画成一张分布图。"""
    rows = session.execute(
        select(EmailMessage.gate_decision, func.count())
        .group_by(EmailMessage.gate_decision)
        .order_by(func.count().desc())
    ).all()
    return {
        "by_decision": [{"decision": d or "unknown", "count": n} for d, n in rows],
        "blocked_total": sum(n for d, n in rows if d and d.startswith("blocked")),
        "total": sum(n for _, n in rows),
    }


@router.get(
    "/assets/raw/{key:path}",
    summary="下载素材原文件（report.export_excel 返回的 URL 就指向这里）",
    response_class=Response,
)
def download_asset(
    key: str,
    _: Principal = Depends(require_permission(PERM_TASK_READ)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> Response:
    """把存储里的原始文件流出去。

    ``report.export_excel`` 返回的 ``url`` 指向这个路由 ——
    没有它的话，Agent 写进邮件正文的链接是个死链，
    "产出可见"这个目标就只完成了一半。

    安全上做两层：先按 ``storage_path`` 查 ``assets`` 表（未登记的 key 一律不服务，
    这比直接信任 URL 里的路径安全得多），再交给存储层自己去挡路径逃逸。
    """
    asset = session.scalar(select(Asset).where(Asset.storage_path == key))
    if asset is None:
        raise HTTPException(status_code=404, detail="素材不存在或未登记")

    try:
        payload = get_storage().read(key)
    except (PathEscapeError, FileNotFoundError, OSError) as exc:
        # 表里有记录但文件没了：明确报 410 而不是 500，
        # 因为"文件被清理掉了"和"服务出错"对调用方是两件不同的事
        raise HTTPException(status_code=410, detail=f"素材文件已不可用：{exc}") from exc

    # 文件名可能是中文，必须按 RFC 5987 编码放在 filename* 里，
    # 直接塞进 filename= 会让部分客户端拿到乱码或直接截断
    disposition = f"attachment; filename*=UTF-8''{quote(asset.name)}"
    return Response(
        content=payload,
        media_type=asset.mime_type or "application/octet-stream",
        headers={"Content-Disposition": disposition},
    )
