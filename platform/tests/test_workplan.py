"""`agents/workplan.py` 的单元测试。

两条核心用例直接来自交接文档 T2 的验收标准 ——
尤其是"挑 5 款适合上新的秋季连衣裙必须只有 1 步、不能出现 notify"，
那是 §1 问题 2（模型自己加了一步用户没要的发邮件）的回归测试。
"""

from __future__ import annotations

from fashionops.agents.workplan import TodoSpec, matched_kinds, plan_as_text, plan_todos


def test_selection_only_request_is_single_step():
    """验收用例 1：只读的选品问题 → 1 步，**不能**出现 notify。"""
    todos = plan_todos("挑 5 款适合上新的秋季连衣裙")

    assert len(todos) == 1
    assert todos[0].kind == "selection"
    assert todos[0].seq == 1
    assert todos[0].approval_required is False
    # 不存在任何 notification / 发信相关的步骤
    assert all(t.kind != "notification" for t in todos)


def test_multi_kind_request_is_three_steps():
    """验收用例 2：选品 + 报表 + 发信 → 3 步，固定业务顺序。"""
    todos = plan_todos("帮我选 3 款秋季女装，再出份报表发给 ops@example.com")

    assert [t.kind for t in todos] == ["selection", "analytics", "notification"]
    assert [t.seq for t in todos] == [1, 2, 3]
    # 只有发邮件那步要过人工闸口
    assert [t.approval_required for t in todos] == [False, False, True]


def test_order_is_business_fixed_not_keyword_order():
    """关键词出现先后不影响拆分顺序（交接文档 D5）。"""
    forward = plan_todos("帮我选 3 款女装，再出报表发给老板")
    backward = plan_todos("把报表发给老板，另外帮我选 3 款女装")

    assert [t.kind for t in forward] == [t.kind for t in backward]


def test_unknown_request_degrades_to_single_step():
    """认不出来就单步，**不编步骤**（"我不知道"比"我编一个"便宜）。"""
    todos = plan_todos("明天天气怎么样")

    assert len(todos) == 1
    assert todos[0].kind == "generic"
    assert todos[0].approval_required is False


def test_notification_only_single_step_requires_approval():
    """只要发一封邮件 → 单步，但发出去收不回来，仍要过闸口。"""
    todos = plan_todos("给 ops@example.com 发一封邮件说明情况")

    assert len(todos) == 1
    assert todos[0].kind == "notification"
    assert todos[0].approval_required is True


def test_single_step_goal_is_user_request_verbatim():
    """单步的 goal 用用户原话 —— 没有比原文更忠实的表述。"""
    todos = plan_todos("挑 5 款适合上新的秋季连衣裙")
    assert todos[0].goal == "挑 5 款适合上新的秋季连衣裙"


def test_matched_kinds_ordered_by_business_sequence():
    assert matched_kinds("选款、出报表、发邮件") == [
        "selection",
        "analytics",
        "notification",
    ]


def test_plan_as_text_marks_approval_steps():
    todos = [
        TodoSpec(seq=1, goal="a", kind="selection", approval_required=False),
        TodoSpec(seq=2, goal="b", kind="notification", approval_required=True),
    ]
    assert plan_as_text(todos) == "1.selection → 2.notification*"
    assert plan_as_text([]) == "（无计划）"
