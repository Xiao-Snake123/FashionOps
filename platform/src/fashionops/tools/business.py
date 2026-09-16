"""业务工具：原三个专用 Agent（选品 / 分析 / 通知）的**代码部分**，包成网关工具。

（见 docs/12-AI协作交接.md T3。）新架构里**只有一个 Agent**，它自己负责
理解需求和写结论文（那是模型的主场），而所有**算数和判定**都通过这里的
工具完成 —— 分工和原来完全一致（见各原模块的头部注释，已随代码带过来）：

| 原来谁干 | 现在 | 工具 |
| --- | --- | --- |
| selection 节点的"硬约束 + 打分排序" | 代码 | ``selection.rank_candidates`` |
| analytics 节点的"阈值告警判定" | 代码 | ``analytics.detect_alerts`` |
| analytics 节点的"报表渲染 + 素材登记" | 代码 | ``analytics.render_business_report`` |
| notification 节点的"发信" | 网关 | 现成的 ``mail.send``（不走本模块） |

## 为什么这些是工具而"写理由/写解读"不是

算数和判定交给代码才可复现、可审计（见 `agents/scoring.py` 头部注释）；
而"写理由"在新架构里**就是 Agent 本身的输出** —— 它是个模型，
不需要再造一个"让模型调的工具去调模型"。

## ``render_business_report`` 为什么直接调 ``export_excel_core``

工具 handler 拿不到网关（`CallContext` 不传给 handler），
所以它复用 `report.export_excel_core` 这个模块级函数，而不是
"在工具里再调一次工具"。外层调用本身过网关，审计/限流在外层已有记录。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from fashionops.agents.analytics_rules import detect_alerts
from fashionops.agents.scoring import HardConstraints, ScoreWeights, rank_candidates
from fashionops.gateway.registry import Scope, ToolRegistry
from fashionops.tools.report import export_excel_core


# ---------------------------------------------------------------------------
# selection.rank_candidates
# ---------------------------------------------------------------------------
class RankCandidatesArgs(BaseModel):
    """选品打分的入参。

    ``candidates`` 直接用 ``erp.query_candidates`` 的返回行 ——
    Agent 拉到候选后原样传进来，不需要自己变换字段
    （字段名对不上是这类链路最常见的静默错误）。
    """

    candidates: list[dict[str, Any]] = Field(
        min_length=1, max_length=500, description="erp.query_candidates 返回的候选商品列表"
    )
    top_n: int = Field(default=3, ge=1, le=50, description="要选出几款")
    min_stock: int = Field(
        default=30, ge=0, le=10000, description="可用库存下限（件）。库存不够卖就是事故"
    )
    min_margin_rate: float = Field(default=0.40, ge=0.0, le=1.0, description="毛利率下限")
    max_cost: float | None = Field(default=None, description="单件成本上限（元）")
    min_rating: float | None = Field(default=None, description="评分下限")


# ---------------------------------------------------------------------------
# analytics.detect_alerts
# ---------------------------------------------------------------------------
class DetectAlertsArgs(BaseModel):
    """告警判定的入参。

    **没有 severity 之类的可填字段** —— 严重程度由代码按阈值算定
    （见 `agents/analytics_rules.py` 头部注释：模型会忽略阈值，
    实测 14.69% 的退货率照样被标 critical）。这里只接受数据，不接受结论。
    """

    totals: dict[str, Any] = Field(default_factory=dict, description="erp.query_sales_summary 的 totals")
    by_category: list[dict[str, Any]] = Field(
        default_factory=list, description="group_by=category 的 groups"
    )
    by_channel: list[dict[str, Any]] = Field(
        default_factory=list, description="group_by=channel 的 groups"
    )


# ---------------------------------------------------------------------------
# analytics.render_business_report
# ---------------------------------------------------------------------------
class RenderReportArgs(BaseModel):
    by_category: dict[str, Any] = Field(description="group_by=category 的完整返回（含 totals/groups）")
    by_channel: dict[str, Any] = Field(
        default_factory=dict,
        description="group_by=channel 的完整返回。渠道维度失败时可以留空，报表会降级成只有类目",
    )
    days: int = Field(default=30, ge=1, le=365, description="数据窗口（天），写进报表副标题")
    task_id: int | None = None
    task_no: str | None = Field(default=None, description="任务号，用于报表文件名，方便人对上号")


def _build_sheets(
    by_category: dict[str, Any], by_channel: dict[str, Any]
) -> list[dict[str, Any]]:
    """构造三张标准工作表（类目 / 渠道 / 整体）。

    从原 analytics 节点原样搬来。数字格式在这里就定好 ——
    ``#,##0.00`` 给金额、``0.0%`` 给比例。不指定的话 Excel 会按默认格式
    显示，金额变成一长串没有分隔的数字，每个看报表的人都要在脑子里
    重新分组。
    """
    return [
        {
            "name": "类目汇总",
            "columns": [
                {"key": "group", "title": "类目"},
                {"key": "gmv", "title": "GMV", "number_format": "#,##0.00", "align": "right"},
                {"key": "order_count", "title": "订单数", "number_format": "#,##0", "align": "right"},
                {"key": "customer_count", "title": "客户数", "number_format": "#,##0", "align": "right"},
                {
                    "key": "avg_order_value",
                    "title": "客单价",
                    "number_format": "#,##0.00",
                    "align": "right",
                },
                {
                    "key": "refund_amount",
                    "title": "退款金额",
                    "number_format": "#,##0.00",
                    "align": "right",
                },
                {"key": "return_rate", "title": "退货率", "number_format": "0.0%", "align": "right"},
            ],
            "rows": by_category.get("groups", []),
        },
        {
            "name": "渠道汇总",
            "columns": [
                {"key": "group", "title": "渠道"},
                {"key": "gmv", "title": "GMV", "number_format": "#,##0.00", "align": "right"},
                {"key": "order_count", "title": "订单数", "number_format": "#,##0", "align": "right"},
                {
                    "key": "avg_order_value",
                    "title": "客单价",
                    "number_format": "#,##0.00",
                    "align": "right",
                },
                {"key": "return_rate", "title": "退货率", "number_format": "0.0%", "align": "right"},
            ],
            "rows": by_channel.get("groups", []),
        },
        {
            "name": "整体",
            "columns": [
                {"key": "metric", "title": "指标"},
                {"key": "value", "title": "数值", "align": "right"},
            ],
            "rows": [
                {"metric": "统计窗口", "value": f"近 {by_category.get('days', 30)} 天"},
                {"metric": "总 GMV", "value": by_category.get("totals", {}).get("gmv")},
                {"metric": "订单数", "value": by_category.get("totals", {}).get("order_count")},
                {"metric": "客单价", "value": by_category.get("totals", {}).get("avg_order_value")},
                {
                    "metric": "退款金额",
                    "value": by_category.get("totals", {}).get("refund_amount"),
                },
            ],
        },
    ]


def register(registry: ToolRegistry) -> None:
    @registry.tool(
        name="selection.rank_candidates",
        description=(
            "对候选商品做硬约束过滤 + 多维加权打分排序（确定性计算，可复现）。"
            "入参 candidates 直接传 erp.query_candidates 的返回。"
            "返回排序后的 top 列表（含分数、加分理由）和被挡掉的样例 —— "
            "写推荐理由时必须引用返回里的具体数字，不要自己算。"
        ),
        args_model=RankCandidatesArgs,
        scopes=(Scope.ERP_READ,),
        read_only=True,  # 纯计算，不产生任何副作用
        timeout_s=10.0,
        tags=("selection",),
    )
    def rank_candidates_tool(
        candidates: list[dict[str, Any]],
        top_n: int = 3,
        min_stock: int = 30,
        min_margin_rate: float = 0.40,
        max_cost: float | None = None,
        min_rating: float | None = None,
    ) -> dict[str, Any]:
        outcome = rank_candidates(
            candidates,
            constraints=HardConstraints(
                min_stock=min_stock,
                min_margin_rate=min_margin_rate,
                max_cost=Decimal(str(max_cost)) if max_cost is not None else None,
                min_rating=min_rating,
            ),
            weights=ScoreWeights(),
        )

        picked = outcome.top(top_n)
        selected = []
        for rank, candidate in enumerate(picked, start=1):
            item = candidate.to_dict()
            item["rank"] = rank
            # highlights 是代码生成的加分理由，每条都对得回具体数字，
            # 经得起运营追问"凭什么给它高分"
            item["reason"] = "；".join(candidate.highlights) or "综合评分靠前"
            selected.append(item)

        if selected:
            summary = (
                f"候选池 {outcome.total_input} 个，通过硬约束 {outcome.passed} 个，"
                f"已按加权评分选出前 {len(selected)}"
            )
        else:
            summary = (
                f"候选池 {outcome.total_input} 个商品全部被硬约束过滤，无可用候选。"
                "需要放宽约束（如降低库存/毛利下限）或补充货源。"
            )

        return {
            "summary": summary,
            "total_input": outcome.total_input,
            "passed": outcome.passed,
            "filtered_out": len(outcome.rejected),
            "constraints": outcome.constraints.describe(),
            "weights": outcome.weights.as_dict(),
            "selected": selected,
            # 被挡掉的样例必须带上 —— 运营最常问"为什么这个款没选上"
            "rejected_samples": [r.to_dict() for r in outcome.rejected[:10]],
        }

    @registry.tool(
        name="analytics.detect_alerts",
        description=(
            "按阈值判定经营告警（退货率 / 客单价缺口 / 渠道集中 / 退款占比），"
            "确定性计算，不经过模型。写分析结论时以这里的 level 为准 —— "
            "**不要自己判断严重程度**。样本不足的分组只会报 insufficient_sample，"
            "不要当趋势解读。"
        ),
        args_model=DetectAlertsArgs,
        scopes=(Scope.ERP_READ,),
        read_only=True,
        timeout_s=10.0,
        tags=("analytics",),
    )
    def detect_alerts_tool(
        totals: dict[str, Any],
        by_category: list[dict[str, Any]],
        by_channel: list[dict[str, Any]],
    ) -> dict[str, Any]:
        alerts = detect_alerts(totals, by_category, by_channel)
        critical = [a for a in alerts if a.level == "critical"]
        warning = [a for a in alerts if a.level == "warning"]
        return {
            # level 是代码算的权威结论；模型只负责解释这些告警意味着什么
            "alerts": [a.model_dump() for a in alerts],
            "critical_count": len(critical),
            "warning_count": len(warning),
            "has_critical": bool(critical),
        }

    @registry.tool(
        name="analytics.render_business_report",
        description=(
            "把已经查到的销售汇总数据渲染成标准经营分析 Excel 报表"
            "（类目 / 渠道 / 整体三张表，数字格式已定好），落盘并登记为素材。"
            "返回 asset_no 与下载 URL。先调 erp.query_sales_summary 拿数据，"
            "再把返回原样传进来，不要自己改数字。"
        ),
        args_model=RenderReportArgs,
        scopes=(Scope.ASSET_WRITE,),
        read_only=False,
        # 同样的入参重跑得到同样的报表，不产生对外副作用，幂等安全
        idempotent=True,
        timeout_s=60.0,
        tags=("analytics", "report"),
        rate_limit_per_minute=10,
    )
    def render_business_report(
        by_category: dict[str, Any],
        by_channel: dict[str, Any],
        days: int = 30,
        task_id: int | None = None,
        task_no: str | None = None,
    ) -> dict[str, Any]:
        filename = (
            f"{date.today():%Y%m%d}-经营分析"
            f"{'-' + task_no if task_no else ''}.xlsx"
        )
        return export_excel_core(
            filename=filename,
            title="服装电商经营分析报表",
            subtitle=f"数据窗口：近 {days} 天 ｜ 生成时间：{date.today():%Y-%m-%d}",
            sheets=_build_sheets(by_category, by_channel),
            produced_by="analytics",
            task_id=task_id,
        )


__all__ = ["register"]
