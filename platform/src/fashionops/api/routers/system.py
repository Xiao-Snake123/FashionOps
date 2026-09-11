"""系统级接口：健康检查与首页概览。

``/health`` 的设计原则：**依赖不可用时也要给出有用信息，而不是直接 500**。
启动阶段最常见的故障就是"MySQL 没起来"或"Redis 忘了开"，
如果这时候接口只回一个 500，排查得自己一个个去 ping，很费时间。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from fashionops import __version__
from fashionops.api.schemas import (
    DependencyStatus,
    HealthResponse,
    SummaryResponse,
)
from fashionops.config import Settings, get_settings
from fashionops.db import get_db
from fashionops.db.base import utcnow
from fashionops.db.models import (
    SKU,
    BIMetric,
    Inventory,
    Order,
    Product,
    Task,
    Ticket,
)

router = APIRouter(tags=["system"])


# ---------------------------------------------------------------------------
# 依赖探活
# ---------------------------------------------------------------------------
def _check_db(session: Session) -> DependencyStatus:
    started = time.perf_counter()
    try:
        session.execute(text("SELECT 1"))
        latency = (time.perf_counter() - started) * 1000
        settings = get_settings()
        backend = "SQLite（降级）" if settings.is_sqlite else "MySQL 8"
        return DependencyStatus(
            name="database", ok=True, detail=backend, latency_ms=round(latency, 2)
        )
    except Exception as exc:  # noqa: BLE001 - 探活接口要吞掉所有异常
        return DependencyStatus(name="database", ok=False, detail=f"{type(exc).__name__}: {exc}")


def _check_redis(settings: Settings) -> DependencyStatus:
    started = time.perf_counter()
    try:
        import redis

        client = redis.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=settings.redis_socket_timeout,
            socket_timeout=settings.redis_socket_timeout,
        )
        client.ping()
        server_version = client.info("server").get("redis_version", "?")
        latency = (time.perf_counter() - started) * 1000
        return DependencyStatus(
            name="redis", ok=True, detail=f"Redis {server_version}", latency_ms=round(latency, 2)
        )
    except Exception as exc:  # noqa: BLE001
        return DependencyStatus(name="redis", ok=False, detail=f"{type(exc).__name__}: {exc}")


def _check_qdrant(settings: Settings) -> DependencyStatus:
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=settings.qdrant_timeout) as client:
            resp = client.get(f"{settings.qdrant_url.rstrip('/')}/healthz")
            resp.raise_for_status()
        latency = (time.perf_counter() - started) * 1000
        return DependencyStatus(name="qdrant", ok=True, detail="Qdrant 就绪", latency_ms=round(latency, 2))
    except Exception as exc:  # noqa: BLE001
        return DependencyStatus(name="qdrant", ok=False, detail=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@router.get("/health", response_model=HealthResponse, summary="健康检查（含依赖探活）")
def health(session: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> HealthResponse:
    deps = [
        _check_db(session),
        _check_redis(settings),
        _check_qdrant(settings),
    ]
    failed = [d.name for d in deps if not d.ok]
    if not failed:
        status = "ok"
    elif failed == ["qdrant"]:
        # 向量库挂了不影响业务读写，只影响客服 RAG —— 降级而不是不可用
        status = "degraded"
    else:
        status = "degraded" if len(failed) < len(deps) else "down"

    return HealthResponse(
        status=status,
        app=settings.app_name,
        version=__version__,
        env=settings.env,
        time=utcnow(),
        dependencies=deps,
        config=settings.dependency_summary(),
    )


@router.get("/api/v1/summary", response_model=SummaryResponse, summary="首页概览数字")
def summary(session: Session = Depends(get_db)) -> SummaryResponse:
    since = utcnow() - timedelta(days=30)

    def scalar(stmt) -> int:  # noqa: ANN001
        return session.scalar(stmt) or 0

    def decimal_scalar(stmt) -> Decimal:  # noqa: ANN001
        return session.scalar(stmt) or Decimal("0")

    # ---- 近 30 天经营 ----
    gmv = decimal_scalar(select(func.sum(Order.actual_amount)).where(Order.paid_at >= since))
    ad_spend = decimal_scalar(select(func.sum(BIMetric.ad_spend)).where(BIMetric.metric_date >= since.date()))
    ad_revenue = decimal_scalar(select(func.sum(BIMetric.ad_revenue)).where(BIMetric.metric_date >= since.date()))

    # 退货率按订单数算更稳（按金额算会被大额订单带偏）
    order_count = scalar(select(func.count()).select_from(Order).where(Order.paid_at >= since))
    refund_count = scalar(
        select(func.count())
        .select_from(Order)
        .where(Order.paid_at >= since, Order.status.in_(("refunded", "refunding")))
    )

    # ---- 库存告急：取每个 SKU 最新一条流水的 quantity_after ----
    latest = (
        select(Inventory.sku_id, func.max(Inventory.occurred_at).label("ts"))
        .group_by(Inventory.sku_id)
        .subquery()
    )
    stockout = scalar(
        select(func.count())
        .select_from(Inventory)
        .join(latest, (Inventory.sku_id == latest.c.sku_id) & (Inventory.occurred_at == latest.c.ts))
        .where(Inventory.quantity_after <= 5)
    )

    return SummaryResponse(
        products=scalar(select(func.count()).select_from(Product)),
        skus=scalar(select(func.count()).select_from(SKU)),
        orders=scalar(select(func.count()).select_from(Order)),
        gmv_30d=gmv,
        return_rate_30d=(Decimal(refund_count) / Decimal(order_count) if order_count else None),
        ad_spend_30d=ad_spend,
        ad_roi_30d=(ad_revenue / ad_spend if ad_spend else None),
        open_tickets=scalar(
            select(func.count()).select_from(Ticket).where(Ticket.status.in_(("open", "processing", "waiting")))
        ),
        escalated_tickets=scalar(
            select(func.count()).select_from(Ticket).where(Ticket.escalated.is_(True))
        ),
        stockout_skus=stockout,
        running_tasks=scalar(select(func.count()).select_from(Task).where(Task.status == "running")),
        failed_tasks=scalar(select(func.count()).select_from(Task).where(Task.status == "failed")),
    )


@router.get("/api/v1/meta", summary="运行时元信息（不含敏感配置）")
def meta(settings: Settings = Depends(get_settings)) -> dict:
    return {
        "app": settings.app_name,
        "version": __version__,
        "env": settings.env,
        "timezone": settings.timezone,
        "now": datetime.now().isoformat(timespec="seconds"),
        "model_provider": settings.model_provider,
        "model_configured": settings.model_configured,
        "external_apis": {
            "ecom": settings.ecom_api_mode,
            "ad": settings.ad_api_mode,
            "erp": settings.erp_api_mode,
        },
    }
