"""`api/schemas.py` 的单元测试。

这里测的都是"**历史脏数据不能把接口打挂**"这一类问题 ——
它们只在**某一行数据落进分页区间**时才现形，所以靠点界面很难发现，
而一旦出现就是整页 500。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fashionops.api.schemas import TaskOut


def _task(**overrides) -> TaskOut:
    payload = {
        "id": 1,
        "task_no": "T1",
        "title": "标题",
        "user_request": "需求",
        "task_type": "generic",
        "status": "succeeded",
        "progress": 100,
        "llm_calls": 1,
        "total_tokens": 10,
        "total_cost": Decimal("0.01"),
        "created_at": datetime(2026, 9, 17, 12, 0, 0),
    }
    payload.update(overrides)
    return TaskOut.model_validate(payload)


def test_result_as_string_null_is_tolerated():
    """历史行把 result 存成了字符串 "null" —— 不能因此 500。"""
    assert _task(result="null").result is None


def test_result_as_json_string_is_parsed():
    assert _task(result='{"summary": "已完成"}').result == {"summary": "已完成"}


def test_result_garbage_becomes_none():
    assert _task(result="不是 JSON").result is None


def test_result_array_is_dropped():
    """声明的是 dict，拿到数组就当没有 —— 宁可少显示，也不要 500。"""
    assert _task(result="[1, 2, 3]").result is None


def test_normal_dict_passthrough():
    assert _task(result={"summary": "x"}).result == {"summary": "x"}


def test_dag_legacy_shape_is_normalized():
    """旧形状 `{nodes, edges}` 要在出口归一化成 `{steps}`（列表和详情同一种）。"""
    out = _task(dag={"nodes": [{"id": "a", "name": "选品"}], "edges": []})
    assert out.dag is not None
    assert out.dag["legacy"] is True
    assert out.dag["steps"][0]["step_id"] == "a"
