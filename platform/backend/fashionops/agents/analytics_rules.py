"""经营告警规则 —— 确定性判定，不经过模型。

## 为什么必须有这个模块

第一版把"严重程度"也交给模型判断，prompt 里写清楚了
「退货率 > 30% 是 critical」。结果真实跑 glm-4-flash 时，
它对 14.69% 的退货率照样标了 critical —— **模型会忽略阈值**。

这不是模型不听话，而是把不该它做的事派给了它。阈值判断是纯粹的
数值比较，代码做得到 100% 准确且可复现；交给模型就变成概率性的。

所以职责重新划成：

| 谁 | 做什么 |
| --- | --- |
| **代码**（本模块） | 按阈值算出「哪些指标越线了、越了多少、多严重」 |
| **模型** | 读这些结论，写成人能看懂的分析文字 |

这也是这个项目从头到尾在讲的那条线：**算数和判定交给代码，表达交给模型**。

## 一条容易被忽略的规则：小样本不下结论

一个只有 3 单的类目退掉 1 单，退货率 33%，按阈值算是 critical。
但这大概率只是噪声 —— 拿它去指挥业务，就是"用统计学上无意义的数字做决策"。

所以低于 ``MIN_ORDERS_FOR_JUDGEMENT`` 的分组**只报事实、不下判定**。
真实数据里最长见的坑不是漏报，而是把噪声当信号。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 阈值
# ---------------------------------------------------------------------------
#: 退货率超过它就告警。30% 是服装电商的经验红线
RETURN_RATE_CRITICAL = 0.30
RETURN_RATE_WARNING = 0.20

#: 客单价低于整体这么多就提示
AOV_GAP_WARNING = 0.20

#: 单一渠道 GMV 占比超过它就提示集中风险
CHANNEL_SHARE_WARNING = 0.60

#: 退款金额占 GMV 比例
REFUND_RATIO_WARNING = 0.15

#: 分组样本少于这么多单，只报事实不下判定
MIN_ORDERS_FOR_JUDGEMENT = 20


class Alert(BaseModel):
    """一条确定性告警。"""

    code: Literal[
        "return_rate",
        "aov_gap",
        "channel_concentration",
        "refund_ratio",
        "insufficient_sample",
    ]
    level: Literal["critical", "warning", "info"]
    dimension: str = Field(description="分组名，如「女装」「直播」")
    metric: str = Field(description="指标名")
    value: float
    threshold: float | None = None
    message: str


def _pct(value: float) -> str:
    return f"{value:.1%}"


def detect_alerts(
    totals: dict[str, Any],
    by_category: list[dict[str, Any]],
    by_channel: list[dict[str, Any]],
) -> list[Alert]:
    """按阈值扫一遍所有分组，返回确定性告警列表。

    ``by_category`` / ``by_channel`` 是 ``erp.query_sales_summary`` 的原始输出，
    这里**不做任何再聚合** —— 汇总口径由 SQL 负责，规则只负责比较。
    """
    alerts: list[Alert] = []

    overall_aov = float(totals.get("avg_order_value") or 0.0)
    overall_gmv = float(totals.get("gmv") or 0.0)
    refund_amount = float(totals.get("refund_amount") or 0.0)

    # ---- 整体退款金额占比 ----
    if overall_gmv > 0:
        ratio = refund_amount / overall_gmv
        if ratio >= REFUND_RATIO_WARNING:
            alerts.append(
                Alert(
                    code="refund_ratio",
                    level="warning" if ratio < RETURN_RATE_CRITICAL else "critical",
                    dimension="整体",
                    metric="退款金额占 GMV 比例",
                    value=round(ratio, 4),
                    threshold=REFUND_RATIO_WARNING,
                    message=(
                        f"整体退款金额 {refund_amount:,.2f} 元，占 GMV 的 {_pct(ratio)}，"
                        f"超过 {_pct(REFUND_RATIO_WARNING)} 参考线"
                    ),
                )
            )

    # ---- 类目与渠道：退货率 + 客单价偏离 ----
    alerts.extend(_scan_groups(by_category, "类目", overall_aov))
    alerts.extend(_scan_groups(by_channel, "渠道", overall_aov))

    # ---- 渠道集中度 ----
    if overall_gmv > 0 and by_channel:
        for row in by_channel:
            share = float(row.get("gmv") or 0.0) / overall_gmv
            if share >= CHANNEL_SHARE_WARNING:
                alerts.append(
                    Alert(
                        code="channel_concentration",
                        level="warning",
                        dimension=row["group"],
                        metric="渠道 GMV 占比",
                        value=round(share, 4),
                        threshold=CHANNEL_SHARE_WARNING,
                        message=(
                            f"{row['group']}渠道贡献了 {_pct(share)} 的 GMV，"
                            f"来源过于集中，一旦该渠道限流或提价会直接影响整体营收"
                        ),
                    )
                )

    order = {"critical": 0, "warning": 1, "info": 2}
    return sorted(alerts, key=lambda a: (order[a.level], -a.value))


def _scan_groups(
    groups: list[dict[str, Any]], kind: str, overall_aov: float
) -> list[Alert]:
    out: list[Alert] = []
    for row in groups:
        name = row["group"]
        orders = int(row.get("order_count") or 0)
        rate = float(row.get("return_rate") or 0.0)

        # 样本太小：只报事实。把它标成 critical 会让整个告警列表失去可信度
        if orders < MIN_ORDERS_FOR_JUDGEMENT:
            if orders > 0 and rate > 0:
                out.append(
                    Alert(
                        code="insufficient_sample",
                        level="info",
                        dimension=name,
                        metric="退货率",
                        value=round(rate, 4),
                        threshold=None,
                        message=(
                            f"{kind}「{name}」仅 {orders} 单、退款 {row.get('refund_count', 0)} 单，"
                            f"样本不足以判断趋势（少于 {MIN_ORDERS_FOR_JUDGEMENT} 单不下判定）"
                        ),
                    )
                )
            continue

        if rate >= RETURN_RATE_CRITICAL:
            out.append(
                Alert(
                    code="return_rate",
                    level="critical",
                    dimension=name,
                    metric="退货率",
                    value=round(rate, 4),
                    threshold=RETURN_RATE_CRITICAL,
                    message=(
                        f"{kind}「{name}」退货率 {_pct(rate)}（{row.get('refund_count', 0)}/{orders} 单），"
                        f"超过 {_pct(RETURN_RATE_CRITICAL)} 红线"
                    ),
                )
            )
        elif rate >= RETURN_RATE_WARNING:
            out.append(
                Alert(
                    code="return_rate",
                    level="warning",
                    dimension=name,
                    metric="退货率",
                    value=round(rate, 4),
                    threshold=RETURN_RATE_WARNING,
                    message=(
                        f"{kind}「{name}」退货率 {_pct(rate)}（{row.get('refund_count', 0)}/{orders} 单），"
                        f"高于 {_pct(RETURN_RATE_WARNING)} 关注线"
                    ),
                )
            )

        aov = float(row.get("avg_order_value") or 0.0)
        if overall_aov > 0 and aov > 0:
            gap = (overall_aov - aov) / overall_aov
            if gap >= AOV_GAP_WARNING:
                out.append(
                    Alert(
                        code="aov_gap",
                        level="warning",
                        dimension=name,
                        metric="客单价",
                        value=round(aov, 2),
                        threshold=round(overall_aov * (1 - AOV_GAP_WARNING), 2),
                        message=(
                            f"{kind}「{name}」客单价 {aov:,.2f} 元，"
                            f"比整体 {overall_aov:,.2f} 元低 {_pct(gap)}"
                        ),
                    )
                )
    return out


__all__ = [
    "AOV_GAP_WARNING",
    "CHANNEL_SHARE_WARNING",
    "MIN_ORDERS_FOR_JUDGEMENT",
    "REFUND_RATIO_WARNING",
    "RETURN_RATE_CRITICAL",
    "RETURN_RATE_WARNING",
    "Alert",
    "detect_alerts",
]
