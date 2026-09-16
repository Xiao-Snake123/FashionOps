"""`agents/loop.py` 的测试 —— 新执行架构的核心验收。

两条最关键的用例来自交接文档 T4/T5：

1. 提交「挑 5 款适合上新的秋季连衣裙」→ **只跑 1 步、不出现发信工具、
   不产生任何审批闸口**。这是 §1 问题 2 的回归：旧架构里模型会自己
   加一步"把结果发邮件"，于是跑完选品就停下问人要不要发邮件。
2. 跑到 `mail.send` → **落审批单、任务停在 waiting_approval、线程结束**，
   而不是把线程挂在那里等（D6）。批准后 resume 能接着跑完。

模型用 `llm/mock.py` 的脚本化假件（`MockReply`），所以整条链路
（规划 → 裁工具目录 → 过网关 → 落库）都是真的，只有"模型说什么"是假的。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

import fashionops.db.base as db_base
from fashionops.agents.loop import TASK_AGENT_NAME, _catalog_for, run_task
from fashionops.agents.recorder import TaskRecorder
from fashionops.config import Settings
from fashionops.db.base import Base, build_engine, reset_engine
from fashionops.db.models import Task, TaskEvent, TaskTodo
from fashionops.db.models_chat import APPROVAL_STATUS_APPROVED, Approval
from fashionops.llm.mock import MockModelClient, MockReply
from fashionops.llm.types import ToolCall
from fashionops.tools import AGENT_SCOPES


@pytest.fixture
def db(tmp_path, monkeypatch):  # noqa: ANN001, ANN201
    """临时 SQLite 库。

    和 `tests/test_chat.py` 的 `db` 夹具同一套做法：替换
    `db.base.get_settings`，让 `session_scope()` 落在临时库上 ——
    不这么做的测试会打到开发库，跑一次污染一次。
    """
    settings = Settings(data_dir=tmp_path, db_host=None)
    reset_engine()
    monkeypatch.setattr(db_base, "get_settings", lambda: settings)

    engine = build_engine(settings)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()
    reset_engine()


@pytest.fixture(autouse=True)
def tools_registered():
    from fashionops.gateway.registry import registry
    from fashionops.tools import register_all_tools

    if not len(registry):
        register_all_tools(registry)


def _new_task(task_no: str, request: str) -> None:
    from fashionops.agents.execution import create_task_row

    create_task_row(
        task_no, user_request=request, task_type="generic", created_by="pytest"
    )


def _candidate(code: str = "SPU-001") -> dict:
    return {
        "spu_code": code,
        "title": f"秋季连衣裙 {code}",
        "category": "连衣裙",
        "available_stock": 120,
        "cost_price": 100.0,
        "list_price": 260.0,
        "margin_rate": 0.55,
        "rating": 4.7,
        "sold_30d": 90,
        "season": "秋季",
    }


def _todo_rows(bind, task_no: str) -> list[TaskTodo]:
    with Session(bind) as session:
        task = session.scalar(select(Task).where(Task.task_no == task_no))
        assert task is not None
        return list(
            session.scalars(
                select(TaskTodo).where(TaskTodo.task_id == task.id).order_by(TaskTodo.seq)
            ).all()
        )


def _task(bind, task_no: str) -> Task:
    with Session(bind) as session:
        task = session.scalar(select(Task).where(Task.task_no == task_no))
        assert task is not None
        session.expunge(task)
        return task


def _approvals(bind, task_no: str) -> list[Approval]:
    with Session(bind) as session:
        return list(session.scalars(select(Approval).where(Approval.task_no == task_no)).all())


def _tool_call_count(bind, task_no: str, tool: str) -> int:
    """事件流里某个工具被**执行**了几次。

    用事件流而不是 mock 的调用记录：事件流是系统的留痕，它同时证明了
    "执行过"和"只执行过一次"（邮件不能发两遍）。
    """
    with Session(bind) as session:
        task = session.scalar(select(Task).where(Task.task_no == task_no))
        assert task is not None
        rows = session.scalars(
            select(TaskEvent).where(
                TaskEvent.task_id == task.id, TaskEvent.event_type == "tool_call"
            )
        ).all()
        return sum(1 for r in rows if (r.payload or {}).get("tool") == tool)


# ===========================================================================
# 验收 1：只读的选品需求 → 一步，且看不到发信工具
# ===========================================================================
def test_selection_only_runs_one_step_without_approval(db):
    task_no = "T-LOOP-001"
    request = "挑 5 款适合上新的秋季连衣裙"
    _new_task(task_no, request)

    client = MockModelClient(
        script=[
            MockReply(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="selection__rank_candidates",
                        arguments={"candidates": [_candidate()], "top_n": 1},
                    )
                ]
            ),
            MockReply(content="推荐 SPU-001：近 30 天销量 90 件，毛利率 55%。"),
        ]
    )

    outcome = run_task(
        task_no,
        user_request=request,
        scopes=AGENT_SCOPES[TASK_AGENT_NAME],
        model=client,
        recorder=TaskRecorder(),
    )

    assert outcome["status"] == "succeeded"

    rows = _todo_rows(db, task_no)
    assert len(rows) == 1, "只读的选品需求必须只有一步（不能被拆出多余步骤）"
    assert rows[0].kind == "selection"
    assert rows[0].status == "done"
    assert rows[0].tool_names == ["selection.rank_candidates"]

    # **核心回归**：没有任何审批闸口 —— 用户没要求发信，就不该有发信的闸口
    assert _approvals(db, task_no) == []
    assert _task(db, task_no).status == "succeeded"

    # 这一步的工具目录里**没有** mail.send：注意力管理，也避免模型自作主张发信
    names = {schema["function"]["name"] for schema in _catalog_for("selection", AGENT_SCOPES[TASK_AGENT_NAME])}
    assert "mail__send" not in names
    assert "selection__rank_candidates" in names


# ===========================================================================
# 验收 2：发信前挂起 → 落审批单 → 线程结束（不阻塞）
# ===========================================================================
def test_notification_step_pauses_with_approval_and_releases(db):
    task_no = "T-LOOP-002"
    request = "帮我选 1 款秋季连衣裙，然后发给 ops@example.com"
    _new_task(task_no, request)

    client = MockModelClient(
        script=[
            # 第 1 步：选品
            MockReply(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="selection__rank_candidates",
                        arguments={"candidates": [_candidate()], "top_n": 1},
                    )
                ]
            ),
            MockReply(content="已选出 SPU-001。"),
            # 第 2 步：发信 —— 会在执行前被闸口拦下
            MockReply(
                tool_calls=[
                    ToolCall(
                        id="c2",
                        name="mail__send",
                        arguments={
                            "to_addrs": ["ops@example.com"],
                            "subject": "秋季连衣裙选品结果",
                            "body_text": "附件是本次选品结果。",
                        },
                    )
                ]
            ),
        ]
    )

    outcome = run_task(
        task_no,
        user_request=request,
        scopes=AGENT_SCOPES[TASK_AGENT_NAME],
        model=client,
        recorder=TaskRecorder(),
    )

    # 线程结束了、函数返回了 —— 这是 D6（审批不阻塞线程）的落点
    assert outcome["status"] == "waiting_approval"

    rows = _todo_rows(db, task_no)
    assert [r.kind for r in rows] == ["selection", "notification"]
    assert rows[0].status == "done"
    assert rows[1].status == "waiting_approval"
    assert rows[1].approval_no

    task = _task(db, task_no)
    assert task.status == "waiting_approval"
    assert task.approval_payload, "任务上要挂审批载荷，前端据此渲染确认卡片"

    approvals = _approvals(db, task_no)
    assert len(approvals) == 1
    approval = approvals[0]
    assert approval.tool_name == "mail.send"
    assert approval.todo_seq == 2
    # 人话摘要必须已经把收件人摆出来了（审批要成立，前提是看得懂）
    assert "ops@example.com" in (approval.preview or "")


# ===========================================================================
# 批准后续跑
# ===========================================================================
def test_resume_after_approval_finishes_task(db):
    from fashionops.agents.loop import resume_task

    task_no = "T-LOOP-003"
    request = "帮我选 1 款秋季连衣裙，然后发给 ops@example.com"
    _new_task(task_no, request)

    pause_client = MockModelClient(
        script=[
            MockReply(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="selection__rank_candidates",
                        arguments={"candidates": [_candidate()], "top_n": 1},
                    )
                ]
            ),
            MockReply(content="已选出 SPU-001。"),
            MockReply(
                tool_calls=[
                    ToolCall(
                        id="c2",
                        name="mail__send",
                        arguments={
                            "to_addrs": ["ops@example.com"],
                            "subject": "选品结果",
                            "body_text": "见附件。",
                        },
                    )
                ]
            ),
        ]
    )
    run_task(
        task_no,
        user_request=request,
        scopes=AGENT_SCOPES[TASK_AGENT_NAME],
        model=pause_client,
        recorder=TaskRecorder(),
    )

    # 恢复时模型只需要写结论 —— 那封邮件是**已经执行过**的动作，
    # 绝不能让它重新调一次（邮件不能发两遍）
    resume_client = MockModelClient(script=[MockReply(content="邮件已发送给 ops@example.com。")])

    outcome = resume_task(
        task_no,
        decision={"approved": True, "by": "boss", "reason": "内容没问题"},
        scopes=AGENT_SCOPES[TASK_AGENT_NAME],
        model=resume_client,
        recorder=TaskRecorder(),
    )

    assert outcome["status"] == "succeeded"

    approval = _approvals(db, task_no)[0]
    assert approval.status == APPROVAL_STATUS_APPROVED
    assert approval.decided_by == "boss"
    assert approval.execution_result is not None, "批准后执行成什么了必须留痕"

    # 恢复时不应该再出现第二次 mail.send 调用
    assert _tool_call_count(db, task_no, "mail.send") == 1

    rows = _todo_rows(db, task_no)
    assert [r.status for r in rows] == ["done", "done"]
    assert _task(db, task_no).status == "succeeded"


def test_reject_cancels_task_and_skips_downstream(db):
    from fashionops.agents.loop import resume_task

    task_no = "T-LOOP-004"
    request = "帮我选 1 款秋季连衣裙，然后发给 ops@example.com"
    _new_task(task_no, request)

    client = MockModelClient(
        script=[
            MockReply(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="selection__rank_candidates",
                        arguments={"candidates": [_candidate()], "top_n": 1},
                    )
                ]
            ),
            MockReply(content="已选出 SPU-001。"),
            MockReply(
                tool_calls=[
                    ToolCall(
                        id="c2",
                        name="mail__send",
                        arguments={
                            "to_addrs": ["ops@example.com"],
                            "subject": "选品结果",
                            "body_text": "见附件。",
                        },
                    )
                ]
            ),
        ]
    )
    run_task(
        task_no,
        user_request=request,
        scopes=AGENT_SCOPES[TASK_AGENT_NAME],
        model=client,
        recorder=TaskRecorder(),
    )

    outcome = resume_task(
        task_no,
        decision={"approved": False, "by": "boss", "reason": "收件人写错了"},
        scopes=AGENT_SCOPES[TASK_AGENT_NAME],
        model=MockModelClient(script=[]),
        recorder=TaskRecorder(),
    )

    assert outcome["status"] == "cancelled"
    rows = _todo_rows(db, task_no)
    # 被驳回的那一步没做成，后面几步的输入就缺一块 → 全部 skipped，不能硬跑
    assert [r.status for r in rows] == ["done", "skipped"]
    assert _task(db, task_no).status == "cancelled"
