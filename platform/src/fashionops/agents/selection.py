"""选品 Agent —— 把"帮我选 3 款秋季女装"变成一份有依据的候选清单。

## 分工：模型负责理解与解释，代码负责计算与判定

这是这个 Agent 最核心的设计决策。

| 环节 | 由谁做 | 为什么 |
| --- | --- | --- |
| 解析需求（类目/季节/数量/预算） | **模型** | 自然语言理解是模型的主场 |
| 拉候选数据 | 工具（网关） | 数据口径要固定，不能让模型即兴推导 |
| 硬约束过滤 | **代码** | "库存 3 件"能不能上架是个客观事实，没有商量余地 |
| 多维打分排序 | **代码** | 让模型算数会得到不可复现且静默错误的结果 |
| 写推荐理由 | **模型** | 自然语言表达是模型的主场 |

很多人做 Agent 会把整段都交给模型，结果是：
同样的输入跑两次排名不一样（没法评测）、
数字算错（而且不报错）、
50 个商品的字段全塞进上下文（token 成本高一个数量级）。

## 节点为什么用「工厂函数」而不是直接接 config

写成 ``make_selection_node(ctx)`` 返回一个闭包，而不是在节点里
``get_context(config)``，有三个好处：

1. 依赖在构图时就确定下来，节点内部不用处理"ctx 没传"这种分支
2. 单测可以直接调节点函数，不用构造 ``RunnableConfig``
3. 不需要依赖 LangGraph 对函数签名的内省行为 —— 那个规则会随版本变
"""

from __future__ import annotations

import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from fashionops.agents.scoring import (
    HardConstraints,
    ScoreWeights,
    SelectionOutcome,
    rank_candidates,
)
from fashionops.agents.state import (
    RunContext,
    TaskState,
    check_budget,
    step_update,
)
from fashionops.gateway import CallContext
from fashionops.llm import Message, structured_call


# ---------------------------------------------------------------------------
# 结构化输出契约
# ---------------------------------------------------------------------------
class SelectionCriteria(BaseModel):
    """从自然语言需求里解析出的选品条件。"""

    category: str | None = Field(default=None, description="一级类目，如「女装」。不确定就留空")
    season: str | None = Field(default=None, description="季节，如「秋季」。不确定就留空")
    count: int = Field(default=3, ge=1, le=10, description="需要选出几款")
    min_stock: int = Field(default=30, ge=0, le=10000, description="可用库存下限（件）")
    min_margin_rate: float = Field(
        default=0.40, ge=0.0, le=1.0, description="毛利率下限，0~1 的小数"
    )
    max_cost: Decimal | None = Field(default=None, description="单件成本上限（元）")
    style_preference: list[str] = Field(
        default_factory=list, description="风格偏好，如「法式」「通勤」"
    )
    reasoning: str = Field(default="", description="一句话说明你是怎么理解这个需求的")


class ItemNarrative(BaseModel):
    spu_code: str = Field(description="商品 SPU 编码，必须来自给定列表")
    reason: str = Field(description="推荐理由，1-2 句，要引用具体数字")


class SelectionNarrative(BaseModel):
    """模型负责写的那部分。"""

    overall: str = Field(description="整体结论，2-3 句：这批候选的整体情况和主要建议")
    items: list[ItemNarrative] = Field(default_factory=list, description="逐款推荐理由")


# ---------------------------------------------------------------------------
# 需求解析
# ---------------------------------------------------------------------------
_CRITERIA_SYSTEM = (
    "你是服装电商的选品助理。从用户的自然语言需求里提取结构化的选品条件。\n"
    "规则：\n"
    "1. 用户没提到的字段用默认值，不要瞎猜具体数字\n"
    "2. 「秋季女装」→ category=女装, season=秋季\n"
    "3. 数量词如「3 款」「几款」→ count\n"
    "4. 只有明确提到成本上限时才填 max_cost\n"
)

#: 解析失败时的兜底关键词表。
#: 用关键词而不是"直接报错"，是因为"需求解析失败"不该让整个任务挂掉 ——
#: 退一步用保守默认值继续跑，往往还能给出有用的结果。
_FALLBACK_KEYWORDS: dict[str, dict[str, str]] = {
    "category": {"女装": "女装", "男装": "男装", "配饰": "配饰"},
    "season": {"春": "春季", "夏": "夏季", "秋": "秋季", "冬": "冬季"},
}


def parse_criteria(ctx: RunContext, user_request: str) -> tuple[SelectionCriteria, Any]:
    """调模型解析需求；失败则退回关键词兜底。"""
    result = structured_call(
        ctx.model,
        [Message.system(_CRITERIA_SYSTEM), Message.user(user_request)],
        SelectionCriteria,
    )
    if result.ok:
        return result.value, result.response  # type: ignore[return-value]

    # 兜底：至少把类目和季节认出来，其余用模型默认值
    fallback = SelectionCriteria(reasoning="（模型解析失败，已按关键词兜底）")
    for word, value in _FALLBACK_KEYWORDS["category"].items():
        if word in user_request:
            fallback.category = value
    for word, value in _FALLBACK_KEYWORDS["season"].items():
        if word in user_request:
            fallback.season = value
    return fallback, result.response


# ---------------------------------------------------------------------------
# 叙事生成
# ---------------------------------------------------------------------------
_NARRATIVE_SYSTEM = (
    "你是服装电商选品专家。下面已经用代码算好了候选商品的评分和排序，"
    "你的任务**只是解释**，不要重新排序、不要改动数字、不要新增候选。\n"
    "每一条理由必须引用给定的具体数字（销量、毛利率、库存、竞品价差）。\n"
    "spu_code 必须严格来自给定列表，不能编造。"
)


def build_narrative(
    ctx: RunContext, criteria: SelectionCriteria, outcome: SelectionOutcome, top_n: int
) -> tuple[SelectionNarrative | None, Any]:
    picked = outcome.top(top_n)
    if not picked:
        return None, None

    payload = {
        "user_criteria": criteria.model_dump(mode="json"),
        "candidates": [c.to_dict() for c in picked],
    }
    result = structured_call(
        ctx.model,
        [
            Message.system(_NARRATIVE_SYSTEM),
            Message.user(
                "请为下面这些已排好序的候选商品写推荐理由。\n"
                f"{_json(payload)}"
            ),
        ],
        SelectionNarrative,
    )
    return (result.value if result.ok else None), result.response  # type: ignore[return-value]


def _json(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# 节点工厂
# ---------------------------------------------------------------------------
def make_selection_node(
    ctx: RunContext,
    *,
    progress_done: int = 45,
    candidate_limit: int = 60,
) -> Callable[[TaskState], dict[str, Any]]:
    """构造选品节点。

    ``ctx`` 里带着网关和模型客户端；返回的闭包可以直接
    ``builder.add_node("selection", make_selection_node(ctx))``。
    """

    def selection_node(state: TaskState) -> dict[str, Any]:
        started = time.perf_counter()
        # 预算检查放在最前面：预算已经用完了就不该再发起新的模型调用
        check_budget(state)

        tool_names: list[str] = []
        tokens = 0
        cost = Decimal("0")
        llm_calls = 0

        # ---- 1. 解析需求（模型）----
        criteria, criteria_response = parse_criteria(ctx, state["user_request"])
        if criteria_response is not None:
            tokens += criteria_response.usage.total_tokens
            cost += criteria_response.cost
            llm_calls += 1

        # ---- 2. 拉候选数据（网关）----
        tool_names.append("erp.query_candidates")
        call = ctx.gateway.call(
            "erp.query_candidates",
            {"category": criteria.category, "season": criteria.season, "limit": candidate_limit},
            ctx=CallContext(
                actor=ctx.actor,
                scopes=ctx.scopes,
                task_id=ctx.task_id,
                trace_id=f"{state.get('task_no', '')}:selection",
            ),
        )
        if not call.ok:
            # 工具失败不抛异常 —— 把错误记进状态，让编排层决定是重试还是转人工
            return step_update(
                state,
                node="selection",
                agent="selection",
                status="failed",
                summary=f"拉取候选商品失败：{call.error}",
                tool_calls=tool_names,
                duration_ms=_ms(started),
                tokens=tokens,
                cost=cost,
                llm_calls=llm_calls,
                error=str(call.error),
                extra={"errors": [f"erp.query_candidates 失败：{call.error}"]},
            )

        candidates: list[dict[str, Any]] = call.data.get("candidates", [])

        # ---- 3. 硬约束 + 打分排序（代码）----
        constraints = HardConstraints(
            min_stock=criteria.min_stock,
            min_margin_rate=criteria.min_margin_rate,
            max_cost=criteria.max_cost,
        )
        outcome = rank_candidates(
            candidates, constraints=constraints, weights=ScoreWeights()
        )

        if not outcome.scored:
            return step_update(
                state,
                node="selection",
                agent="selection",
                status="failed",
                summary=(
                    f"候选池 {outcome.total_input} 个商品全部被硬约束过滤，无可用候选"
                ),
                tool_calls=tool_names,
                duration_ms=_ms(started),
                tokens=tokens,
                cost=cost,
                llm_calls=llm_calls,
                error="没有通过硬约束的候选",
                extra={
                    "findings": {
                        **state.get("findings", {}),
                        "selection": {
                            "criteria": criteria.model_dump(mode="json"),
                            "filtered_out": len(outcome.rejected),
                            "rejected_samples": [r.to_dict() for r in outcome.rejected[:5]],
                        },
                    },
                    "errors": ["候选池为空，需要放宽约束或补充货源"],
                },
            )

        # ---- 4. 写推荐理由（模型）----
        narrative, narrative_response = build_narrative(
            ctx, criteria, outcome, criteria.count
        )
        if narrative_response is not None:
            tokens += narrative_response.usage.total_tokens
            cost += narrative_response.cost
            llm_calls += 1

        picked = outcome.top(criteria.count)
        reasons = {item.spu_code: item.reason for item in (narrative.items if narrative else [])}

        selected = []
        for rank, candidate in enumerate(picked, start=1):
            item = candidate.to_dict()
            item["rank"] = rank
            # 模型可能编出不存在的 spu_code；取不到理由就退回代码生成的 highlights，
            # 保证每条推荐**一定有依据**，而不是留空或显示模型的幻觉
            item["reason"] = reasons.get(candidate.spu_code) or "；".join(candidate.highlights)
            selected.append(item)

        summary = narrative.overall if narrative else "已按多维度加权评分选出候选商品"
        return step_update(
            state,
            node="selection",
            agent="selection",
            status="succeeded",
            summary=summary,
            tool_calls=tool_names,
            duration_ms=_ms(started),
            tokens=tokens,
            cost=cost,
            llm_calls=llm_calls,
            progress=progress_done,
            extra={
                "selected": selected,
                "findings": {
                    **state.get("findings", {}),
                    "selection": {
                        "criteria": criteria.model_dump(mode="json"),
                        "total_input": outcome.total_input,
                        "passed": outcome.passed,
                        "filtered_out": len(outcome.rejected),
                        "constraints": outcome.constraints.describe(),
                        "weights": outcome.weights.as_dict(),
                        # 被挡掉的样例要留在状态里：运营最常问的是
                        # "为什么这个款没选上"，答不出来就很难解释
                        "rejected_samples": [r.to_dict() for r in outcome.rejected[:10]],
                        "overall": summary,
                    },
                },
            },
        )

    return selection_node


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
