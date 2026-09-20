"""`tools/business.py` 的冒烟测试。

验证三件事：能注册、能过网关被调用、输入输出契约稳。
数值正确性的验证在 `test_selection.py` / `test_analytics_rules.py`（打分、
告警的单元测试早就存在），这里不重复 —— 只测"包"这一层。

特别注意 `render_business_report`：它落盘 + 登记 assets，走真实存储与
测试库 —— 这是刻意的，落盘和登记本来就是它存在的意义，
mock 掉就什么都没测到。测试结束后清理本地文件。
"""

from __future__ import annotations

import pytest

from fashionops.gateway.registry import registry
from fashionops.tools import register_all_tools


@pytest.fixture()
def clean_registry():
    """注册是全局幂等前提（别处可能已注册过），这里只保证"在场"。

    注册表本身不允许重名注册（刻意的，防止重复导入把 handler 换掉），
    所以不能无脑 `register_all_tools()` —— 先查再注册。
    """
    registered_here = "selection.rank_candidates" not in registry
    if registered_here:
        register_all_tools()
    yield
    # 别的测试模块还要用全局注册表，这里不卸载


def _candidate(
    code: str = "SPU-001",
    stock: int = 50,
    margin: float = 0.55,
    rating: float = 4.6,
    cost: float = 100.0,
) -> dict:
    # 字段名与 erp.query_candidates 的返回对齐 —— Agent 会把那边的结果
    # 原样传进来，字段对不上是这类链路最常见的静默错误。
    return {
        "spu_code": code,
        "title": f"测试商品 {code}",
        "category": "连衣裙",
        "available_stock": stock,
        "cost_price": cost,
        "list_price": cost / (1 - margin) if margin < 1 else cost,
        "margin_rate": margin,
        "rating": rating,
        "sold_30d": 80,
        "return_rate": 0.02,
    }


def test_rank_candidates_registered_and_callable(clean_registry):
    spec = registry.get("selection.rank_candidates")
    assert spec.read_only is True  # 纯计算，不产生副作用

    data = spec.handler(
        candidates=[_candidate(), _candidate("SPU-002", stock=5)],
        top_n=1,
        min_stock=30,
    )
    assert data["passed"] == 1
    assert data["filtered_out"] == 1
    assert data["selected"][0]["spu_code"] == "SPU-001"
    assert data["selected"][0]["rank"] == 1
    assert data["selected"][0]["reason"]  # 推荐理由必须引用具体数字


def test_rank_candidates_rejects_all(clean_registry):
    spec = registry.get("selection.rank_candidates")
    data = spec.handler(
        candidates=[_candidate(stock=1)],  # 库存低于下限
        top_n=3,
    )
    assert data["selected"] == []
    assert "无可用候选" in data["summary"]


def test_detect_alerts_registered_and_callable(clean_registry):
    spec = registry.get("analytics.detect_alerts")
    assert spec.read_only is True

    totals = {
        "gmv": 100000.0,
        "order_count": 100,
        "customer_count": 40,
        "avg_order_value": 1000.0,
        "refund_amount": 15000.0,
        "return_rate": 0.15,
    }
    data = spec.handler(
        totals=totals,
        by_category=[
            # 60 单（≥ MIN_ORDERS_FOR_JUDGEMENT 才下判定）且退货率 35%
            # 越过 0.30 红线 → critical
            {"group": "连衣裙", "gmv": 80000.0, "order_count": 60,
             "customer_count": 25, "avg_order_value": 1333.33,
             "refund_amount": 13000.0, "return_rate": 0.35}
        ],
        by_channel=[],
    )
    assert data["critical_count"] >= 1
    assert data["has_critical"] is True
    levels = {a["level"] for a in data["alerts"]}
    assert levels <= {"critical", "warning", "info"}


def test_render_business_report_creates_asset(clean_registry):
    spec = registry.get("analytics.render_business_report")
    assert spec.read_only is False
    assert spec.idempotent is True

    by_category = {
        "days": 30,
        "totals": {
            "gmv": 100000.0,
            "order_count": 100,
            "customer_count": 40,
            "avg_order_value": 1000.0,
            "refund_amount": 15000.0,
            "return_rate": 0.15,
        },
        "groups": [
            {
                "group": "连衣裙",
                "gmv": 80000.0,
                "order_count": 60,
                "customer_count": 25,
                "avg_order_value": 1333.33,
                "refund_amount": 13000.0,
                "return_rate": 0.21,
            }
        ],
    }
    data = spec.handler(by_category=by_category, by_channel={}, days=30, task_no="T-001")

    assert data["asset_no"].startswith("AS")  # AS + 日期 + 序号
    assert data["sheet_names"] == ["类目汇总", "渠道汇总", "整体"]
    assert "T-001" in data["filename"]
    assert data["url"]
