"""记录器测试 —— 事件流 / agent_runs / 任务主表。

这些用例原来长在 `test_orchestrator.py` 里（那个文件随旧执行架构一起删了），
断言的内容**一条没减**，只是驱动方式从 ``TaskRunner.submit/resume``
换成了 ``loop.run_task/resume_task``：记录器是旁路，不该和"谁在跑"绑死。

其中 ``test_seq_is_continuous_across_recorder_instances`` 是一条**真实 bug
的回归**（seq 撞唯一约束导致"批准之后的事件全丢了"，而日志里只有一行
warning）—— 新架构里 resume 仍然是"新进程新 recorder"，这个坑还在，
所以这条用例必须留着。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fashionops.agents.loop import run_task
from fashionops.agents.recorder import (
    TaskRecorder,
    read_events,
    task_snapshot,
)
from fashionops.db.models import AgentRun, Task
from fashionops.db.models_chat import Approval
from fashionops.llm.mock import MockModelClient, MockReply
from fashionops.llm.types import ToolCall
from fashionops.tools import AGENT_SCOPES

#: 一个"选品 + 发信"的需求：第一步能跑完，第二步一定挂在审批上。
#: 用它来观察 resume 前后的记录行为。
_REQUEST = "帮我选 1 款秋季连衣裙，然后发给 ops@example.com"


def _candidate() -> dict:
    return {
        "spu_code": "SPU-001",
        "title": "秋季连衣裙 SPU-001",
        "category": "连衣裙",
        "available_stock": 120,
        "cost_price": 100.0,
        "list_price": 260.0,
        "margin_rate": 0.55,
        "rating": 4.7,
        "sold_30d": 90,
    }


def _client() -> MockModelClient:
    return MockModelClient(
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


def _new_task(task_no: str, request: str = _REQUEST) -> None:
    from fashionops.agents.execution import create_task_row

    create_task_row(task_no, user_request=request, task_type="generic", created_by="pytest")


def test_task_and_events_persisted(db):
    task_no = "R-LOOP-1"
    _new_task(task_no)

    run_task(
        task_no,
        user_request=_REQUEST,
        scopes=AGENT_SCOPES["task"],
        model=_client(),
        recorder=TaskRecorder(),
    )

    snap = task_snapshot(task_no)
    assert snap and snap["status"] == "waiting_approval"
    assert snap["dag"], "计划要写进 tasks.dag，界面靠它显示步骤"

    kinds = [e["event_type"] for e in read_events(task_no)]
    assert "task_started" in kinds
    assert "task_planned" in kinds
    assert "approval_required" in kinds
    assert "todo_started" in kinds
    assert "tool_call" in kinds
    # 还没结束：任务停在闸口上，不该有 task_finished
    assert "task_finished" not in kinds


def test_agent_runs_recorded_for_the_task_agent(db):
    """``agent_runs`` 是评测数据源：每一步一条，agent_name 都是 task。"""
    task_no = "R-LOOP-2"
    _new_task(task_no)

    run_task(
        task_no,
        user_request=_REQUEST,
        scopes=AGENT_SCOPES["task"],
        model=_client(),
        recorder=TaskRecorder(),
    )

    with Session(db) as session:
        task = session.scalar(select(Task).where(Task.task_no == task_no))
        rows = session.scalars(select(AgentRun).where(AgentRun.task_id == task.id)).all()

    # 第一步跑完 + 第二步挂起，各记一条
    assert [r.agent_name for r in rows] == ["task", "task"]
    assert rows[0].status == "success"
    assert rows[1].status == "success"  # pending → success（见 recorder 的映射表）
    # 时间与 token 是评测要的四个数字的基础
    assert all(r.latency_ms is not None for r in rows)
    assert rows[0].tool_call_count >= 1


def test_seq_is_continuous_across_recorder_instances(db):
    """**回归真实 bug**：resume 时新建 recorder 让 seq 从 1 重来。

    撞 ``uq_task_event_seq`` 唯一约束 → 插入失败被 ``_safe`` 吞掉 →
    表现是"批准之后的所有事件都不见了"，日志里只有一行 warning。

    这条用例锁住两件事：seq 连续，以及没有静默写入失败。
    """
    from fashionops.agents.loop import resume_task

    task_no = "R-LOOP-3"
    _new_task(task_no)

    run_task(
        task_no,
        user_request=_REQUEST,
        scopes=AGENT_SCOPES["task"],
        model=_client(),
        recorder=TaskRecorder(),
    )
    before = len(read_events(task_no))

    # 全新的 recorder 实例（模拟"续跑是新线程、新对象"）
    recorder2 = TaskRecorder()
    resume_task(
        task_no,
        decision={"approved": True, "by": "boss"},
        scopes=AGENT_SCOPES["task"],
        model=MockModelClient(script=[MockReply(content="邮件已发送。")]),
        recorder=recorder2,
    )

    events = read_events(task_no)
    seqs = [e["seq"] for e in events]
    assert len(events) > before, "resume 之后的事件被丢了"
    assert seqs == list(range(1, len(seqs) + 1)), f"seq 不连续：{seqs}"
    assert recorder2.write_failures == 0, "有记录被静默丢弃"


def test_write_failures_are_counted(monkeypatch):
    """记录失败不能带崩任务，但**必须可被检测**。"""
    recorder = TaskRecorder()
    monkeypatch.setattr(
        "fashionops.agents.recorder.session_scope",
        lambda: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    recorder.event("whatever", "x")  # 不该抛异常
    assert recorder.write_failures == 1


def test_finish_clears_approval_payload(db):
    """任务结束（含被驳回）后要把待审批载荷清掉 ——
    否则界面上一直挂着一个"待确认"的红点，而任务早就结束了。"""
    from fashionops.agents.loop import resume_task

    task_no = "R-LOOP-4"
    _new_task(task_no)
    run_task(
        task_no,
        user_request=_REQUEST,
        scopes=AGENT_SCOPES["task"],
        model=_client(),
        recorder=TaskRecorder(),
    )
    assert (task_snapshot(task_no) or {})["approval_payload"]

    resume_task(
        task_no,
        decision={"approved": False, "by": "boss", "reason": "不需要发"},
        scopes=AGENT_SCOPES["task"],
        model=MockModelClient(script=[]),
        recorder=TaskRecorder(),
    )

    snap = task_snapshot(task_no)
    assert snap and snap["status"] == "cancelled"
    assert snap["approval_payload"] is None


def test_null_recorder_writes_nothing(db):
    """``enabled=False`` 时一行都不写 —— 脚本/演示用得上。"""
    task_no = "R-LOOP-5"
    _new_task(task_no, "挑 2 款秋季连衣裙")

    run_task(
        task_no,
        user_request="挑 2 款秋季连衣裙",
        scopes=AGENT_SCOPES["task"],
        model=MockModelClient(
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
                MockReply(content="推荐 SPU-001。"),
            ]
        ),
        recorder=TaskRecorder(enabled=False),
    )

    with Session(db) as session:
        # task_events / agent_runs 一条都不该有
        assert session.scalar(select(func.count()).select_from(AgentRun)) == 0
        assert session.scalar(select(func.count()).select_from(Approval)) == 0
