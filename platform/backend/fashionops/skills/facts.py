"""技能文档里可以引用的「事实」—— **值全部从代码现取，一律不手写**。

## 为什么必须有这一层

技能文档里注定要出现一批数字：退货率超多少算红线、库存下限多少件、
五维权重各占多少、分组样本低于多少不下结论。

这些数字**代码里已经有了**（`agents/analytics_rules.py`、`agents/scoring.py`）。
如果技能文档把它们**再抄一遍**，系统里就有了两份事实：

- 改代码时只改了 Python 那份；
- md 里那份静静地变成假的 —— 而模型会照着假的那份讲给运营听，
  讲得有根有据，因为"文档里就是这么写的"。

所以技能文档里写的是占位符 ``{{ return_rate_critical }}``，
渲染时才从代码取值。**改了常量，技能文档下一次渲染就是对的。**

## 代价，说清楚

技能文档单独打开看是**不完整**的（占位符没填）。这是刻意的取舍：
宁可单看 md 缺一块，也不要系统里存在两份会各自漂移的数字。
想看填好之后的样子：``fashionops-skills render <name>``。

## 加新事实的三条规矩

1. **值必须来自 import 得到的常量或 dataclass 默认值**，不能在返回的字典里
   写死一个数字 —— 那就又变成手抄了，只不过抄到了 Python 文件里。
2. 事实名用**蛇形小写**，和占位符同名（``{{ min_stock }}`` ↔ ``"min_stock"``）。
3. 事实只放"业务规范"类数字。像 `MAX_CHUNK_CHARS` 这种纯实现参数不要放 ——
   技能文档讲的是**怎么做业务**，不是系统怎么实现的。
"""

from __future__ import annotations

from functools import lru_cache


def _pct(value: float) -> str:
    """0.3 → ``30%``。技能文档是给人读的，不写 0.3。"""
    return f"{value:.0%}"


@lru_cache(maxsize=1)
def facts() -> dict[str, str]:
    """全部可引用的事实。

    **延迟 import**：本模块会被 `agents.loop` 间接引用，而 `agents` 包的
    `__init__` 会 import 一批东西 —— 在模块顶层 import `agents.*` 有踩进
    循环导入的风险，而那种报错（`partially initialized module`）跟真实原因
    差得很远。放进函数里、用 lru_cache 兜住开销，一次都不多花。
    """
    from fashionops.agents.analytics_rules import (
        AOV_GAP_WARNING,
        CHANNEL_SHARE_WARNING,
        MIN_ORDERS_FOR_JUDGEMENT,
        REFUND_RATIO_WARNING,
        RETURN_RATE_CRITICAL,
        RETURN_RATE_WARNING,
    )
    from fashionops.agents.scoring import HardConstraints, ScoreWeights

    # 默认实例就是事实 —— 不给"文档里填一个别的值"留任何余地
    constraints = HardConstraints()
    weights = ScoreWeights()

    return {
        # ---- 经营分析的阈值（analytics_rules.py）----
        "return_rate_critical": _pct(RETURN_RATE_CRITICAL),
        "return_rate_warning": _pct(RETURN_RATE_WARNING),
        "aov_gap_warning": _pct(AOV_GAP_WARNING),
        "channel_share_warning": _pct(CHANNEL_SHARE_WARNING),
        "refund_ratio_warning": _pct(REFUND_RATIO_WARNING),
        "min_orders_for_judgement": str(MIN_ORDERS_FOR_JUDGEMENT),
        # ---- 选品约束与权重（scoring.py）----
        "min_stock": str(constraints.min_stock),
        "min_margin_rate": _pct(constraints.min_margin_rate),
        "exclude_statuses": "、".join(constraints.exclude_statuses),
        "weight_sell_through": _pct(weights.sell_through),
        "weight_margin": _pct(weights.margin),
        "weight_price_gap": _pct(weights.price_gap),
        "weight_rating": _pct(weights.rating),
        "weight_stock_depth": _pct(weights.stock_depth),
    }


def describe() -> list[tuple[str, str]]:
    """给 CLI 和测试用：事实名 + 当前值，按名排序。"""
    return sorted(facts().items())


__all__ = ["describe", "facts"]
