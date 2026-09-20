"""选品打分：**确定性的多维度评分 + 硬约束过滤**。

## 核心设计：算数不交给模型

很多人做 Agent 会把"给这些商品打分排序"整个丢给 LLM。这个做法有三个问题：

1. **不可复现** —— 同样的输入跑两次得到不同的排序，没法做评测
2. **不会算数** —— 让模型比大小、算百分比，它经常算错，而且是静默地错
3. **贵** —— 把 50 个商品的全部字段塞进上下文，token 成本高得多

所以这里的分工是：

* **代码负责**：硬约束过滤（过就是过、不过就是不过）、归一化、加权求和、排序
* **模型负责**：理解"我想要什么样的款"、以及给最终结果写**理由**

模型不碰数字，出来的是可复现、可审计、可评测的结果。
"选品理由"这种自然语言产出交回给模型，人读起来才自然。

## 归一化为什么混用两种方式

* 比率型指标（毛利率、评分、库存深度）用**绝对值**归一化 ——
  它们有天然的上限和含义，0.5 的毛利率就是 0.5，不该因为池子里都是高毛利品就贬值
* 数量型指标（近 30 天销量）用**池内百分位** —— 它没有绝对标准，
  女装类目一天卖 20 件可能算好，配饰类目卖 20 件是爆款。
  百分位能自动适配不同类目，不用给每个类目单独配阈值
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HardConstraints:
    """硬约束：不满足就**直接出局**，不参与打分。

    这是"约束求解"的思路，不是"扣分"。区别很重要：
    扣分制下，一个库存只有 3 件的款可能因为其他维度分数高而排到前面，
    然后投放出去、卖爆了、发不出货 —— 这是真实会发生的业务事故。
    硬约束把它一刀切掉，不给任何翻身机会。
    """

    #: 可用库存低于这个数就不考虑 —— 再好的款没货也白搭
    min_stock: int = 30
    #: 毛利率下限
    min_margin_rate: float = 0.40
    #: 成本上限（元）。None 表示不限
    max_cost: Decimal | None = None
    #: 这些状态直接排除
    exclude_statuses: tuple[str, ...] = ("off_shelf",)
    #: 评分下限。None 表示不限
    min_rating: float | None = None

    def describe(self) -> list[str]:
        """给前端和日志看的可读描述。"""
        items = [
            f"可用库存 ≥ {self.min_stock} 件",
            f"毛利率 ≥ {self.min_margin_rate:.0%}",
        ]
        if self.max_cost is not None:
            items.append(f"成本 ≤ ¥{self.max_cost}")
        if self.min_rating is not None:
            items.append(f"评分 ≥ {self.min_rating}")
        if self.exclude_statuses:
            items.append(f"排除状态：{'/'.join(self.exclude_statuses)}")
        return items


@dataclass(frozen=True)
class ScoreWeights:
    """各维度权重。和必须为 1.0（构造时会校验）。"""

    sell_through: float = 0.30
    margin: float = 0.25
    price_gap: float = 0.20
    rating: float = 0.15
    stock_depth: float = 0.10

    def __post_init__(self) -> None:
        total = (
            self.sell_through + self.margin + self.price_gap + self.rating + self.stock_depth
        )
        if abs(total - 1.0) > 1e-6:
            # 权重和不为 1 时总分范围会漂移，排序还能用但分数没有可比性。
            # 明确报错比"默默接受"好 —— 后者会让不同批次的分数字段对不上。
            raise ValueError(f"权重之和必须为 1.0，当前为 {total}")

    def as_dict(self) -> dict[str, float]:
        return {
            "sell_through": self.sell_through,
            "margin": self.margin,
            "price_gap": self.price_gap,
            "rating": self.rating,
            "stock_depth": self.stock_depth,
        }


#: 归一化时用到的参考值。改这里就能调"要多好才算满分"。
TARGET_MARGIN_RATE = 0.70  # 毛利率到 70% 算满分
TARGET_STOCK = 300  # 库存到 300 件算满分
RATING_FLOOR = 4.0  # 评分 4.0 以下是 0 分
RATING_CEIL = 5.0


# ---------------------------------------------------------------------------
# 结果对象
# ---------------------------------------------------------------------------
@dataclass
class Rejection:
    """被硬约束挡掉的候选。

    **必须记录原因**。不记的话，"为什么这个款没被选上"就永远答不出来，
    而这是运营最常问的问题。演示时也正是这些拒绝记录
    说明"库存告急的款真的被挡住了"。
    """

    spu_code: str
    title: str
    reason: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "spu_code": self.spu_code,
            "title": self.title,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass
class ScoredCandidate:
    """通过硬约束、并完成打分的候选。"""

    spu_code: str
    title: str
    category: str | None
    sub_category: str | None = None
    season: str | None = None
    style_tags: list[str] = field(default_factory=list)

    cost_price: float | None = None
    list_price: float | None = None
    margin_rate: float | None = None
    rating: float | None = None
    available_stock: int = 0
    sold_30d: int = 0
    competitor_avg_price: float | None = None

    #: 各维度归一化到 0~1 的分数
    dimension_scores: dict[str, float] = field(default_factory=dict)
    total_score: float = 0.0
    #: 人类可读的加分理由，由代码生成（不靠模型编）
    highlights: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "spu_code": self.spu_code,
            "title": self.title,
            "category": self.category,
            "sub_category": self.sub_category,
            "season": self.season,
            "style_tags": self.style_tags,
            "cost_price": self.cost_price,
            "list_price": self.list_price,
            "margin_rate": self.margin_rate,
            "rating": self.rating,
            "available_stock": self.available_stock,
            "sold_30d": self.sold_30d,
            "competitor_avg_price": self.competitor_avg_price,
            "dimension_scores": {k: round(v, 4) for k, v in self.dimension_scores.items()},
            "total_score": round(self.total_score, 2),
            "highlights": self.highlights,
        }


@dataclass
class SelectionOutcome:
    """一轮选品的完整结果。

    带上 ``rejected`` 和 ``constraints`` 是有意的：
    结果不只要给出"选了谁"，还要能回答"为什么没选另一些"。
    """

    scored: list[ScoredCandidate] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    constraints: HardConstraints = field(default_factory=HardConstraints)
    weights: ScoreWeights = field(default_factory=ScoreWeights)
    total_input: int = 0

    @property
    def passed(self) -> int:
        return len(self.scored)

    def top(self, n: int) -> list[ScoredCandidate]:
        return self.scored[:n]

    def to_dict(self, *, top_n: int | None = None) -> dict[str, Any]:
        picked = self.scored[:top_n] if top_n else self.scored
        return {
            "total_input": self.total_input,
            "passed": self.passed,
            "rejected": len(self.rejected),
            "constraints": self.constraints.describe(),
            "weights": self.weights.as_dict(),
            "selected": [c.to_dict() for c in picked],
            "rejected_samples": [r.to_dict() for r in self.rejected[:5]],
        }


# ---------------------------------------------------------------------------
# 硬约束
# ---------------------------------------------------------------------------
def check_constraints(
    candidate: Mapping[str, Any], constraints: HardConstraints
) -> Rejection | None:
    """返回 ``None`` 表示通过；返回 ``Rejection`` 表示被挡掉。"""
    spu = str(candidate.get("spu_code") or "?")
    title = str(candidate.get("title") or "")

    if candidate.get("status") in constraints.exclude_statuses:
        return Rejection(spu, title, "status", f"状态为 {candidate.get('status')}，不可上架")

    stock = int(candidate.get("available_stock") or 0)
    if stock < constraints.min_stock:
        return Rejection(
            spu, title, "stock",
            f"可用库存 {stock} 件，低于下限 {constraints.min_stock} 件（卖出去发不了货）",
        )

    margin = candidate.get("margin_rate")
    if margin is None:
        return Rejection(spu, title, "margin", "缺少成本或吊牌价，无法计算毛利")
    if float(margin) < constraints.min_margin_rate:
        return Rejection(
            spu, title, "margin",
            f"毛利率 {float(margin):.1%}，低于下限 {constraints.min_margin_rate:.0%}",
        )

    if constraints.max_cost is not None:
        cost = candidate.get("cost_price")
        if cost is None or Decimal(str(cost)) > constraints.max_cost:
            return Rejection(
                spu, title, "cost",
                f"成本 ¥{cost} 超过上限 ¥{constraints.max_cost}",
            )

    if constraints.min_rating is not None:
        rating = candidate.get("rating")
        if rating is None or float(rating) < constraints.min_rating:
            return Rejection(
                spu, title, "rating",
                f"评分 {rating} 低于下限 {constraints.min_rating}",
            )

    return None


def apply_constraints(
    candidates: list[Mapping[str, Any]], constraints: HardConstraints
) -> tuple[list[Mapping[str, Any]], list[Rejection]]:
    passed: list[Mapping[str, Any]] = []
    rejected: list[Rejection] = []
    for candidate in candidates:
        rejection = check_constraints(candidate, constraints)
        if rejection is None:
            passed.append(candidate)
        else:
            rejected.append(rejection)
    return passed, rejected


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------
def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _percentile_ranks(values: list[float]) -> list[float]:
    """把一组值转成 0~1 的百分位排名。

    并列的取平均名次 —— 否则两个一模一样的商品会拿到不同的分数，
    排序结果依赖输入顺序，不可复现。

    只有一个元素时返回 1.0（它既是最好也是唯一）。
    """
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [1.0]

    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        # 名次从 0 开始；并列的用区间中点
        average_rank = (i + j) / 2
        score = average_rank / (n - 1)
        for k in range(i, j + 1):
            ranks[order[k]] = score
        i = j + 1
    return ranks


# ---------------------------------------------------------------------------
# 打分
# ---------------------------------------------------------------------------
def score_candidates(
    candidates: list[Mapping[str, Any]],
    *,
    weights: ScoreWeights | None = None,
) -> list[ScoredCandidate]:
    """给通过硬约束的候选打分并排序（降序）。

    **必须在整个候选池上一起算**，不能一个一个算 ——
    销量的百分位是池内相对的，单独算一个候选的百分位没有意义。
    """
    weights = weights or ScoreWeights()
    if not candidates:
        return []

    sold = [float(c.get("sold_30d") or 0) for c in candidates]
    sold_ranks = _percentile_ranks(sold)

    scored: list[ScoredCandidate] = []
    for candidate, sell_rank in zip(candidates, sold_ranks, strict=True):
        margin = float(candidate.get("margin_rate") or 0)
        rating = candidate.get("rating")
        stock = int(candidate.get("available_stock") or 0)
        price = candidate.get("list_price")
        competitor = candidate.get("competitor_avg_price")

        dimensions = {
            "sell_through": sell_rank,
            "margin": _clamp(margin / TARGET_MARGIN_RATE),
            "rating": (
                _clamp((float(rating) - RATING_FLOOR) / (RATING_CEIL - RATING_FLOOR))
                if rating is not None
                else 0.0
            ),
            "stock_depth": _clamp(stock / TARGET_STOCK),
            "price_gap": _price_gap_score(price, competitor),
        }

        total = sum(dimensions[key] * getattr(weights, key) for key in dimensions)
        highlights = _build_highlights(candidate, dimensions, weights)

        scored.append(
            ScoredCandidate(
                spu_code=str(candidate.get("spu_code") or ""),
                title=str(candidate.get("title") or ""),
                category=candidate.get("category"),
                sub_category=candidate.get("sub_category"),
                season=candidate.get("season"),
                style_tags=list(candidate.get("style_tags") or []),
                cost_price=_as_float(candidate.get("cost_price")),
                list_price=_as_float(price),
                margin_rate=margin,
                rating=_as_float(rating),
                available_stock=stock,
                sold_30d=int(candidate.get("sold_30d") or 0),
                competitor_avg_price=_as_float(competitor),
                dimension_scores=dimensions,
                # 转成 0~100 更好读，也方便跨批次对比
                total_score=round(total * 100, 2),
                highlights=highlights,
            )
        )

    # 同分时用 spu_code 兜底排序 —— 保证结果**完全可复现**。
    # 只按分数排的话，同分商品的相对顺序依赖 sort 的稳定性，
    # 而输入顺序一变结果就变，评测时会出现"同样的数据两次跑结果不同"。
    scored.sort(key=lambda c: (-c.total_score, c.spu_code))
    return scored


def _price_gap_score(price: Any, competitor_avg: Any) -> float:
    """定价相对竞品有多少空间。

    吊牌价明显低于竞品均价 → 有价格优势 → 高分。
    高过竞品均价 → 没有空间 → 0 分。

    只给"低于"的部分打分：定价远低于竞品时，说明**还有往上涨的余地**
    （即"价格空间"），这是好事的另一面 —— 上架时可以把售价定在竞品之下、
    吊牌之上，既保毛利又有竞争力。
    """
    if price is None or not competitor_avg:
        return 0.0
    price_f, competitor_f = float(price), float(competitor_avg)
    if competitor_f <= 0:
        return 0.0
    gap = (competitor_f - price_f) / competitor_f
    # 低于竞品 30% 以上就认为价格空间已经被充分压缩，给满分
    return _clamp(gap / 0.30)


def _build_highlights(
    candidate: Mapping[str, Any], dimensions: dict[str, float], weights: ScoreWeights
) -> list[str]:
    """给每个候选生成可读的加分理由。

    注意这是**代码生成的**，不是模型编的 —— 每条都能对回具体数字，
    经得起运营追问"凭什么给它高分"。
    """
    highlights: list[str] = []
    if dimensions["sell_through"] >= 0.8:
        highlights.append(f"近 30 天销量 {candidate.get('sold_30d')} 件，位列候选池前 20%")
    if dimensions["margin"] >= 0.8:
        highlights.append(f"毛利率 {float(candidate.get('margin_rate') or 0):.1%}，处于健康区间")
    if dimensions["price_gap"] >= 0.7:
        competitor = candidate.get("competitor_avg_price")
        price = candidate.get("list_price")
        if competitor and price:
            diff = (float(competitor) - float(price)) / float(competitor)
            highlights.append(f"吊牌价低于同类竞品均价 {diff:.1%}，有调价与促销空间")
    if dimensions["rating"] >= 0.8:
        highlights.append(f"站内评分 {candidate.get('rating')}，口碑良好")
    if dimensions["stock_depth"] >= 0.8:
        highlights.append(f"可用库存 {candidate.get('available_stock')} 件，可支撑首波投放")
    return highlights


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


# ---------------------------------------------------------------------------
# 一步到位
# ---------------------------------------------------------------------------
def rank_candidates(
    candidates: list[Mapping[str, Any]],
    *,
    constraints: HardConstraints | None = None,
    weights: ScoreWeights | None = None,
) -> SelectionOutcome:
    """硬约束过滤 → 打分排序。选品 Agent 的主逻辑。"""
    constraints = constraints or HardConstraints()
    passed, rejected = apply_constraints(candidates, constraints)
    scored = score_candidates(passed, weights=weights)
    return SelectionOutcome(
        scored=scored,
        rejected=rejected,
        constraints=constraints,
        weights=weights or ScoreWeights(),
        total_input=len(candidates),
    )
