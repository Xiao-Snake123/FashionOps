"""Agent 层测试：状态定义、预算控制、checkpointer，以及真实的 HITL 中断恢复。

这里最有价值的不是断言本身，而是**真的把一个 LangGraph 图跑起来**：
带 ``interrupt()`` 的图跑一半停下来，用同一个 ``thread_id`` 接着跑完。
这是"任务中断可恢复"这个技术点唯一有说服力的验证方式 ——
光测 ``new_state()`` 返回的字典长得对不对，说明不了任何事。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from fashionops.agents import (
    ApprovalRequest,
    Artifact,
    BudgetExceeded,
    RunContext,
    TaskState,
    budget_status,
    check_budget,
    get_checkpointer,
    get_context,
    latest_state,
    new_state,
    pending_nodes,
    reset_checkpointer_cache,
    thread_config,
)
from fashionops.config import Settings


# ===========================================================================
# 状态与预算
# ===========================================================================
class TestState:
    def test_new_state_has_sane_defaults(self):
        state = new_state(task_no="T001", user_request="帮我选 3 款秋季女装")
        assert state["task_no"] == "T001"
        assert state["thread_id"] == "T001", "thread_id 约定就用 task_no"
        assert state["status"] == "running"
        assert state["steps"] == []
        assert state["budget_tokens"] > 0

    def test_defaults_are_not_shared_between_instances(self):
        """默认值里的列表必须是**每次新建**的，否则两个任务的轨迹会串在一起。"""
        a = new_state(task_no="A", user_request="x")
        b = new_state(task_no="B", user_request="y")
        a["steps"].append("污染")  # type: ignore[union-attr]
        assert b["steps"] == []


class TestBudget:
    def test_fresh_state_is_well_within_budget(self):
        status = budget_status(new_state(task_no="T", user_request="x"))
        assert not status["exhausted"]
        assert not status["warning"]
        assert status["ratio"] == 0.0

    def test_warning_threshold(self):
        state = new_state(task_no="T", user_request="x", budget_tokens=100)
        state["total_tokens"] = 85
        status = budget_status(state)
        assert status["warning"]
        assert not status["exhausted"]

    def test_exhausted_when_tokens_used_up(self):
        state = new_state(task_no="T", user_request="x", budget_tokens=100)
        state["total_tokens"] = 100
        assert budget_status(state)["exhausted"]
        with pytest.raises(BudgetExceeded):
            check_budget(state)

    def test_cost_can_exhaust_budget_independently(self):
        """token 没超但钱超了也算超 —— 贵模型烧钱快，两个维度都要看。"""
        state = new_state(
            task_no="T", user_request="x",
            budget_tokens=1_000_000, budget_cost=Decimal("1.00"),
        )
        state["total_tokens"] = 1000
        state["total_cost"] = Decimal("1.50")
        assert budget_status(state)["exhausted"]

    def test_cost_alone_can_drive_the_ratio(self):
        """token 没超但钱超了也算超 —— 贵模型烧钱快，两个维度都要看。"""
        state = new_state(task_no="T", user_request="x", budget_cost=Decimal("0.003"))
        state["total_tokens"] = 0
        state["total_cost"] = Decimal("0.001")
        status = budget_status(state)
        assert status["used_cost"] == pytest.approx(0.001)
        # ratio 会被 round 到 4 位小数，所以用 abs 容差而不是 rel
        assert status["ratio"] == pytest.approx(1 / 3, abs=1e-4)


class TestRunContext:
    def test_context_is_not_part_of_state(self):
        """依赖对象绝不能进状态 —— 会把 checkpoint 写挂。"""
        assert "gateway" not in TaskState.__annotations__
        assert "model" not in TaskState.__annotations__
        assert "ctx" not in TaskState.__annotations__

    def test_get_context_from_config(self):
        ctx = RunContext(gateway=object(), model=object(), actor="test")
        config = thread_config("T001", ctx)
        assert get_context(config) is ctx

    def test_missing_context_gives_actionable_error(self):
        """默认的 KeyError 对调用方毫无帮助，这里要直接告诉他怎么写。"""
        with pytest.raises(RuntimeError, match="configurable"):
            get_context({"configurable": {"thread_id": "x"}})

    def test_none_config_also_raises_cleanly(self):
        with pytest.raises(RuntimeError, match="RunContext"):
            get_context(None)


# ===========================================================================
# Checkpointer
# ===========================================================================
class TestCheckpointer:
    def test_sqlite_backend_creates_file(self, tmp_path):
        settings = Settings(data_dir=tmp_path)
        get_checkpointer(settings, backend="sqlite")
        assert settings.checkpoint_db.exists()

    def test_memory_backend_is_available(self, tmp_path):
        saver = get_checkpointer(Settings(data_dir=tmp_path), backend="memory")
        assert saver is not None

    def test_saver_is_cached_per_backend(self, tmp_path):
        settings = Settings(data_dir=tmp_path)
        assert get_checkpointer(settings, backend="sqlite") is get_checkpointer(
            settings, backend="sqlite"
        )

    def test_redis_backend_falls_back_to_sqlite_when_unreachable(self, tmp_path):
        """Redis 连不上时不应该让应用起不来，而是降级到 SQLite。"""
        settings = Settings(
            data_dir=tmp_path,
            # 指向一个不存在的端口
            redis_url="redis://127.0.0.1:1/0",
        )
        saver = get_checkpointer(settings, backend="redis")
        assert saver is not None  # 拿到了降级后的 saver，而不是异常


# ===========================================================================
# 真图 + HITL 中断恢复
# ===========================================================================
def _build_graph(checkpointer):
    """一张最小的上新链路图：需求理解 → 选品 → 人工闸口 → 完成。"""

    def understand(state: TaskState) -> dict:
        return {
            "current_node": "understand",
            "progress": 15,
            "completed_nodes": ["understand"],
            "findings": {**state.get("findings", {}), "understand": {"intent": "新品上新"}},
        }

    def select(state: TaskState) -> dict:
        return {
            "current_node": "select",
            "progress": 60,
            "completed_nodes": ["select"],
            "selected": [{"spu_code": "SPU0001", "score": 87.4}],
            "artifacts": [
                Artifact(kind="report", name="选品分析.xlsx", ref="AS0001",
                         produced_by="selection")
            ],
            "total_tokens": state.get("total_tokens", 0) + 1200,
        }

    def gate(state: TaskState) -> dict:
        # 动态中断：图会在这里停下并把状态存进 checkpointer
        decision = interrupt(
            {
                "action": "把选出的 3 款商品上架到抖音小店",
                "reason": "上架是高危写操作，必须人工确认",
                "risk": "high",
            }
        )
        return {
            "current_node": "gate",
            "progress": 85,
            "completed_nodes": ["gate"],
            "pending_approval": False,
            "result": {"approved": decision},
        }

    builder = StateGraph(TaskState)
    builder.add_node("understand", understand)
    builder.add_node("select", select)
    builder.add_node("gate", gate)
    builder.add_edge(START, "understand")
    builder.add_edge("understand", "select")
    builder.add_edge("select", "gate")
    builder.add_edge("gate", END)
    return builder.compile(checkpointer=checkpointer)


class TestGraphWithHitl:
    def test_graph_runs_to_interrupt_and_persists(self, tmp_path):
        settings = Settings(data_dir=tmp_path)
        saver = get_checkpointer(settings, backend="sqlite")
        graph = _build_graph(saver)

        state = new_state(task_no="T260911001", user_request="帮我选 3 款秋季女装并上架")
        config = thread_config("T260911001", RunContext(gateway=None, model=None, actor="user"))

        result = graph.invoke(state, config)

        # 图确实停了
        assert "__interrupt__" in result

        # 注意：current_node 是 **select**，不是 gate。
        # 因为 gate 里的 interrupt() 是抛异常中断的，节点末尾那句
        # `return {"current_node": "gate"}` 根本没机会执行。
        # 想知道"停在哪个节点"必须看 checkpoint 的 next 字段。
        assert result["current_node"] == "select"
        assert result["completed_nodes"] == ["understand", "select"]
        assert result["selected"] == [{"spu_code": "SPU0001", "score": 87.4}]

        # 这才是"停在哪个节点"的正确读法
        assert pending_nodes(graph, "T260911001") == ["gate"]

        # 状态确实落盘了
        persisted = latest_state(saver, "T260911001")
        assert persisted is not None
        assert persisted["user_request"] == "帮我选 3 款秋季女装并上架"
        assert persisted["selected"] == [{"spu_code": "SPU0001", "score": 87.4}]

    def test_resume_from_interrupt_completes_task(self, tmp_path):
        """核心验证：断点续跑。这正是 checkpointer 存在的意义。"""
        settings = Settings(data_dir=tmp_path)
        saver = get_checkpointer(settings, backend="sqlite")
        graph = _build_graph(saver)

        config = thread_config("T260911002", RunContext(gateway=None, model=None, actor="user"))
        graph.invoke(new_state(task_no="T260911002", user_request="上新"), config)
        assert pending_nodes(graph, "T260911002") == ["gate"]

        # 人工批准后恢复
        final = graph.invoke(Command(resume=True), config)

        assert final["current_node"] == "gate"
        assert final["progress"] == 85
        assert final["result"] == {"approved": True}
        assert final["pending_approval"] is False
        assert pending_nodes(graph, "T260911002") == []
        # 前面节点累积的数据没有丢 —— 它们是从 checkpoint 读回来的，不是重跑的
        assert final["selected"] == [{"spu_code": "SPU0001", "score": 87.4}]
        assert final["completed_nodes"] == ["understand", "select", "gate"]

    def test_can_reject_at_gate(self, tmp_path):
        settings = Settings(data_dir=tmp_path)
        saver = get_checkpointer(settings, backend="sqlite")
        graph = _build_graph(saver)

        config = thread_config("T260911003", RunContext(gateway=None, model=None, actor="user"))
        graph.invoke(new_state(task_no="T260911003", user_request="上新"), config)

        final = graph.invoke(Command(resume=False), config)
        assert final["result"] == {"approved": False}

    def test_state_survives_new_checkpointer_instance(self, tmp_path):
        """换一个 checkpointer 实例（相当于换了进程）还能读到状态。

        这是"崩溃后恢复"的等价实验：进程没了，状态还在。
        """
        settings = Settings(data_dir=tmp_path)
        saver1 = get_checkpointer(settings, backend="sqlite")
        graph = _build_graph(saver1)
        config = thread_config("T260911004", RunContext(gateway=None, model=None, actor="user"))
        graph.invoke(new_state(task_no="T260911004", user_request="上新"), config)

        # 模拟进程重启：清掉缓存，重新构造 saver
        reset_checkpointer_cache()
        saver2 = get_checkpointer(settings, backend="sqlite")
        assert saver2 is not saver1, "缓存没清干净，这个测试就失去意义了"

        restored = latest_state(saver2, "T260911004")
        assert restored is not None
        assert restored["selected"] == [{"spu_code": "SPU0001", "score": 87.4}]
        assert restored["total_tokens"] == 1200

        # 用新的 saver 重新构图，确认它也能看到中断点
        assert pending_nodes(_build_graph(saver2), "T260911004") == ["gate"]

    def test_reducer_accumulates_instead_of_replacing(self, tmp_path):
        """``completed_nodes`` 用 operator.add 做 reducer。

        如果用普通赋值，每个节点都会把上一个节点的记录覆盖掉，
        最后只剩最后一个节点 —— 那样"执行到了哪几步"就永远查不清。
        """
        settings = Settings(data_dir=tmp_path)
        saver = get_checkpointer(settings, backend="sqlite")
        graph = _build_graph(saver)

        config = thread_config("T260911005", RunContext(gateway=None, model=None, actor="user"))
        result = graph.invoke(new_state(task_no="T260911005", user_request="上新"), config)

        assert result["completed_nodes"] == ["understand", "select"]
        assert len(result["artifacts"]) == 1
        assert result["artifacts"][0].ref == "AS0001"
        assert result["total_tokens"] == 1200

    def test_pending_nodes_is_empty_for_unstarted_task(self, tmp_path):
        saver = get_checkpointer(Settings(data_dir=tmp_path), backend="sqlite")
        assert pending_nodes(_build_graph(saver), "从来没有跑过的任务") == []

    def test_unknown_thread_returns_none(self, tmp_path):
        saver = get_checkpointer(Settings(data_dir=tmp_path), backend="sqlite")
        assert latest_state(saver, "不存在的任务") is None


# ===========================================================================
# 结构化对象
# ===========================================================================
class TestStructuredObjects:
    def test_artifact_carries_provenance(self):
        """产出物要能回答"这是谁在什么时候产出的" —— 排查问题全靠它。"""
        artifact = Artifact(kind="image", name="主图.jpg", ref="AS0001", produced_by="creative")
        assert artifact.produced_by == "creative"
        assert artifact.produced_at is not None

    def test_approval_request_says_what_and_why(self):
        """审批请求必须写清"批什么"和"为什么" —— 只说"需要确认"，人没法决策。"""
        request = ApprovalRequest(
            node="ads",
            action="把日预算从 800 调到 1500",
            reason="ROI 3.8 高于阈值 2.5，加预算能放大收益",
            risk="high",
            payload={"from": 800, "to": 1500},
        )
        assert request.approved is None
        assert request.payload["to"] == 1500
