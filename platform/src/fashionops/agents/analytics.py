"""数据分析 Agent —— 把经营数据变成结论 + 一份能直接发出去的 Excel。

## 沿用选品 Agent 的分工

* **代码**：聚合查询、退货率、客单价、阈值判定、报表渲染 ——
  凡是能和数字比较的都由代码算
* **模型**：读已经算好的数字和告警，写成人能看懂的分析文字

这里踩过一个坑，值得写在最前面：

第一版把"严重程度"也交给模型判断，prompt 里写了「退货率 > 30% 是 critical」。
真实跑 glm-4-flash 时，它对 14.69% 的退货率照样标了 critical。

**模型会忽略阈值**。这不是它不听话，而是把不该它做的事派给了它。
所以告警判定搬到了 ``analytics_rules.detect_alerts``（纯代码、可复现），
模型接下来只负责解释"这些告警意味着什么"。

## 上下文工程

这个 Agent 不接收上一节点的完整产出，只接收**精炼结论**：:

    findings["selection"] -> {"criteria": ..., "passed": 7, "filtered_out": 2, "overall": "..."}

为什么不直接给全部候选商品的明细？
因为一个候选列表有几十条记录、每条十几个字段，塞进上下文要几千 token，
而模型真正需要的只是"选了几款、为什么"。**上游节点负责压缩，下游节点拿结论**，
这是长链路不爆上下文的核心做法。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from fashionops.agents.analytics_rules import Alert, detect_alerts
from fashionops.agents.state import (
    Artifact,
    RunContext,
    TaskState,
    check_budget,
    step_update,
)
from fashionops.gateway import CallContext
from fashionops.llm import Message, structured_call


# ---------------------------------------------------------------------------
# 结构化输出
# ---------------------------------------------------------------------------
class MetricInsight(BaseModel):
    """一条分析发现（模型写的解读）。

    **没有 severity 字段** —— 这是故意的。严重程度由代码按阈值算定，
    让模型再填一遍只会得到和告警列表互相矛盾的两份"严重程度"。
    """

    dimension: str = Field(description="涉及维度，如「连衣裙类目」「抖音渠道」")
    observation: str = Field(description="观察到的事实，必须引用给定数字")
    action: str = Field(default="", description="建议动作，一句话")


class AnalyticsNarrative(BaseModel):
    """模型负责写的那部分：解读，不是计算。"""

    summary: str = Field(description="整体结论，3-5 句，要有具体数字")
    insights: list[MetricInsight] = Field(
        default_factory=list, description="值得关注的发现，3-6 条，按重要性排序"
    )
    data_quality_notes: list[str] = Field(
        default_factory=list, description="数据口径或质量问题提示，没有就留空"
    )


_SYSTEM = (
    "你是服装电商的数据分析师。下面给你的数字和告警**已经由系统算好了**，"
    "你的任务是解读它们，不是重新计算。\n"
    "硬性要求：\n"
    "1. 不要重新计算或改动任何数字，只引用给定的值\n"
    "2. **不要自己判断严重程度** —— 系统已经给出 alerts，严重程度以它为准；"
    "你的 insights 只写解释和建议，不要写「这是严重问题」这类定性\n"
    "3. 每条 observation 必须包含具体数字，不要写「有所上升」这种空话\n"
    "4. 如果 alerts 里出现了 insufficient_sample，说明该分组样本太小，"
    "在结论里不要把它当趋势\n"
    "5. 建议动作要具体可执行，不要写「加强管理」这种废话\n"
)


# ---------------------------------------------------------------------------
# 内部步骤
# ---------------------------------------------------------------------------
def _tool_call(
    ctx: RunContext,
    state: TaskState,
    tool_name: str,
    args: dict[str, Any],
    node: str,
) -> Any:
    return ctx.gateway.call(
        tool_name,
        args,
        ctx=CallContext(
            actor=ctx.actor,
            scopes=ctx.scopes,
            task_id=ctx.task_id,
            trace_id=f"{state.get('task_no', '')}:{node}",
            # 带上幂等键，粒度是「任务 + 节点」。
            # 网关会把它和入参摘要拼起来做真正的键。
            # 效果：节点因为下游失败被重跑时，不会多出一条重复的 assets 记录 ——
            # 用户看到两份同名报表、其中一份还是旧的，比没有报表更糟。
            idempotency_key=f"{state.get('task_no', '')}:{node}" if state.get("task_no") else None,
        ),
    )


def _build_sheets(
    by_category: dict[str, Any], by_channel: dict[str, Any]
) -> list[dict[str, Any]]:
    """构造交给 ``report.export_excel`` 的表格定义。

    数字格式在这里就定好 —— ``#,##0.00`` 给金额、``0.0%`` 给比例。
    不指定的话 Excel 会按默认格式显示，金额变成一长串没有分隔的数字，
    每个看报表的人都要在脑子里重新分组。
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


# ---------------------------------------------------------------------------
# 节点工厂
# ---------------------------------------------------------------------------
def make_analytics_node(
    ctx: RunContext,
    *,
    days: int = 30,
    progress_done: int = 75,
) -> Callable[[TaskState], dict[str, Any]]:
    """构造数据分析节点。"""

    def analytics_node(state: TaskState) -> dict[str, Any]:
        started = time.perf_counter()
        check_budget(state)

        tool_names: list[str] = []
        tokens = 0
        cost = Decimal("0")
        llm_calls = 0

        # ---- 1. 拉聚合数据（网关）----
        by_category = _tool_call(
            ctx, state, "erp.query_sales_summary", {"days": days, "group_by": "category"}, "analytics"
        )
        tool_names.append("erp.query_sales_summary")
        if not by_category.ok:
            return step_update(
                state, node="analytics", agent="analytics", status="failed",
                summary=f"销售数据查询失败：{by_category.error}",
                tool_calls=tool_names, duration_ms=_ms(started),
                error=str(by_category.error),
                extra={"errors": [f"erp.query_sales_summary 失败：{by_category.error}"]},
            )

        by_channel = _tool_call(
            ctx, state, "erp.query_sales_summary", {"days": days, "group_by": "channel"}, "analytics"
        )
        tool_names.append("erp.query_sales_summary")
        if by_channel.ok:
            channel_data = by_channel.data
        else:
            # 渠道维度失败不该让整份报表作废 —— 类目维度已经拿到了，
            # 降级成"只有类目数据"继续做，比直接失败有用得多。
            # 但降级必须显式记录，不能装作一切正常。
            channel_data = {"days": days, "group_by": "channel", "totals": {}, "groups": []}

        # ---- 2. 代码算告警（纯确定性，不经过模型）----
        alerts = detect_alerts(
            by_category.data.get("totals", {}),
            by_category.data.get("groups", []),
            channel_data.get("groups", []),
        )
        critical_alerts = [a for a in alerts if a.level == "critical"]

        # ---- 3. 让模型解读（只给算好的数字和告警）----
        prior = state.get("findings", {}).get("selection")
        payload = {
            "window_days": days,
            "totals": by_category.data.get("totals", {}),
            "by_category": by_category.data.get("groups", []),
            "by_channel": channel_data.get("groups", []),
            # 告警是代码判定的，直接交给模型当"已知事实"
            "alerts": [a.model_dump() for a in alerts],
            # 上游结论是"精炼过的"，不是完整候选列表
            "upstream_selection": _compact(prior),
        }
        narrative, response = _narrate(ctx, payload)
        if response is not None:
            tokens += response.usage.total_tokens
            cost += response.cost
            llm_calls += 1

        if narrative is None:
            return step_update(
                state, node="analytics", agent="analytics", status="failed",
                summary="模型未能产出合规的分析结论（结构化输出多次校验失败）",
                tool_calls=tool_names, duration_ms=_ms(started), tokens=tokens, cost=cost,
                llm_calls=llm_calls,
                error="结构化输出失败",
                extra={"errors": ["analytics 结构化输出失败"]},
            )

        critical = critical_alerts

        # ---- 4. 出报表并登记素材（网关）----
        filename = (
            f"{date.today():%Y%m%d}-经营分析"
            f"{'-' + state.get('task_no', '') if state.get('task_no') else ''}.xlsx"
        )
        export = _tool_call(
            ctx, state, "report.export_excel",
            {
                "filename": filename,
                "title": "服装电商经营分析报表",
                "subtitle": f"数据窗口：近 {days} 天 ｜ 生成时间：{date.today():%Y-%m-%d}",
                "sheets": _build_sheets(by_category.data, channel_data),
                "produced_by": "analytics",
                "task_id": ctx.task_id,
            },
            "analytics",
        )
        tool_names.append("report.export_excel")

        artifact: Artifact | None = None
        if export.ok:
            artifact = Artifact(
                kind="report",
                name=export.data["filename"],
                ref=export.data["asset_no"],
                produced_by="analytics",
                meta={
                    "storage_key": export.data["storage_key"],
                    "url": export.data["url"],
                    "size_bytes": export.data["size_bytes"],
                    "sheet_names": export.data["sheet_names"],
                },
            )

        extra: dict[str, Any] = {
            "findings": {
                **state.get("findings", {}),
                "analytics": {
                    "window_days": days,
                    "totals": by_category.data.get("totals", {}),
                    "by_category": by_category.data.get("groups", []),
                    "by_channel": channel_data.get("groups", []),
                    # 代码判定的告警：这才是权威的"什么是问题"
                    "alerts": [a.model_dump() for a in alerts],
                    "critical_count": len(critical),
                    # 模型写的解读：解释和建议
                    "summary": narrative.summary,
                    "insights": [i.model_dump() for i in narrative.insights],
                    "data_quality_notes": narrative.data_quality_notes,
                    "report": export.data if export.ok else None,
                    "report_error": None if export.ok else str(export.error),
                },
            }
        }
        if artifact is not None:
            extra["artifacts"] = [artifact]

        if not export.ok:
            # 报表失败不阻断分析结论 —— 结论本身仍然有价值，只是没有附件
            extra["errors"] = [
                *state.get("errors", []),
                f"报表导出失败：{export.error}",
            ]

        return step_update(
            state, node="analytics", agent="analytics", status="succeeded",
            summary=narrative.summary,
            tool_calls=tool_names, duration_ms=_ms(started),
            tokens=tokens, cost=cost, llm_calls=llm_calls, progress=progress_done,
            extra=extra,
        )

    return analytics_node


def _narrate(ctx: RunContext, payload: dict[str, Any]) -> tuple[AnalyticsNarrative | None, Any]:
    result = structured_call(
        ctx.model,
        [
            Message.system(_SYSTEM),
            Message.user(
                "下面是已经算好的经营数据，请解读。\n"
                f"{json.dumps(payload, ensure_ascii=False, default=str)}"
            ),
        ],
        AnalyticsNarrative,
    )
    return (result.value if result.ok else None), result.response  # type: ignore[return-value]


def _compact(prior: Any) -> Any:
    """把上游结论压成只剩必要字段。

    直接把整个 findings['selection'] 塞进去的话，里面还有 top-10 的完整候选明细
    （每条十几个字段），纯属浪费 token。下游只需要知道"选了几款、挡了几款"。
    """
    if not isinstance(prior, dict):
        return None
    keys = ("criteria", "total_input", "passed", "filtered_out", "overall")
    return {k: prior[k] for k in keys if k in prior}


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


__all__ = [
    "Alert",
    "AnalyticsNarrative",
    "MetricInsight",
    "detect_alerts",
    "make_analytics_node",
]
