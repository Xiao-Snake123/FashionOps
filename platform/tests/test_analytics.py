"""数据分析 —— 聚合口径与告警规则的测试。

分两层：

1. **聚合 SQL**（真实临时库）—— 退货率口径、outer join 不漏单、三种分组维度
2. **告警规则是纯函数** —— 阈值、小样本不下结论、排序

## 原来的"节点端到端"去哪了

那组测试跑的是已删除的 `make_analytics_node`（它自己调模型写解读、
生成报表）。新架构里这一步拆成了工具调用 + Agent 自己写解读：

* 工具层（报表能生成、xlsx 能读回来）→ `tests/test_business_tools.py`
* 端到端（挂闸口、落库、续跑）→ `tests/test_loop.py`

`analytics_rules.py` **一行都没改**（交接文档明确要求），
所以它的断言原样保留 —— 阈值判定是这个项目"算数交给代码"那条线的样板。
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from fashionops.agents.analytics_rules import (
    MIN_ORDERS_FOR_JUDGEMENT,
    Alert,
    detect_alerts,
)
from fashionops.agents.context import RunContext
from fashionops.db.base import Base, build_engine, reset_engine, utcnow
from fashionops.db.models import SKU, Order, Product
from fashionops.gateway import (
    CallContext,
    IdempotencyStore,
    MemoryAuditSink,
    MemoryCircuitBreaker,
    MemoryRateLimiter,
    ToolGateway,
    ToolRegistry,
)
from fashionops.llm import MockModelClient
from fashionops.tools import AGENT_SCOPES, register_all_tools

# ===========================================================================
# 临时库
# ===========================================================================
#: (spu, 类目, 成本, 吊牌价)
_GOODS = [
    ("SPU0001", "女装", 60, 200),
    ("SPU0002", "女装", 70, 210),
    ("SPU0003", "鞋靴", 80, 240),
]

#: (订单号, spu, 渠道, 状态, 金额, 几天前)
_ORDERS = [
    ("SO-001", "SPU0001", "淘宝", "completed", "200.00", 3),
    ("SO-002", "SPU0001", "淘宝", "completed", "200.00", 4),
    ("SO-003", "SPU0001", "抖音", "completed", "180.00", 30),  # 窗口外，不该被统计
    ("SO-004", "SPU0002", "抖音", "refunded", "210.00", 6),
    ("SO-005", "SPU0002", "抖音", "completed", "210.00", 7),
    ("SO-006", "SPU0003", "淘宝", "refunding", "240.00", 8),
    ("SO-007", "SPU0003", "天猫", "completed", "240.00", 9),
    ("SO-008", "SPU0003", "天猫", "cancelled", "999.00", 10),  # 取消单，不该被统计
    ("SO-009", None, "淘宝", "completed", "150.00", 11),  # 商品已删 → sku_id 为空
]


@pytest.fixture
def seeded_env(tmp_path, monkeypatch):
    """临时库 + 指向临时目录的存储，两个外部依赖都自己造。"""
    import fashionops.storage as storage_mod
    from fashionops.config import Settings
    from fashionops.db import base as db_base

    settings = Settings(data_dir=tmp_path, db_host=None)
    reset_engine()
    monkeypatch.setattr(db_base, "get_settings", lambda: settings)
    monkeypatch.setattr(storage_mod, "get_settings", lambda: settings)
    storage_mod.reset_storage()

    engine = build_engine(settings)
    Base.metadata.create_all(engine)

    from sqlalchemy.orm import Session

    now = utcnow()
    with Session(engine) as session:
        skus: dict[str, int] = {}
        for spu, category, cost, price in _GOODS:
            product = Product(
                spu_code=spu,
                title=f"测试商品 {spu}",
                category=category,
                sub_category="连衣裙",
                season="秋季",
                cost_price=Decimal(cost),
                list_price=Decimal(price),
                rating=Decimal("4.6"),
                status="on_sale",
            )
            session.add(product)
            session.flush()
            sku = SKU(
                product_id=product.id,
                sku_code=f"{spu}-M",
                color="米白",
                size="M",
                cost_price=Decimal(cost),
                sale_price=Decimal(price),
            )
            session.add(sku)
            session.flush()
            skus[spu] = sku.id

        for no, spu, channel, status, amount, days_ago in _ORDERS:
            session.add(
                Order(
                    order_no=no,
                    sku_id=skus.get(spu) if spu else None,
                    buyer_nick=f"买家{no}",
                    channel=channel,
                    quantity=1,
                    unit_price=Decimal(amount),
                    total_amount=Decimal(amount),
                    actual_amount=Decimal(amount),
                    status=status,
                    paid_at=now - timedelta(days=days_ago),
                )
            )
        session.commit()

    yield settings
    reset_engine()
    storage_mod.reset_storage()


def _build_context(*, model=None, scopes=None):  # noqa: ANN001
    registry = ToolRegistry()
    register_all_tools(registry)
    audit = MemoryAuditSink()
    gateway = ToolGateway(
        registry,
        rate_limiter=MemoryRateLimiter(),
        breaker=MemoryCircuitBreaker(),
        audit_sink=audit,
        idempotency=IdempotencyStore(use_redis=False),
        rate_limit_per_minute=100,
    )
    ctx = RunContext(
        gateway=gateway,
        model=model or MockModelClient(),
        actor="analytics_agent",
        scopes=AGENT_SCOPES["analytics"] if scopes is None else scopes,
    )
    return ctx, audit


def _call(ctx: RunContext, tool: str, args: dict, scopes=None):  # noqa: ANN001
    return ctx.gateway.call(
        tool,
        args,
        # 注意不能用 ``scopes or ...`` —— 空的 frozenset 是 falsy，
        # 会静默变成"有全部权限"，而想测的恰恰是"没权限"
        ctx=CallContext(actor="test", scopes=AGENT_SCOPES["analytics"] if scopes is None else scopes),
    )


# ===========================================================================
# 1. 聚合 SQL 口径
# ===========================================================================
class TestSalesSummaryQuery:
    def test_returns_expected_totals(self, seeded_env):
        ctx, _ = _build_context()
        result = _call(ctx, "erp.query_sales_summary", {"days": 30, "group_by": "category"})
        assert result.ok, result.error

        totals = result.data["totals"]
        # 窗口内有效单：200 + 200 + 210 + 210 + 240 + 240 + 150 = 1450
        # 排除：30 天前的 SO-003、已取消的 SO-008
        assert totals["gmv"] == pytest.approx(1450.0)
        assert totals["order_count"] == 7

    def test_cancelled_orders_are_excluded(self, seeded_env):
        ctx, _ = _build_context()
        result = _call(ctx, "erp.query_sales_summary", {"days": 30, "group_by": "channel"})
        amounts = {g["group"]: g["gmv"] for g in result.data["groups"]}
        # SO-008 是 999 元的取消单，落在天猫渠道。如果没排除，天猫会远大于 240
        assert amounts["天猫"] == pytest.approx(240.0)

    def test_return_rate_uses_order_count_not_amount(self, seeded_env):
        """退货率按**订单数**算。

        抖音渠道：2 单，其中 SO-004 退款 → 退货率 50%。
        如果错按金额算：210 / (210+210) 也是 50%……所以这个用例特意选了
        金额和订单数不成比例的数据来区分口径 —— 见下一个用例。
        """
        ctx, _ = _build_context()
        result = _call(ctx, "erp.query_sales_summary", {"days": 30, "group_by": "channel"})
        dy = next(g for g in result.data["groups"] if g["group"] == "抖音")
        assert dy["order_count"] == 2
        assert dy["refund_count"] == 1
        assert dy["return_rate"] == pytest.approx(0.5)

    def test_category_grouping_survives_deleted_product(self, seeded_env):
        """商品被删后订单的 ``sku_id`` 会置空，outer join 才能保住这些单子。

        用 inner join 的话 SO-009（150 元）会凭空消失，
        而"总额对不上"这种问题几乎没人会立刻发现。
        """
        ctx, _ = _build_context()
        result = _call(ctx, "erp.query_sales_summary", {"days": 30, "group_by": "category"})
        groups = {g["group"]: g for g in result.data["groups"]}
        assert "（未分类）" in groups, f"缺失未分类分组：{list(groups)}"
        assert groups["（未分类）"]["gmv"] == pytest.approx(150.0)
        # 各分组之和 == 总额，说明没有漏单
        assert sum(g["gmv"] for g in result.data["groups"]) == pytest.approx(
            result.data["totals"]["gmv"]
        )

    def test_day_grouping_returns_iso_dates(self, seeded_env):
        ctx, _ = _build_context()
        result = _call(ctx, "erp.query_sales_summary", {"days": 30, "group_by": "day"})
        assert result.ok, result.error
        for g in result.data["groups"]:
            # func.date() 在 SQLite 上返回 'YYYY-MM-DD'
            assert g["group"][:4].isdigit() and len(g["group"]) >= 10

    def test_category_filter_restricts_rows(self, seeded_env):
        ctx, _ = _build_context()
        result = _call(
            ctx,
            "erp.query_sales_summary",
            {"days": 30, "group_by": "channel", "category": "女装"},
        )
        # 女装只有 SO-001/002/004/005 → 淘宝 400 + 抖音 420
        assert result.data["totals"]["gmv"] == pytest.approx(820.0)

    def test_avg_order_value_is_derived_from_gmv_and_count(self, seeded_env):
        ctx, _ = _build_context()
        result = _call(ctx, "erp.query_sales_summary", {"days": 30, "group_by": "category"})
        for g in result.data["groups"]:
            assert g["avg_order_value"] == pytest.approx(
                round(g["gmv"] / g["order_count"], 2), abs=0.01
            )

    def test_top_n_is_respected(self, seeded_env):
        ctx, _ = _build_context()
        result = _call(
            ctx, "erp.query_sales_summary", {"days": 30, "group_by": "channel", "top_n": 2}
        )
        assert result.data["count"] == 2
        # 必须按 GMV 降序，否则"取前 N"没有意义
        gmvs = [g["gmv"] for g in result.data["groups"]]
        assert gmvs == sorted(gmvs, reverse=True)

    def test_out_of_window_orders_are_excluded(self, seeded_env):
        ctx, _ = _build_context()
        short = _call(ctx, "erp.query_sales_summary", {"days": 30, "group_by": "channel"})
        long = _call(ctx, "erp.query_sales_summary", {"days": 60, "group_by": "channel"})
        assert long.data["totals"]["gmv"] > short.data["totals"]["gmv"]

    def test_requires_erp_scope(self, seeded_env):
        ctx, _ = _build_context()
        result = _call(ctx, "erp.query_sales_summary", {"days": 30}, scopes=frozenset())
        assert not result.ok


# ===========================================================================
# 2. 告警规则（纯函数）
# ===========================================================================
def _group(name, orders, refunds, gmv, **kw):  # noqa: ANN001
    return {
        "group": name,
        "order_count": orders,
        "refund_count": refunds,
        "gmv": gmv,
        "avg_order_value": round(gmv / orders, 2) if orders else 0.0,
        "refund_amount": kw.pop("refund_amount", 0.0),
        "return_rate": round(refunds / orders, 4) if orders else 0.0,
        **kw,
    }


class TestAlertRules:
    """告警判定是纯函数，不依赖模型 —— 这正是把它抽出来的理由。"""

    def test_high_return_rate_is_critical(self):
        alerts = detect_alerts(
            {"gmv": 10000, "avg_order_value": 100, "refund_amount": 0},
            [_group("女装", 100, 35, 10000)],
            [],
        )
        alert = next(a for a in alerts if a.code == "return_rate")
        assert alert.level == "critical"
        assert alert.value == pytest.approx(0.35)
        assert alert.threshold == pytest.approx(0.30)

    def test_medium_return_rate_is_warning(self):
        alerts = detect_alerts(
            {"gmv": 10000, "avg_order_value": 100, "refund_amount": 0},
            [_group("女装", 100, 25, 10000)],
            [],
        )
        alert = next(a for a in alerts if a.code == "return_rate")
        assert alert.level == "warning"

    def test_healthy_group_produces_no_return_rate_alert(self):
        alerts = detect_alerts(
            {"gmv": 10000, "avg_order_value": 100, "refund_amount": 0},
            [_group("女装", 100, 5, 10000)],
            [],
        )
        assert [a for a in alerts if a.code == "return_rate"] == []

    def test_small_sample_is_not_escalated(self):
        """小样本不下判定 —— 3 单退 1 单的 33% 只是噪声。

        真实数据里最长见的坑不是漏报，而是把噪声当信号：
        拿它去指挥业务，就是在用统计学上无意义的数字做决策。
        """
        alerts = detect_alerts(
            {"gmv": 1000, "avg_order_value": 100, "refund_amount": 0},
            [_group("童装", 3, 1, 1000)],
            [],
        )
        codes = {a.code for a in alerts}
        assert "return_rate" not in codes
        sample = next(a for a in alerts if a.code == "insufficient_sample")
        assert sample.level == "info"
        assert str(MIN_ORDERS_FOR_JUDGEMENT) in sample.message

    def test_stable_sort_critical_first(self):
        alerts = detect_alerts(
            {"gmv": 10000, "avg_order_value": 100, "refund_amount": 0},
            [_group("女装", 100, 35, 6000), _group("男装", 100, 25, 4000)],
            [],
        )
        levels = [a.level for a in alerts]
        assert levels == sorted(levels, key=lambda x: {"critical": 0, "warning": 1, "info": 2}[x])

    def test_channel_concentration_warns(self):
        alerts = detect_alerts(
            {"gmv": 10000, "avg_order_value": 100, "refund_amount": 0},
            [],
            [_group("直播", 70, 5, 8000), _group("自然流量", 20, 1, 2000)],
        )
        concentration = next(a for a in alerts if a.code == "channel_concentration")
        assert concentration.dimension == "直播"
        assert concentration.value == pytest.approx(0.8)

    def test_aov_gap_is_flagged(self):
        alerts = detect_alerts(
            {"gmv": 10000, "avg_order_value": 100, "refund_amount": 0},
            [_group("童装", 100, 0, 5000)],  # 客单价 50，比整体低 50%
            [],
        )
        gap = next(a for a in alerts if a.code == "aov_gap")
        assert gap.level == "warning"
        assert "50.00" in gap.message

    def test_refund_ratio_on_totals(self):
        alerts = detect_alerts(
            {"gmv": 10000, "avg_order_value": 100, "refund_amount": 2000},
            [],
            [],
        )
        ratio = next(a for a in alerts if a.code == "refund_ratio")
        assert ratio.value == pytest.approx(0.2)

    def test_zero_gmv_does_not_divide_by_zero(self):
        assert detect_alerts({"gmv": 0, "avg_order_value": 0, "refund_amount": 0}, [], []) == []

    def test_alert_serializes_for_prompt(self):
        alert = Alert(
            code="return_rate",
            level="critical",
            dimension="女装",
            metric="退货率",
            value=0.35,
            threshold=0.30,
            message="x",
        )
        assert alert.model_dump()["level"] == "critical"

