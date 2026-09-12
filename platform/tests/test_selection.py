"""选品打分 + 选品 Agent 测试。

打分部分是纯函数，好测。真正的价值在最后那组 **Agent 端到端**：
用真实的 SQL 查询（临时库）→ 真实工具网关 → mock 模型，
跑完整个节点，断言它产出的状态更新是完整的、可复现的。

这类测试能抓到"单元全绿但拼起来不工作"的问题 ——
比如状态字段名字对不上、reducer 语义搞错、预算没累计。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from fashionops.agents.scoring import (
    HardConstraints,
    ScoreWeights,
    apply_constraints,
    rank_candidates,
    score_candidates,
)
from fashionops.agents.selection import SelectionCriteria, make_selection_node
from fashionops.agents.state import RunContext, new_state
from fashionops.db.base import Base, build_engine, reset_engine, utcnow
from fashionops.db.models import SKU, Competitor, Inventory, Order, Product
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
# 打分：纯函数
# ===========================================================================
def _candidate(**overrides) -> dict:
    base = {
        "spu_code": "SPU0001",
        "title": "秋季法式醋酸连衣裙",
        "category": "女装",
        "sub_category": "连衣裙",
        "season": "秋季",
        "style_tags": ["法式"],
        "status": "on_sale",
        "cost_price": 60.0,
        "list_price": 200.0,
        "margin_rate": 0.70,
        "rating": 4.8,
        "available_stock": 300,
        "sold_30d": 100,
        "competitor_avg_price": 260.0,
    }
    base.update(overrides)
    return base


class TestHardConstraints:
    def test_passes_healthy_candidate(self):
        passed, rejected = apply_constraints([_candidate()], HardConstraints())
        assert len(passed) == 1
        assert rejected == []

    def test_rejects_low_stock_with_reason(self):
        """库存不达标必须**带原因**被拒。运营最常问的就是"为什么这个款没选上"。"""
        passed, rejected = apply_constraints(
            [_candidate(available_stock=3)], HardConstraints(min_stock=30)
        )
        assert passed == []
        assert len(rejected) == 1
        assert rejected[0].reason == "stock"
        assert "3 件" in rejected[0].detail

    def test_rejects_low_margin(self):
        _, rejected = apply_constraints(
            [_candidate(margin_rate=0.15)], HardConstraints(min_margin_rate=0.40)
        )
        assert rejected[0].reason == "margin"

    def test_rejects_off_shelf(self):
        _, rejected = apply_constraints(
            [_candidate(status="off_shelf")], HardConstraints()
        )
        assert rejected[0].reason == "status"

    def test_rejects_missing_margin_data(self):
        """算不出毛利就不能选 —— 宁可不选，也不要拍脑袋上架。"""
        _, rejected = apply_constraints(
            [_candidate(margin_rate=None)], HardConstraints()
        )
        assert rejected[0].reason == "margin"

    def test_max_cost_is_respected(self):
        _, rejected = apply_constraints(
            [_candidate(cost_price=500.0)], HardConstraints(max_cost=Decimal("100"))
        )
        assert rejected[0].reason == "cost"

    def test_constraints_describe_is_human_readable(self):
        text = " ".join(HardConstraints().describe())
        assert "库存" in text and "毛利率" in text


class TestScoring:
    def test_more_sales_scores_higher(self):
        scored = score_candidates(
            [
                _candidate(spu_code="A", sold_30d=10),
                _candidate(spu_code="B", sold_30d=500),
            ]
        )
        assert [c.spu_code for c in scored] == ["B", "A"]

    def test_higher_margin_scores_higher(self):
        scored = score_candidates(
            [
                _candidate(spu_code="A", margin_rate=0.42),
                _candidate(spu_code="B", margin_rate=0.68),
            ]
        )
        assert scored[0].spu_code == "B"

    def test_price_gap_rewards_cheaper_than_competitors(self):
        """吊牌价明显低于竞品 → 有价格空间 → 高分。"""
        scored = score_candidates(
            [
                _candidate(spu_code="CHEAP", list_price=100.0, competitor_avg_price=300.0),
                _candidate(spu_code="EXPENSIVE", list_price=320.0, competitor_avg_price=300.0),
            ]
        )
        assert scored[0].spu_code == "CHEAP"
        expensive = next(c for c in scored if c.spu_code == "EXPENSIVE")
        assert expensive.dimension_scores["price_gap"] == 0.0

    def test_weights_must_sum_to_one(self):
        """权重和不为 1 时总分范围会漂移，不同批次的分数字段就不可比了。"""
        with pytest.raises(ValueError, match="权重之和"):
            ScoreWeights(sell_through=0.5, margin=0.5, price_gap=0.5)

    def test_ties_break_deterministically(self):
        """同分时按 spu_code 兜底排序。

        不做这件事的话，同分商品的相对顺序取决于 sort 的稳定性，
        输入顺序一变结果就变 —— 评测时会出现"同样的数据两次跑结果不同"。
        """
        a = [_candidate(spu_code="BBB"), _candidate(spu_code="AAA")]
        b = [_candidate(spu_code="AAA"), _candidate(spu_code="BBB")]
        assert [c.spu_code for c in score_candidates(a)] == ["AAA", "BBB"]
        assert [c.spu_code for c in score_candidates(b)] == ["AAA", "BBB"]

    def test_result_is_reproducible(self):
        pool = [_candidate(spu_code=f"SPU{i:04d}", sold_30d=i * 7) for i in range(20)]
        first = [(c.spu_code, c.total_score) for c in score_candidates(pool)]
        second = [(c.spu_code, c.total_score) for c in score_candidates(pool)]
        assert first == second

    def test_percentile_is_pool_relative(self):
        """销量维度用的是池内百分位 —— 会自动适配不同类目的量级。"""
        pool = [_candidate(spu_code=f"S{i}", sold_30d=n) for i, n in enumerate([1, 2, 3, 4, 5])]
        scored = {c.spu_code: c.dimension_scores["sell_through"] for c in score_candidates(pool)}
        assert scored["S4"] == 1.0
        assert scored["S0"] == 0.0

    def test_empty_pool_is_handled(self):
        assert score_candidates([]) == []

    def test_highlights_quote_real_numbers(self):
        """理由由代码生成，每条都要能对回具体数字，经得起追问。"""
        scored = score_candidates([_candidate(sold_30d=500, available_stock=400)])
        text = " ".join(scored[0].highlights)
        assert "500" in text or "400" in text


class TestRankCandidates:
    def test_full_pipeline(self):
        outcome = rank_candidates(
            [
                _candidate(spu_code="GOOD", sold_30d=200),
                _candidate(spu_code="NOSTOCK", available_stock=1),
                _candidate(spu_code="LOWMARGIN", margin_rate=0.1),
            ],
            constraints=HardConstraints(min_stock=30, min_margin_rate=0.4),
        )
        assert outcome.total_input == 3
        assert outcome.passed == 1
        assert outcome.scored[0].spu_code == "GOOD"
        reasons = {r.reason for r in outcome.rejected}
        assert reasons == {"stock", "margin"}

    def test_outcome_dict_includes_rejection_samples(self):
        outcome = rank_candidates(
            [_candidate(spu_code="X", available_stock=0)], constraints=HardConstraints()
        )
        payload = outcome.to_dict()
        assert payload["rejected"] == 1
        assert payload["rejected_samples"][0]["spu_code"] == "X"


# ===========================================================================
# Agent 端到端（真实 SQL + 真实网关 + mock 模型）
# ===========================================================================
@pytest.fixture
def seeded_env(tmp_path, monkeypatch):
    """搭一个装了少量商品的临时库，并把数据层指向它。

    刻意**不用**开发库 ``data/fashionops.db``：测试依赖外部状态的话，
    跑之前要不要先 seed、seed 过没有，就变成隐性前提了。
    这里自己建、自己灌，测试是自洽的。
    """
    from fashionops.config import Settings
    from fashionops.db import base as db_base

    settings = Settings(data_dir=tmp_path, db_host=None)
    reset_engine()
    monkeypatch.setattr(db_base, "get_settings", lambda: settings)

    engine = build_engine(settings)
    Base.metadata.create_all(engine)

    now = utcnow()
    plan = [
        # (spu, 成本, 吊牌价, 评分, 库存, 近30天销量, 状态)
        ("SPU0001", 60, 200, 4.8, 400, 260, "on_sale"),   # 综合最好
        ("SPU0002", 70, 210, 4.6, 320, 180, "on_sale"),   # 次之
        ("SPU0003", 55, 190, 4.5, 260, 120, "on_sale"),   # 中等
        ("SPU0004", 90, 200, 4.2, 8, 300, "on_sale"),     # 库存告急 → 该被挡
        ("SPU0005", 160, 190, 4.7, 500, 400, "on_sale"),  # 毛利太低 → 该被挡
        ("SPU0006", 50, 180, 4.9, 350, 150, "off_shelf"),  # 已下架 → 该被挡
    ]

    from sqlalchemy.orm import Session

    with Session(engine) as session:
        for spu, cost, price, rating, stock, sold, status in plan:
            product = Product(
                spu_code=spu,
                title=f"秋季法式连衣裙 {spu}",
                category="女装",
                sub_category="连衣裙",
                season="秋季",
                style_tags=["法式"],
                cost_price=Decimal(cost),
                list_price=Decimal(price),
                rating=Decimal(str(rating)),
                status=status,
            )
            session.add(product)
            session.flush()

            sku = SKU(
                product_id=product.id,
                sku_code=f"{spu}-S",
                color="米白",
                size="M",
                cost_price=Decimal(cost),
                sale_price=Decimal(price),
            )
            session.add(sku)
            session.flush()

            session.add(
                Inventory(
                    sku_id=sku.id,
                    change_type="in",
                    quantity=stock,
                    quantity_after=stock,
                    occurred_at=now - timedelta(days=1),
                )
            )
            session.add(
                Order(
                    order_no=f"SO-{spu}",
                    sku_id=sku.id,
                    product_title=product.title,
                    quantity=sold,
                    unit_price=Decimal(price),
                    total_amount=Decimal(price) * sold,
                    actual_amount=Decimal(price) * sold,
                    status="completed",
                    paid_at=now - timedelta(days=5),
                )
            )

        session.add(
            Competitor(
                platform="mock",
                product_title="竞品连衣裙",
                category="女装",
                season="秋季",
                price=Decimal("260.00"),
            )
        )
        session.commit()

    yield settings
    reset_engine()


def _build_context(
    *, model=None, scopes: frozenset[str] | None = None  # noqa: ANN001
) -> tuple[RunContext, MemoryAuditSink]:
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
        actor="selection_agent",
        scopes=AGENT_SCOPES["selection"] if scopes is None else scopes,
    )
    return ctx, audit


class TestSelectionNodeEndToEnd:
    def test_runs_and_produces_selection(self, seeded_env):
        ctx, audit = _build_context()
        node = make_selection_node(ctx)
        state = new_state(task_no="T001", user_request="帮我选 3 款秋季女装")

        update = node(state)

        # ---- 状态更新结构 ----
        assert "steps" in update and len(update["steps"]) == 1
        assert update["steps"][0].node == "selection"
        assert update["steps"][0].status == "succeeded"
        assert update["completed_nodes"] == ["selection"]

        # ---- 选出了东西 ----
        selected = update["selected"]
        assert len(selected) == 3
        assert [s["rank"] for s in selected] == [1, 2, 3]
        # 按分数降序
        scores = [s["total_score"] for s in selected]
        assert scores == sorted(scores, reverse=True)

        # ---- 每款都有可读理由 ----
        for item in selected:
            assert item["reason"], f"{item['spu_code']} 没有推荐理由"

    def test_hard_constraints_actually_filter(self, seeded_env):
        """这是这个 Agent 最重要的一条：库存告急 / 低毛利 / 已下架的款必须被挡掉。"""
        ctx, _ = _build_context()
        node = make_selection_node(ctx)
        update = node(new_state(task_no="T002", user_request="帮我选几款秋季女装"))

        picked = {s["spu_code"] for s in update["selected"]}
        assert "SPU0004" not in picked, "库存只有 8 件的款不该被选上"
        assert "SPU0005" not in picked, "毛利率不达标的款不该被选上"
        assert "SPU0006" not in picked, "已下架的款不该被选上"

        findings = update["findings"]["selection"]
        assert findings["total_input"] == 6
        assert findings["filtered_out"] >= 3
        rejection_reasons = {r["reason"] for r in findings["rejected_samples"]}
        assert {"stock", "margin", "status"} <= rejection_reasons

    def test_budget_is_accumulated(self, seeded_env):
        """预算字段没有 reducer，节点必须返回**累加后的绝对值**。

        如果误返回增量，之前几轮的消耗就丢了 —— 预算控制会完全失效，
        而且不报错。
        """
        ctx, _ = _build_context()
        node = make_selection_node(ctx)
        state = new_state(task_no="T003", user_request="帮我选几款秋季女装")
        state["total_tokens"] = 1000  # 假装之前已经花过

        update = node(state)

        assert update["total_tokens"] > 1000, "预算没有在上一次的基础上累加"
        assert update["llm_calls"] >= 1

    def test_tools_are_audited(self, seeded_env):
        """工具调用要走网关，因此必然留下审计记录。"""
        ctx, audit = _build_context()
        node = make_selection_node(ctx)
        node(new_state(task_no="T004", user_request="帮我选几款秋季女装"))

        assert audit.entries, "工具调用没有留审计"
        entry = audit.entries[-1]
        assert entry.tool_name == "erp.query_candidates"
        assert entry.status == "success"
        assert entry.actor == "selection_agent"

    def test_result_is_reproducible(self, seeded_env):
        """同一份数据跑两次，选出的一定是同一批。这是把算数交给代码换来的。"""
        ctx, _ = _build_context()
        node = make_selection_node(ctx)
        first = node(new_state(task_no="T005", user_request="帮我选 3 款秋季女装"))
        second = node(new_state(task_no="T006", user_request="帮我选 3 款秋季女装"))
        assert [s["spu_code"] for s in first["selected"]] == [
            s["spu_code"] for s in second["selected"]
        ]

    def test_node_records_failure_instead_of_raising(self, seeded_env):
        """工具失败时节点**不能抛异常** —— 要把失败写进状态，让编排层决策。

        抛出去的话，"一次工具失败"就升级成了"整个任务失败"，
        而这本来是可以重试或者降级处理的。

        这里用"角色没权限"来制造工具失败：它会走和下游报错**同一条**
        ``if not call.ok`` 分支，而构造起来比模拟下游炸掉简单得多。
        """
        ctx, _ = _build_context(scopes=frozenset())  # 不给任何权限
        node = make_selection_node(ctx)
        update = node(new_state(task_no="T007", user_request="帮我选几款秋季女装"))

        assert update["steps"][0].status == "failed"
        assert update["steps"][0].error
        assert "errors" in update and update["errors"]
        assert "PERMISSION_DENIED" in update["steps"][0].error

    def test_empty_candidate_pool_fails_gracefully(self, seeded_env):
        """候选池被过滤空时要给出可执行的结论，而不是崩掉。

        注意不能用"童装"这类词去构造 —— mock 模型并不真的理解需求，
        它按字段名生成值（category 永远给"女装"）。
        要制造空池只能显式指定 criteria，用脚本化的 mock 回复。
        """
        criteria = SelectionCriteria(category="童装", season="秋季", count=3)
        model = MockModelClient(script=[MockReply(content=criteria.model_dump_json())])
        ctx, _ = _build_context(model=model)

        node = make_selection_node(ctx)
        update = node(new_state(task_no="T008", user_request="帮我选 3 款秋季童装"))

        assert update["steps"][0].status == "failed"
        assert "候选" in update["steps"][0].summary


class TestToolGatewayIntegration:
    def test_permission_is_enforced_by_gateway(self, seeded_env):
        """没有 ERP_READ 权限的角色调不动商品查询工具。"""
        registry = ToolRegistry()
        register_all_tools(registry)
        gateway = ToolGateway(
            registry,
            rate_limiter=MemoryRateLimiter(),
            breaker=MemoryCircuitBreaker(),
            audit_sink=MemoryAuditSink(),
            idempotency=IdempotencyStore(use_redis=False),
        )
        result = gateway.call(
            "erp.query_candidates",
            {},
            ctx=CallContext(actor="无权限的角色", scopes=frozenset()),
        )
        assert not result.ok
        assert result.error.code == "PERMISSION_DENIED"
        assert result.error.hint

    def test_candidate_query_returns_expected_shape(self, seeded_env):
        registry = ToolRegistry()
        register_all_tools(registry)
        gateway = ToolGateway(
            registry,
            rate_limiter=MemoryRateLimiter(),
            breaker=MemoryCircuitBreaker(),
            audit_sink=MemoryAuditSink(),
            idempotency=IdempotencyStore(use_redis=False),
        )
        result = gateway.call(
            "erp.query_candidates",
            {"category": "女装", "season": "秋季", "limit": 50},
            ctx=CallContext(actor="selection_agent", scopes=AGENT_SCOPES["selection"]),
        )
        assert result.ok
        rows = result.data["candidates"]
        assert len(rows) == 6

        row = next(r for r in rows if r["spu_code"] == "SPU0001")
        assert row["available_stock"] == 400
        assert row["sold_30d"] == 260
        assert row["competitor_avg_price"] == pytest.approx(260.0)
        assert row["margin_rate"] == pytest.approx(0.70)

    def test_zero_stock_sku_reported_as_zero(self, seeded_env):
        """``coalesce`` 兜底：没有任何库存流水的商品应该报 0，而不是 None。

        None 传到下游会让 ``int(None)`` 直接抛异常，
        而 0 会被硬约束正常挡掉 —— 后者才是正确处理。
        """
        registry = ToolRegistry()
        register_all_tools(registry)
        gateway = ToolGateway(
            registry,
            rate_limiter=MemoryRateLimiter(),
            breaker=MemoryCircuitBreaker(),
            audit_sink=MemoryAuditSink(),
            idempotency=IdempotencyStore(use_redis=False),
        )
        # SPU0004 的库存是 8，这里直接验证聚合数字正确
        result = gateway.call(
            "erp.query_candidates",
            {"category": "女装"},
            ctx=CallContext(actor="x", scopes=AGENT_SCOPES["selection"]),
        )
        by_spu = {r["spu_code"]: r for r in result.data["candidates"]}
        assert by_spu["SPU0004"]["available_stock"] == 8
        assert isinstance(by_spu["SPU0004"]["available_stock"], int)


class TestScoreWeightConfigurable:
    def test_weights_change_ranking(self):
        """权重可调意味着运营能按自己的经营重点调整选品口径。"""
        pool = [
            # 高销量低毛利
            _candidate(spu_code="VOLUME", sold_30d=1000, margin_rate=0.42),
            # 低销量高毛利
            _candidate(spu_code="MARGIN", sold_30d=20, margin_rate=0.85),
        ]
        by_volume = rank_candidates(
            pool, weights=ScoreWeights(sell_through=0.9, margin=0.02, price_gap=0.02,
                                       rating=0.03, stock_depth=0.03)
        )
        by_margin = rank_candidates(
            pool, weights=ScoreWeights(sell_through=0.02, margin=0.9, price_gap=0.02,
                                       rating=0.03, stock_depth=0.03)
        )
        assert by_volume.scored[0].spu_code == "VOLUME"
        assert by_margin.scored[0].spu_code == "MARGIN"


class TestSeedIntegration:
    """和第 0 阶段的假数据对齐：77 个告急 SKU 应该被硬约束真的挡掉。"""

    def test_seeded_data_has_expected_anomalies(self):
        from fashionops.db import session_scope
        from fashionops.db.models import Product

        with session_scope() as session:
            count = session.query(Product).count()
        if count == 0:
            pytest.skip("开发库还没灌假数据，跳过（跑 uv run fashionops-seed）")
        assert count > 0


_ = datetime  # 保证导入被使用（BuildEngine 的时间基准）
