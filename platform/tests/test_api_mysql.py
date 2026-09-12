"""跨方言接口测试 —— 在**真实 MySQL** 上把所有只读接口跑一遍。

## 为什么必须有这个文件

切到本地 MySQL 的那天，`/api/v1/campaigns` 直接 500：:

    pymysql.err.ProgrammingError: (1064, "...near 'NULLS LAST) AS anon_1'")

原因是排序写成了 ``AdCampaign.roi.asc().nulls_last()``。
SQLite 3.30+ 支持 ``ORDER BY x NULLS LAST``，所以开发期一切正常；
**MySQL 8.0 不支持这个语法**，只有换库才暴露。

单元测试抓不到它，因为单元测试跑的是 SQLite —— 同一个"方言盲区"。
这不是测试写得少，而是**测试和开发用同一个库，就永远测不出方言差异**。

所以这个文件只做一件事：拿真实 MySQL 当后端，把接口全部打一遍。
没有 MySQL 时整个文件自动跳过（沿用 ``test_gateway_redis.py`` 的做法），
所以 CI 上不装 MySQL 也能跑。

> 注意：接口全是只读 GET，不会往开发库里写数据。
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from fashionops.api.main import app
from fashionops.config import get_settings
from fashionops.db.base import Base, build_engine, get_db, reset_engine, utcnow

# ---------------------------------------------------------------------------
# 接口清单：加一个新接口就补一行，这里的价值在于"新增接口别忘了过 MySQL"
# ---------------------------------------------------------------------------
READ_ENDPOINTS: tuple[str, ...] = (
    "/health",
    "/api/v1/summary",
    "/api/v1/meta",
    "/api/v1/products?page_size=200",
    "/api/v1/products/categories",
    "/api/v1/inventory/low-stock?threshold=5",
    "/api/v1/tasks?page_size=200",
    "/api/v1/tickets?page_size=200",
    "/api/v1/tickets?page_size=200&only_escalated=true",
    "/api/v1/campaigns?page_size=200",
    "/api/v1/campaigns?page_size=200&only_losing=true",
    "/api/v1/metrics/daily?days=30",
    "/api/v1/assets?page_size=200",
    "/api/v1/kb",
)


# ===========================================================================
# 1. 排序语义：NULL 必须排在最后（用 SQLite，随时可跑，不依赖外部服务）
# ===========================================================================
@pytest.fixture
def sqlite_client(tmp_path, monkeypatch):
    """临时 SQLite 库 + 只插广告计划的最小数据集。"""
    from sqlalchemy.orm import Session

    from fashionops.config import Settings
    from fashionops.db import base as db_base

    settings = Settings(data_dir=tmp_path, db_host=None)
    reset_engine()
    monkeypatch.setattr(db_base, "get_settings", lambda: settings)

    engine = build_engine(settings)
    Base.metadata.create_all(engine)

    from fashionops.db.models import AdCampaign

    with Session(engine) as session:
        session.add_all(
            [
                AdCampaign(
                    campaign_no="AD-ROI-3",
                    name="高 ROI",
                    platform="mock",
                    objective="gmv",
                    status="running",
                    spend=Decimal("100.00"),
                    revenue=Decimal("390.00"),
                    roi=Decimal("3.90"),
                    start_at=utcnow() - timedelta(days=5),
                ),
                # 刚建、还没回传数据 → roi 为 NULL
                AdCampaign(
                    campaign_no="AD-ROI-NULL",
                    name="新建未回传",
                    platform="mock",
                    objective="gmv",
                    status="draft",
                    spend=Decimal("0.00"),
                    revenue=Decimal("0.00"),
                    roi=None,
                    start_at=utcnow() - timedelta(days=1),
                ),
                AdCampaign(
                    campaign_no="AD-ROI-0",
                    name="亏损",
                    platform="mock",
                    objective="roi",
                    status="running",
                    spend=Decimal("500.00"),
                    revenue=Decimal("250.00"),
                    roi=Decimal("0.50"),
                    start_at=utcnow() - timedelta(days=5),
                ),
            ]
        )
        session.commit()

    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=engine)

    def _override():  # noqa: ANN202
        with factory() as s:
            yield s

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
    reset_engine()


class TestCampaignOrdering:
    """ROI 为 NULL 的行必须排在最后。

    写成 ``.nulls_last()`` 时 SQLite 通过、MySQL 报语法错 ——
    这个用例把**语义**锁住，另一个类把**方言**锁住，两者缺一不可。
    """

    def test_nulls_sort_last(self, sqlite_client):
        resp = sqlite_client.get("/api/v1/campaigns?page_size=200")
        assert resp.status_code == 200, resp.text
        rois = [item["roi"] for item in resp.json()["items"]]
        assert rois[-1] is None, f"NULL 没排在最后：{rois}"
        non_null = [r for r in rois if r is not None]
        assert non_null == sorted(non_null), f"非空 ROI 没有升序：{non_null}"

    def test_only_losing_still_works_with_null_present(self, sqlite_client):
        resp = sqlite_client.get("/api/v1/campaigns?page_size=200&only_losing=true")
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert [i["campaign_no"] for i in items] == ["AD-ROI-0"]


# ===========================================================================
# 2. 真实 MySQL：接口全部打一遍
# ===========================================================================
@pytest.fixture(scope="module")
def mysql_engine():
    """连真实 MySQL。没配 / 连不上 / 没建表都直接跳过，不报失败。"""
    settings = get_settings()
    if settings.is_sqlite:
        pytest.skip("当前 .env 的 db_host 为空（跑的是 SQLite），跳过跨方言测试")

    try:
        engine = build_engine(settings)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"本机 MySQL 连不上，跳过：{type(exc).__name__}: {exc}")

    existing = set(inspect(engine).get_table_names())
    from fashionops.db.models import ALL_MODELS

    absent = [m.__tablename__ for m in ALL_MODELS if m.__tablename__ not in existing]
    if absent:
        pytest.skip(
            f"MySQL 里还没建表（缺 {len(absent)} 张）：先跑 "
            f"fashionops-db init && fashionops-seed。缺表清单：{absent[:5]}…"
        )
    return engine


@pytest.fixture(scope="module")
def mysql_client(mysql_engine):
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=mysql_engine)

    def _override():  # noqa: ANN202
        with factory() as s:
            yield s

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


class TestApiOnMySql:
    """每个只读接口都要能在 MySQL 上跑通并返回 200。

    ``/api/v1/campaigns`` 曾经在这里挂掉 —— 它用的 ``NULLS LAST``
    是 SQLite 特有能力，MySQL 8.0 不认。这个参数化用例是那个 bug 的回归防线。
    """

    @pytest.mark.parametrize("endpoint", READ_ENDPOINTS)
    def test_endpoint_returns_200(self, mysql_client, endpoint):
        resp = mysql_client.get(endpoint)
        assert resp.status_code == 200, f"{endpoint} -> {resp.status_code} {resp.text[:400]}"

    def test_health_reports_mysql(self, mysql_client):
        deps = {d["name"]: d for d in mysql_client.get("/health").json()["dependencies"]}
        assert deps["database"]["ok"] is True
        assert "MySQL" in deps["database"]["detail"]

    def test_paged_endpoints_have_data(self, mysql_client):
        """真 MySQL 应该已经灌过假数据，不该返回空列表。"""
        for endpoint in (
            "/api/v1/products?page_size=5",
            "/api/v1/tasks?page_size=5",
            "/api/v1/tickets?page_size=5",
            "/api/v1/campaigns?page_size=5",
        ):
            body = mysql_client.get(endpoint).json()
            assert body["total"] > 0, f"{endpoint} 查不到任何数据，是不是忘了 seed？"

    def test_aggregate_query_runs_on_mysql(self, mysql_client):
        """聚合里用了 ``func.date()`` / ``case()`` / outer join，方言差异高发区。

        这里直接调工具，确认 SQL 在 MySQL 上真能执行（不只是编译通过）。
        """
        from fashionops.gateway import (
            CallContext,
            IdempotencyStore,
            MemoryCircuitBreaker,
            MemoryRateLimiter,
            ToolGateway,
            ToolRegistry,
        )
        from fashionops.llm import MockModelClient  # noqa: F401  (导入即校验依赖可用)
        from fashionops.tools import AGENT_SCOPES, register_all_tools

        registry = ToolRegistry()
        register_all_tools(registry)
        gateway = ToolGateway(
            registry,
            rate_limiter=MemoryRateLimiter(),
            breaker=MemoryCircuitBreaker(),
            idempotency=IdempotencyStore(use_redis=False),
        )
        result = gateway.call(
            "erp.query_sales_summary",
            {"days": 30, "group_by": "day"},
            ctx=CallContext(actor="test", scopes=AGENT_SCOPES["analytics"]),
        )
        assert result.ok, result.error
        assert result.data["totals"]["gmv"] > 0
