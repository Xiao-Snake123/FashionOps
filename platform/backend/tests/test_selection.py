"""选品打分 —— **算数部分**的测试。

打分是纯函数（`agents/scoring.py`），所以这里全部是"给输入、断言输出"，
不连库、不调模型：可复现是这一层的全部价值，测试也必须照这个来写。

## 原来这里还有一组"选品 Agent 端到端"

它跑的是已被删除的 `make_selection_node` + TaskState。新架构里
"拉候选 → 打分"变成两次工具调用（`erp.query_candidates` →
`selection.rank_candidates`），所以那组用例被这两处覆盖取代：

* `tests/test_business_tools.py` —— 工具这一层（入参契约、过网关、返回形状）
* `tests/test_loop.py` —— 端到端（规划 → 裁目录 → 执行 → 落库）

`scoring.py` 本身**一行都没改**（交接文档明确要求），所以它的断言原样保留。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from fashionops.agents.scoring import (
    HardConstraints,
    ScoreWeights,
    apply_constraints,
    rank_candidates,
    score_candidates,
)


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

