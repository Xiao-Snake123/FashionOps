"""数据分析 Agent 测试。

分三层：

1. **聚合 SQL**（真实临时库）—— 退货率口径、outer join 不漏单、三种分组维度
2. **节点端到端** —— 真实 SQL + 真实网关 + mock 模型，跑完整节点
3. **产物的可用性** —— 把生成的 xlsx **读回来**验证，而不是只断言"返回了 URL"

第 3 层是这个文件里最有价值的部分。只断言 ``export.ok is True``
的话，一个生成了空工作簿、或者所有数字都变成 ``####`` 的实现照样能过。
"""

from __future__ import annotations

import io
from datetime import timedelta
from decimal import Decimal

import pytest

from fashionops.agents.analytics import (
    AnalyticsNarrative,
    MetricInsight,
    make_analytics_node,
)
from fashionops.agents.analytics_rules import (
    MIN_ORDERS_FOR_JUDGEMENT,
    Alert,
    detect_alerts,
)
from fashionops.agents.state import RunContext, new_state
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
from fashionops.llm import MockModelClient, MockReply
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


# ===========================================================================
# 3. 节点端到端
# ===========================================================================
def _narrative_reply() -> MockReply:
    """把预设结论转成一条脚本回复。

    ``MockReply`` 只有 ``content`` / ``tool_calls`` 两个字段（刻意不做成
    ``value``），所以要把 Pydantic 实例序列化成 JSON 字符串 ——
    这也正是真实模型返回的形态，测起来更接近实际。
    """
    return MockReply(
        content=AnalyticsNarrative(
            summary="近 30 天 GMV 1450 元，共 7 单，退货率 14.3%，整体健康。",
            insights=[
                MetricInsight(
                    dimension="抖音渠道",
                    observation="抖音 2 单里退款 1 单，退货率 50%，远高于整体 14.3%",
                    action="拉取抖音退款原因明细，先看是尺码问题还是描述不符",
                ),
                MetricInsight(
                    dimension="天猫渠道",
                    observation="天猫只有 240 元 GMV，占比 16.6%，明显低于淘宝",
                    action="确认天猫是否缺货导致曝光不足",
                ),
            ],
            data_quality_notes=["有 1 单关联商品已被删除，归入「未分类」"],
        ).model_dump_json()
    )


class TestAnalyticsNodeEndToEnd:
    def test_produces_report_artifact(self, seeded_env):
        ctx, _ = _build_context(model=MockModelClient(script=[_narrative_reply()]))
        state = new_state(task_no="T100", user_request="出一份经营分析报表")

        update = make_analytics_node(ctx)(state)

        assert update["steps"][0].status == "succeeded"
        assert update["steps"][0].node == "analytics"
        artifacts = update["artifacts"]
        assert len(artifacts) == 1
        assert artifacts[0].kind == "report"
        assert artifacts[0].produced_by == "analytics"
        assert artifacts[0].ref.startswith("AS")
        assert artifacts[0].name.endswith(".xlsx")

    def test_findings_are_written_back(self, seeded_env):
        ctx, _ = _build_context(model=MockModelClient(script=[_narrative_reply()]))
        update = make_analytics_node(ctx)(new_state(task_no="T101", user_request="报表"))
        findings = update["findings"]["analytics"]
        assert len(findings["insights"]) == 2
        assert findings["totals"]["gmv"] == pytest.approx(1450.0)
        assert findings["report"]["asset_no"].startswith("AS")
        assert findings["by_channel"]  # 渠道维度也写回了
        assert findings["alerts"] is not None

    def test_critical_count_comes_from_code_not_model(self, seeded_env):
        """关键：critical_count 由代码算，模型影响不了它。

        真实跑 glm-4-flash 时它把 14.69% 的退货率标成了 critical ——
        所以严重程度绝不能来自模型的自由文本。
        """
        ctx, _ = _build_context(model=MockModelClient(script=[_narrative_reply()]))
        update = make_analytics_node(ctx)(new_state(task_no="T101b", user_request="报表"))
        findings = update["findings"]["analytics"]

        # 临时库每个分组都只有几单 → 不足 20 单，退货率维度只报"样本不足"
        codes = {a["code"] for a in findings["alerts"]}
        assert "insufficient_sample" in codes
        assert "return_rate" not in codes
        # 但整体退款金额占 GMV 450/1450 = 31%，这个口径不受样本量限制 → critical
        criticals = [a for a in findings["alerts"] if a["level"] == "critical"]
        assert [a["code"] for a in criticals] == ["refund_ratio"]
        assert findings["critical_count"] == len(criticals) == 1
        # 而模型在脚本里写了「退货率 50%」也照样改不了这个数字
        assert any("50%" in i["observation"] for i in findings["insights"])

    def test_model_schema_has_no_severity_field(self):
        """模型不该被要求判断严重程度，所以 schema 里干脆不放这个字段。"""
        assert "severity" not in MetricInsight.model_fields
        assert "severity" not in AnalyticsNarrative.model_fields

    def test_generated_workbook_is_readable(self, seeded_env):
        """把 xlsx 读回来 —— 断言它真是一份能打开、有内容的报表。"""
        from openpyxl import load_workbook

        ctx, _ = _build_context(model=MockModelClient(script=[_narrative_reply()]))
        update = make_analytics_node(ctx)(new_state(task_no="T102", user_request="报表"))

        asset_no = update["artifacts"][0].ref
        key = update["artifacts"][0].meta["storage_key"]

        from fashionops.storage import get_storage

        payload = get_storage().read(key)
        assert len(payload) > 2000  # 空工作簿不会这么大

        wb = load_workbook(io.BytesIO(payload))
        assert set(wb.sheetnames) == {"类目汇总", "渠道汇总", "整体"}

        ws = wb["类目汇总"]
        assert ws["A1"].value  # 大标题
        # 表头在标题下方，逐行找到它，避免把布局写死在测试里
        header_row = next(
            r for r in range(1, 10) if ws.cell(row=r, column=1).value == "类目"
        )
        values = {
            ws.cell(row=r, column=1).value: ws.cell(row=r, column=2).value
            for r in range(header_row + 1, ws.max_row + 1)
        }
        assert values["女装"] == pytest.approx(820.0)  # 200+200+210+210
        assert values["（未分类）"] == pytest.approx(150.0)

        # 金额必须带千分位格式，否则运营在 Excel 里看到的是裸数字
        gmv_cell = ws.cell(row=header_row + 1, column=2)
        assert gmv_cell.number_format == "#,##0.00"
        assert asset_no  # 素材号非空

    def test_asset_row_is_registered(self, seeded_env):
        ctx, _ = _build_context(model=MockModelClient(script=[_narrative_reply()]))
        update = make_analytics_node(ctx)(new_state(task_no="T103", user_request="报表"))
        asset_no = update["artifacts"][0].ref

        from sqlalchemy.orm import Session

        from fashionops.db.base import get_engine
        from fashionops.db.models import Asset

        with Session(get_engine()) as session:
            asset = session.query(Asset).filter_by(asset_no=asset_no).one()
        assert asset.asset_type == "spreadsheet"
        assert asset.generator == "analytics"
        assert asset.size_bytes > 0

    def test_budget_is_accumulated(self, seeded_env):
        ctx, _ = _build_context(model=MockModelClient(script=[_narrative_reply()]))
        update = make_analytics_node(ctx)(new_state(task_no="T104", user_request="报表"))
        assert update["llm_calls"] == 1
        assert update["total_tokens"] > 0

    def test_report_export_is_idempotent_on_rerun(self, seeded_env):
        """节点重跑不能产生第二份同名报表。

        第二次调用时网关会命中幂等缓存，直接返回第一次的结果 ——
        所以不会有新的 assets 记录。
        """
        ctx, _ = _build_context(
            model=MockModelClient(script=[_narrative_reply(), _narrative_reply()])
        )
        node = make_analytics_node(ctx)
        state = new_state(task_no="T105", user_request="报表")

        first = node(state)
        second = node(state)

        assert first["artifacts"][0].ref == second["artifacts"][0].ref

        from sqlalchemy.orm import Session

        from fashionops.db.base import get_engine
        from fashionops.db.models import Asset

        with Session(get_engine()) as session:
            count = session.query(Asset).filter_by(generator="analytics").count()
        assert count == 1

    def test_tools_are_audited(self, seeded_env):
        ctx, audit = _build_context(model=MockModelClient(script=[_narrative_reply()]))
        make_analytics_node(ctx)(new_state(task_no="T106", user_request="报表"))
        names = {r.tool_name for r in audit.entries}
        assert "erp.query_sales_summary" in names
        assert "report.export_excel" in names

    def test_consumes_upstream_selection_compactly(self, seeded_env):
        """上游结论只取摘要字段，不带完整候选明细。

        这是长链路不爆上下文的关键 —— 直接塞整个 selection 结果的话，
        里面还有 top-10 候选的十几个字段，纯属浪费 token。
        """
        ctx, _ = _build_context(model=MockModelClient(script=[_narrative_reply()]))
        node = make_analytics_node(ctx)
        state = new_state(task_no="T107", user_request="报表")
        state["findings"] = {
            "selection": {
                "criteria": {"category": "女装", "count": 3},
                "passed": 7,
                "filtered_out": 2,
                "overall": "选中 3 款",
                # 下面这些字段不该被传给模型
                "top_items": [{"spu_code": f"S{i}", "reason": "x" * 200} for i in range(10)],
                "rejected_samples": [{"spu_code": "R", "detail": "y" * 200}],
            }
        }
        update = node(state)
        assert update["steps"][0].status == "succeeded"
        assert update["findings"]["analytics"]["totals"]["gmv"] > 0

    def test_scope_denial_is_recorded(self, seeded_env):
        """没有 ASSET_WRITE 权限时报表导出会被网关拒绝，但节点不崩。"""
        ctx, _ = _build_context(
            model=MockModelClient(script=[_narrative_reply()]),
            scopes=frozenset(s for s in AGENT_SCOPES["analytics"] if s != "asset:write"),
        )
        update = make_analytics_node(ctx)(new_state(task_no="T108", user_request="报表"))

        assert update["steps"][0].status == "succeeded"  # 结论仍然产出
        assert "artifacts" not in update
        assert update["findings"]["analytics"]["report"] is None
        assert update["errors"]

    def test_reproducible_across_runs(self, seeded_env):
        """同样输入跑两次，结论和数字完全一致。"""
        first = make_analytics_node(_build_context(model=MockModelClient(script=[_narrative_reply()]))[0])(
            new_state(task_no="T109a", user_request="报表")
        )
        second = make_analytics_node(_build_context(model=MockModelClient(script=[_narrative_reply()]))[0])(
            new_state(task_no="T109b", user_request="报表")
        )
        assert first["findings"]["analytics"]["totals"] == second["findings"]["analytics"]["totals"]
        assert (
            first["findings"]["analytics"]["summary"]
            == second["findings"]["analytics"]["summary"]
        )
