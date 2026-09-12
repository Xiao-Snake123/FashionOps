"""编排层测试：计划校验 + StateGraph + HITL + 任务落库。

分四层，和前几个 Agent 的测试结构一致：

1. **计划校验是纯函数** —— 每个拒绝分支一条用例
2. **失败传播是纯函数** —— 上游挂了，下游必须被跳过
3. **图端到端** —— 真实 SQLite 临时库 + 真实网关 + mock 模型 + 内存 checkpointer
4. **HITL 与落库** —— interrupt / resume / 驳回，以及事件流与 agent_runs 真的写进去了

第 4 层里有一条用例是专门回归一个真实 bug 的：
**resume 时新建的 recorder 让 seq 从 1 重新开始，撞唯一约束，事件被静默丢弃。**
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fashionops.agents.checkpoint import (
    get_checkpointer,
    latest_state,
    reset_checkpointer_cache,
    thread_config,
)
from fashionops.agents.orchestrator import build_orchestrator
from fashionops.agents.planner import (
    EXECUTABLE_AGENTS,
    PlanIssue,
    PlannedStep,
    TaskPlan,
    blocked_steps,
    build_plan,
    default_plan_for,
    requested_agents,
    topological_order,
    validate_plan,
)
from fashionops.agents.recorder import TaskRecorder, read_events, task_snapshot
from fashionops.agents.runner import TaskRunner
from fashionops.agents.state import RunContext, new_state
from fashionops.db.base import Base, build_engine, reset_engine, utcnow
from fashionops.db.models import (
    SKU,
    AgentRun,
    Competitor,
    Inventory,
    Order,
    Product,
    Task,
)
from fashionops.gateway import (
    IdempotencyStore,
    MemoryCircuitBreaker,
    MemoryRateLimiter,
    ToolGateway,
    ToolRegistry,
)
from fashionops.llm import MockModelClient, MockReply
from fashionops.tools import AGENT_SCOPES, register_all_tools


# ===========================================================================
# 1. 计划校验（纯函数）
# ===========================================================================
def _plan(*steps: dict) -> TaskPlan:
    return TaskPlan(reasoning="t", steps=[PlannedStep(**s) for s in steps])


def _codes(issues: list[PlanIssue]) -> list[str]:
    return [i.code for i in issues]


class TestPlanValidation:
    def test_valid_plan_passes(self):
        plan = _plan(
            {"step_id": "pick", "agent": "selection", "goal": "选款"},
            {"step_id": "report", "agent": "analytics", "goal": "出报表", "depends_on": ["pick"]},
        )
        assert validate_plan(plan) == []

    def test_unknown_agent_rejected(self):
        """模型编出不存在的 Agent 时必须拦下。

        ``agent`` 现在是 ``Literal``，Pydantic 在结构层就挡了一层；
        这里用 ``model_construct`` 绕过校验，模拟**从 checkpoint 读回的
        外部计划**那条路径 —— 那时已经不过 schema 了，所以语义校验必须有。
        """
        bypassed = PlannedStep.model_construct(
            step_id="x", agent="image_agent", goal="g", depends_on=[], approval_required=False
        )
        plan = TaskPlan.model_construct(reasoning="t", steps=[bypassed])
        issues = validate_plan(plan)
        assert _codes(issues) == ["unknown_agent"]
        assert "selection" in issues[0].hint  # hint 要列出可用值

    def test_agent_field_is_literal(self):
        """``agent`` 是 Literal —— 模型的 JSON schema 里就被约束住了。"""
        with pytest.raises(Exception):  # noqa: B017, PT011
            _plan({"step_id": "x", "agent": "not_an_agent", "goal": "g"})

    def test_duplicate_step_id(self):
        issues = validate_plan(
            _plan(
                {"step_id": "a", "agent": "selection", "goal": "g"},
                {"step_id": "a", "agent": "analytics", "goal": "g"},
            )
        )
        assert "duplicate_step_id" in _codes(issues)

    def test_duplicate_agent_rejected(self):
        """一个 Agent 只能出现一次 —— 图是静态构建的，出现两次路由分不清第几次。"""
        issues = validate_plan(
            _plan(
                {"step_id": "a", "agent": "selection", "goal": "g"},
                {"step_id": "b", "agent": "selection", "goal": "g"},
            )
        )
        assert "duplicate_agent" in _codes(issues)

    def test_unknown_dependency(self):
        issues = validate_plan(
            _plan({"step_id": "a", "agent": "selection", "goal": "g", "depends_on": ["zz"]})
        )
        assert _codes(issues) == ["unknown_dependency"]

    def test_self_dependency(self):
        issues = validate_plan(
            _plan({"step_id": "a", "agent": "selection", "goal": "g", "depends_on": ["a"]})
        )
        assert "self_dependency" in _codes(issues)

    def test_cycle_detected(self):
        issues = validate_plan(
            _plan(
                {"step_id": "a", "agent": "selection", "goal": "g", "depends_on": ["b"]},
                {"step_id": "b", "agent": "analytics", "goal": "g", "depends_on": ["a"]},
            )
        )
        assert _codes(issues) == ["cycle"]

    def test_step_id_pattern_enforced(self):
        with pytest.raises(Exception):  # noqa: B017, PT011
            _plan({"step_id": "Bad-Id", "agent": "selection", "goal": "g"})

    # ---- 用户点名要的能力不能被漏掉 -------------------------------------
    def test_requested_agents_from_keywords(self):
        wanted = requested_agents("帮我选 3 款女装，出报表发给 ops@a.com")
        assert set(wanted) == {"selection", "analytics", "notification"}

    def test_requested_agents_regex_handles_digits(self):
        """「选 3 款」中间有数字，纯子串匹配会漏掉 —— 这条踩过。"""
        assert "selection" in requested_agents("帮我选 3 款女装")

    def test_missing_requested_agent_rejected(self):
        """真实踩到的案例：模型把「选品 + 分析」合并成一个步骤。

        这种漏拆在结构上完全合法（无环、依赖存在、Agent 名合法），
        schema 校验抓不住 —— 但后果是报表没生成、下游节点失败。
        """
        plan = _plan(
            {"step_id": "a", "agent": "selection", "goal": "选品并分析"},
            {"step_id": "b", "agent": "notification", "goal": "发邮件", "depends_on": ["a"]},
        )
        issues = validate_plan(plan, user_request="选 2 款女装并出报表发给 ops@a.com")
        assert _codes(issues) == ["missing_requested_agent"]
        assert "analytics" in issues[0].message

    def test_no_false_positive_when_only_selection_requested(self):
        plan = _plan({"step_id": "a", "agent": "selection", "goal": "选款"})
        assert validate_plan(plan, user_request="帮我选 2 款女装") == []

    # ---- 拓扑排序 -------------------------------------------------------
    def test_topological_order_ignores_input_order(self):
        plan = _plan(
            {"step_id": "c", "agent": "notification", "goal": "g", "depends_on": ["b"]},
            {"step_id": "a", "agent": "selection", "goal": "g"},
            {"step_id": "b", "agent": "analytics", "goal": "g", "depends_on": ["a"]},
        )
        assert topological_order(plan.steps) == ["a", "b", "c"]

    def test_parallel_steps_keep_declared_order(self):
        """没有依赖关系的步骤按计划里的原始顺序排 —— 保证可复现。"""
        plan = _plan(
            {"step_id": "y", "agent": "analytics", "goal": "g"},
            {"step_id": "x", "agent": "selection", "goal": "g"},
        )
        assert topological_order(plan.steps) == ["y", "x"]

    def test_cycle_returns_none(self):
        plan = _plan(
            {"step_id": "a", "agent": "selection", "goal": "g", "depends_on": ["b"]},
            {"step_id": "b", "agent": "analytics", "goal": "g", "depends_on": ["a"]},
        )
        assert topological_order(plan.steps) is None


class TestDefaultPlan:
    @pytest.mark.parametrize(
        ("request_text", "expect"),
        [
            ("帮我选 3 款秋季女装", ["selection"]),
            ("出份经营报表", ["analytics"]),
            ("出一份日报邮件通知老板", ["analytics", "notification"]),
            ("帮我看看", ["selection", "analytics"]),  # 认不出来 → 最保守组合
        ],
    )
    def test_keyword_fallback(self, request_text, expect):
        plan = default_plan_for(request_text)
        assert [s.agent for s in plan.steps] == expect

    def test_order_is_business_correct_not_keyword_order(self):
        """不管关键词谁先出现，顺序都应是 选品 → 分析 → 通知。"""
        plan = default_plan_for("发给 ops@a.com，先出报表再选品")
        assert [s.agent for s in plan.steps] == ["analytics", "notification", "selection"][:0] + [
            "selection",
            "analytics",
            "notification",
        ]

    def test_notification_requires_approval(self):
        plan = default_plan_for("出报表发给 ops@a.com")
        note = next(s for s in plan.steps if s.agent == "notification")
        assert note.approval_required is True

    def test_fallback_plan_is_itself_valid(self):
        for text in ("帮我选 3 款女装", "出份报表发给 a@b.com", "帮我看看"):
            plan = default_plan_for(text)
            assert validate_plan(plan, user_request=text) == []


class TestBlockedSteps:
    """上游失败 → 传递下游必须被跳过。"""

    def test_direct_dependent_blocked(self):
        steps = [
            {"step_id": "a", "depends_on": []},
            {"step_id": "b", "depends_on": ["a"]},
        ]
        assert blocked_steps(steps, {"a"}) == {"b"}

    def test_transitive_dependent_blocked(self):
        """A 挂 → B 挂 → C 也必须挂。

        只看直接依赖的话 C 会被认为"可以跑"（它的依赖 B 既没成功也没失败），
        于是 C 带着空输入跑出一个看起来正常的结果 —— 比直接失败危险得多。
        """
        steps = [
            {"step_id": "a", "depends_on": []},
            {"step_id": "b", "depends_on": ["a"]},
            {"step_id": "c", "depends_on": ["b"]},
        ]
        assert blocked_steps(steps, {"a"}) == {"b", "c"}

    def test_independent_branch_not_blocked(self):
        steps = [
            {"step_id": "a", "depends_on": []},
            {"step_id": "b", "depends_on": ["a"]},
            {"step_id": "x", "depends_on": []},
        ]
        assert blocked_steps(steps, {"a"}) == {"b"}

    def test_failed_itself_not_in_blocked(self):
        assert "a" not in blocked_steps([{"step_id": "a", "depends_on": []}], {"a"})


# ===========================================================================
# 2. 环境与上下文
# ===========================================================================
@pytest.fixture
def seeded_env(tmp_path, monkeypatch):
    """临时库 + 一批能通过硬约束的商品，并把邮件策略放开。"""
    import fashionops.storage as storage_mod
    import fashionops.tools.mail as mail_tool
    from fashionops.config import Settings
    from fashionops.db import base as db_base

    settings = Settings(
        data_dir=tmp_path,
        db_host=None,
        cache_backend="memory",
        mail_enabled=True,
        mail_dry_run=True,
        mail_send_allowlist="@example.com",
    )
    reset_engine()
    monkeypatch.setattr(db_base, "get_settings", lambda: settings)
    monkeypatch.setattr(storage_mod, "get_settings", lambda: settings)
    storage_mod.reset_storage()
    # 邮件闸门默认连真 Redis，键是全局的 `mail:<地址>` —— 测试里必须换成
    # 进程内的，否则跨用例累计计数，第二个用例莫名被限流
    monkeypatch.setattr(mail_tool, "get_rate_limiter", lambda _b: MemoryRateLimiter())
    reset_checkpointer_cache()

    engine = build_engine(settings)
    Base.metadata.create_all(engine)

    now = utcnow()
    with Session(engine) as session:
        for spu, cost, price, stock, sold in [
            ("SPU0001", 60, 200, 400, 260),
            ("SPU0002", 70, 210, 320, 180),
            ("SPU0003", 55, 190, 260, 120),
        ]:
            product = Product(
                spu_code=spu,
                title=f"秋季法式连衣裙 {spu}",
                category="女装",
                sub_category="连衣裙",
                season="秋季",
                style_tags=["法式"],
                cost_price=Decimal(cost),
                list_price=Decimal(price),
                rating=Decimal("4.7"),
                status="on_sale",
            )
            session.add(product)
            session.flush()
            sku = SKU(
                product_id=product.id,
                sku_code=f"{spu}-M",
                color="米白",
                size="M",
                cost_price=Decimal(cost),
                sale_price=Decimal(price),
            )
            session.add(sku)
            session.flush()
            session.add(
                Inventory(
                    sku_id=sku.id,
                    change_type="in",
                    quantity=stock,
                    quantity_after=stock,
                    occurred_at=now - timedelta(days=1),
                )
            )
            session.add(
                Order(
                    order_no=f"SO-{spu}",
                    sku_id=sku.id,
                    channel="淘宝",
                    product_title=product.title,
                    quantity=sold,
                    unit_price=Decimal(price),
                    total_amount=Decimal(price) * sold,
                    actual_amount=Decimal(price) * sold,
                    status="completed",
                    paid_at=now - timedelta(days=3),
                )
            )
        session.add(
            Competitor(
                platform="mock",
                product_title="竞品连衣裙",
                category="女装",
                season="秋季",
                price=Decimal("260.00"),
            )
        )
        session.commit()

    yield settings
    reset_engine()
    reset_checkpointer_cache()
    storage_mod.reset_storage()


def _build_context(*, model=None):  # noqa: ANN001
    registry = ToolRegistry()
    register_all_tools(registry)
    gateway = ToolGateway(
        registry,
        rate_limiter=MemoryRateLimiter(),
        breaker=MemoryCircuitBreaker(),
        idempotency=IdempotencyStore(use_redis=False),
        rate_limit_per_minute=200,
    )
    return (
        RunContext(
            gateway=gateway,
            model=model or MockModelClient(),
            actor="orchestrator",
            scopes=AGENT_SCOPES["orchestrator"],
        ),
        registry,
    )


def _plan_reply(*steps: dict, approval_on: str | None = None) -> MockReply:
    """脚本化一个合法计划。

    mock 模型自己生成的计划**必然**语义非法（它不可能知道 depends_on 要指向
    别的 step_id），所以走通"非兜底"路径必须脚本化 —— 见 planner 模块 docstring。
    """
    payload = []
    for step in steps:
        item = dict(step)
        if approval_on and item["agent"] == approval_on:
            item["approval_required"] = True
        payload.append(item)
    plan = TaskPlan(reasoning="脚本化计划", steps=[PlannedStep(**s) for s in payload])
    return MockReply(content=plan.model_dump_json())


PLAN_2 = (
    {
        "step_id": "pick",
        "agent": "selection",
        "goal": "筛选候选款式",
    },
    {
        "step_id": "report",
        "agent": "analytics",
        "goal": "产出经营报表",
        "depends_on": ["pick"],
    },
)
PLAN_3 = (
    *PLAN_2,
    {
        "step_id": "send",
        "agent": "notification",
        "goal": "把结论发给运营",
        "depends_on": ["report"],
    },
)


# ===========================================================================
# 3. 图端到端
# ===========================================================================
class TestOrchestratorGraph:
    def test_runs_planned_steps_in_order(self, seeded_env):
        ctx, _ = _build_context(model=MockModelClient(script=[_plan_reply(*PLAN_2)]))
        graph = build_orchestrator(ctx, checkpointer=get_checkpointer(backend="memory"))
        out = graph.invoke(
            new_state(task_no="G1", user_request="帮我选 3 款秋季女装并出报表"), thread_config("G1", ctx)
        )

        assert out["status"] == "succeeded"
        assert out["completed_steps"] == ["pick", "report"]
        # 只在计划里的步骤跑 —— 没规划的 notification 不能被跑
        assert [s.node for s in out["steps"] if s.node in EXECUTABLE_AGENTS] == [
            "selection",
            "analytics",
        ]

    def test_step_id_differs_from_node_name(self, seeded_env):
        """计划里的 step_id 和节点名可以不同（``pick`` vs ``selection``）。

        这正是 ``completed_steps`` 不能复用 ``completed_nodes`` 的原因 ——
        拿节点名去算"这一步做完没"会错位，表现为步骤被重复执行。
        """
        ctx, _ = _build_context(model=MockModelClient(script=[_plan_reply(*PLAN_2)]))
        graph = build_orchestrator(ctx, checkpointer=get_checkpointer(backend="memory"))
        out = graph.invoke(new_state(task_no="G2", user_request="出报表"), thread_config("G2", ctx))
        assert out["completed_steps"] == ["pick", "report"]
        assert "selection" in out["completed_nodes"]
        assert "pick" not in out["completed_nodes"]

    def test_upstream_failure_blocks_downstream(self, seeded_env, monkeypatch):
        """选品失败 → 分析**根本不跑**，任务判失败。"""
        import fashionops.storage as storage_mod
        from fashionops.config import Settings

        # 换个空的临时库，selection 必然因"候选池为空"失败
        empty = Settings(data_dir=seeded_env.data_dir / "empty", db_host=None, cache_backend="memory")
        from fashionops.db import base as db_base

        monkeypatch.setattr(db_base, "get_settings", lambda: empty)
        monkeypatch.setattr(storage_mod, "get_settings", lambda: empty)
        reset_engine()
        from fashionops.db.base import build_engine as be

        Base.metadata.create_all(be(empty))

        ctx, _ = _build_context(model=MockModelClient(script=[_plan_reply(*PLAN_2)]))
        graph = build_orchestrator(ctx, checkpointer=get_checkpointer(backend="memory"))
        out = graph.invoke(new_state(task_no="G3", user_request="选品并出报表"), thread_config("G3", ctx))

        assert out["status"] == "failed"
        assert out["failed_steps"] == ["pick"]
        # 下游一步都没跑
        assert "analytics" not in [s.node for s in out["steps"]]
        assert (out["result"] or {})["skipped_steps"] == ["report"]

    def test_illegal_plan_falls_back_instead_of_failing(self, seeded_env):
        """模型给不出合法计划时用兜底链路，**任务照样跑完**。

        规划是优化项，不是关键路径 —— 做成关键路径等于每次任务都有概率
        因为一句没写好而全盘失败。
        """
        ctx, _ = _build_context(model=MockModelClient(script=[MockReply(content="{}")]))
        graph = build_orchestrator(ctx, checkpointer=get_checkpointer(backend="memory"))
        out = graph.invoke(
            new_state(task_no="G4", user_request="帮我选 3 款秋季女装并出报表"), thread_config("G4", ctx)
        )
        assert out["status"] == "succeeded"
        assert (out["findings"]["plan"] or {}).get("used_fallback") is True

    def test_graph_uses_plan_not_hardcoded_order(self, seeded_env):
        """只规划一步时，不该白跑另外两个 Agent。"""
        ctx, _ = _build_context(
            model=MockModelClient(
                script=[_plan_reply({"step_id": "report", "agent": "analytics", "goal": "出报表"})]
            )
        )
        graph = build_orchestrator(ctx, checkpointer=get_checkpointer(backend="memory"))
        out = graph.invoke(new_state(task_no="G5", user_request="出份报表"), thread_config("G5", ctx))

        ran = [s.node for s in out["steps"] if s.node in EXECUTABLE_AGENTS]
        assert ran == ["analytics"]


# ===========================================================================
# 4. HITL 与落库
# ===========================================================================
def _runner(seeded_env, *, plan_steps=PLAN_3, approval_on="notification"):  # noqa: ANN001
    """造一个 TaskRunner，并把 checkpointer 一并返回。

    必须返回它：测试里查状态要用**同一个** saver。
    直接用 ``get_checkpointer()`` 会读到真实应用的配置（可能是 Redis），
    拿到的是另一个实例，表现为"查不到状态"，看起来像图没跑。
    """
    from fashionops.agents.recorder import TaskRecorder

    ctx, _ = _build_context(
        model=MockModelClient(script=[_plan_reply(*plan_steps, approval_on=approval_on)])
    )
    saver = get_checkpointer(seeded_env, backend="memory")
    runner = TaskRunner(ctx, recorder=TaskRecorder(), checkpointer=saver)
    return runner, saver


class TestHitl:
    def test_interrupts_at_approval_step(self, seeded_env):
        runner, _saver = _runner(seeded_env)
        out = runner.submit(task_no="H1", user_request="选品出报表发给 ops@example.com")

        assert out.waiting_approval
        assert out.status == "waiting_approval"
        # 审批前不该跑 notification
        assert "notification" not in out.executed_nodes
        assert out.approval
        # 落库状态要能告诉前端"在等人确认"
        assert (task_snapshot("H1") or {})["status"] == "waiting_approval"
        assert (task_snapshot("H1") or {})["approval_payload"]

    def test_approve_resumes_and_finishes(self, seeded_env):
        runner, _saver = _runner(seeded_env)
        runner.submit(task_no="H2", user_request="选品出报表发给 ops@example.com")
        out = runner.resume(task_no="H2", decision={"approved": True, "by": "张三"})

        assert not out.interrupted
        assert out.status == "succeeded"
        assert "notification" in out.executed_nodes
        assert (task_snapshot("H2") or {})["status"] == "succeeded"

    def test_approval_records_the_real_step_id(self, seeded_env):
        """批准的必须是真实 step_id，不是路由哨兵。

        踩过的坑：审批闸口从 ``route`` 取 step_id，而 ``route`` 这时是
        ``__approval__`` 哨兵 —— 批准结果记到哨兵头上，hub 下次还判"没批"，
        表现为**无限弹同一个审批**。
        """
        runner, saver = _runner(seeded_env)
        runner.submit(task_no="H3", user_request="选品出报表发给 ops@example.com")
        out = runner.resume(task_no="H3", decision={"approved": True, "by": "李四"})

        assert out.status == "succeeded"
        approved = (latest_state(saver, "H3") or {}).get("approved_steps", [])
        assert "send" in approved
        assert "__approval__" not in approved

    def test_reject_cancels_without_running_step(self, seeded_env):
        runner, saver = _runner(seeded_env)
        runner.submit(task_no="H4", user_request="选品出报表发给 ops@example.com")
        out = runner.resume(
            task_no="H4", decision={"approved": False, "by": "王五", "reason": "名单没核对"}
        )

        assert out.status == "cancelled"
        assert "notification" not in out.executed_nodes
        state = latest_state(saver, "H4") or {}
        # 被驳回的步骤**不能**记进 completed_steps —— 它没跑过
        assert "send" not in state.get("completed_steps", [])
        assert any("驳回" in e for e in state.get("errors", []))

    def test_reject_clears_approval_payload(self, seeded_env):
        runner, _saver = _runner(seeded_env)
        runner.submit(task_no="H5", user_request="选品出报表发给 ops@example.com")
        runner.resume(task_no="H5", decision=False)
        snap = task_snapshot("H5") or {}
        assert snap["status"] == "cancelled"
        # 任务结束了，待审批内容要清掉，否则界面一直挂着"待确认"
        assert snap["approval_payload"] is None

    def test_cancel_has_its_own_event_type(self, seeded_env):
        """人工取消**不能**复用 ``task_failed``。

        事件流是给人排查用的：看到 ``task_failed`` 的人会去查错误栈，
        而取消根本没有错误 —— 是人做的决定。混在同一个名字里会误导排查方向。

        同一类问题还有：任务终态是 cancelled，但 error_message 有值
        （记的是"为什么被取消"），前端不能据此就扣"失败原因"的帽子。
        """
        runner, _saver = _runner(seeded_env)
        runner.submit(task_no="H7", user_request="选品出报表发给 ops@example.com")
        runner.resume(task_no="H7", decision={"approved": False, "by": "王五", "reason": "名单没核对"})

        kinds = [e["event_type"] for e in read_events("H7")]
        assert kinds[-1] == "task_cancelled"
        assert "task_failed" not in kinds
        # 取消要看得见，但不该像真失败那样报 error 级
        assert read_events("H7")[-1]["level"] == "warn"

    def test_failure_keeps_using_task_failed(self, seeded_env):
        """反过来：真失败仍然必须是 ``task_failed`` + error 级。

        不加这条的话，把两种终态一起改成 ``task_cancelled`` 也能让上一条用例通过 ——
        两条用例合起来才真正锁住"取消与失败是两种东西"。
        """
        runner, _saver = _runner(seeded_env)
        runner.submit(task_no="H8", user_request="选品出报表发给 ops@example.com")
        runner.recorder.finish(task_no="H8", status="failed", error_message="boom")

        events = read_events("H8")
        assert events[-1]["event_type"] == "task_failed"
        assert events[-1]["level"] == "error"

    @pytest.mark.parametrize("decision", [True, {"approved": True}, "yes", {"approve": "y"}])
    def test_decision_formats_tolerated(self, seeded_env, decision):
        """resume 的载荷格式容忍多种写法 —— 调用方可能是前端、脚本或人。"""
        runner, _saver = _runner(seeded_env)
        task_no = f"H6-{abs(hash(str(decision))) % 10000}"
        runner.submit(task_no=task_no, user_request="选品出报表发给 ops@example.com")
        out = runner.resume(task_no=task_no, decision=decision)
        assert out.status == "succeeded"


class TestRecorder:
    def test_task_and_events_persisted(self, seeded_env):
        runner, _saver = _runner(seeded_env)
        runner.submit(task_no="R1", user_request="选品出报表发给 ops@example.com")
        runner.resume(task_no="R1", decision=True)

        snap = task_snapshot("R1")
        assert snap and snap["status"] == "succeeded"
        assert snap["dag"]  # 计划写进了 tasks.dag

        events = read_events("R1")
        kinds = [e["event_type"] for e in events]
        assert "task_started" in kinds
        assert "task_planned" in kinds
        assert "approval_required" in kinds
        assert "task_resumed" in kinds
        assert "task_finished" in kinds

    def test_agent_runs_only_for_business_agents(self, seeded_env):
        """``agent_runs`` 是评测数据源，混进编排节点会把指标算歪。"""
        runner, _saver = _runner(seeded_env)
        runner.submit(task_no="R2", user_request="选品出报表发给 ops@example.com")
        runner.resume(task_no="R2", decision=True)

        with Session(build_engine(seeded_env)) as session:
            task = session.scalar(select(Task).where(Task.task_no == "R2"))
            rows = session.scalars(select(AgentRun).where(AgentRun.task_id == task.id)).all()
        names = [r.agent_name for r in rows]
        assert names == ["selection", "analytics", "notification"]
        assert all(r.status == "success" for r in rows)
        # 时间与 token 是评测要的四个数字的基础
        assert all(r.latency_ms is not None for r in rows)

    def test_seq_is_continuous_across_recorder_instances(self, seeded_env):
        """**回归真实 bug**：resume 时新建的 recorder 让 seq 从 1 重来。

        撞 ``uq_task_event_seq`` 唯一约束 → 插入失败被 ``_safe`` 吞掉 →
        表现是"批准之后的所有事件都不见了"，日志里只有一行 warning。

        这条用例锁住两件事：seq 连续，以及没有静默写入失败。
        """
        runner, _saver = _runner(seeded_env)
        runner.submit(task_no="R3", user_request="选品出报表发给 ops@example.com")
        before = len(read_events("R3"))

        # 换一个全新的 recorder 实例（模拟 resume 时的 build_runner）
        runner2, _saver2 = _runner(seeded_env)
        runner2.resume(task_no="R3", decision=True)

        events = read_events("R3")
        seqs = [e["seq"] for e in events]
        assert len(events) > before, "resume 之后的事件被丢了"
        assert seqs == list(range(1, len(seqs) + 1)), f"seq 不连续：{seqs}"
        assert runner2.recorder.write_failures == 0, "有记录被静默丢弃"

    def test_write_failures_are_counted(self, seeded_env, monkeypatch):
        """记录失败不能带崩任务，但**必须可被检测**。"""
        recorder = TaskRecorder()
        monkeypatch.setattr(
            "fashionops.agents.recorder.session_scope",
            lambda: (_ for _ in ()).throw(RuntimeError("db down")),
        )
        recorder.event("whatever", "x")  # 不该抛异常
        assert recorder.write_failures == 1

    def test_finish_clears_approval_payload(self, seeded_env):
        runner, _saver = _runner(seeded_env)
        runner.submit(task_no="R4", user_request="选品出报表发给 ops@example.com")
        assert (task_snapshot("R4") or {})["approval_payload"]
        runner.resume(task_no="R4", decision=True)
        assert (task_snapshot("R4") or {})["approval_payload"] is None

    def test_null_recorder_writes_nothing(self, seeded_env):
        ctx, _ = _build_context(model=MockModelClient(script=[_plan_reply(*PLAN_2)]))
        runner = TaskRunner(
            ctx, recorder=TaskRecorder(enabled=False), checkpointer=get_checkpointer(backend="memory")
        )
        out = runner.submit(task_no="R5", user_request="选品并出报表")
        assert out.status == "succeeded"
        with Session(build_engine(seeded_env)) as session:
            assert session.scalar(select(func.count()).select_from(Task).where(Task.task_no == "R5")) == 0


class TestRunnerOutcomes:
    def test_submit_returns_executed_nodes(self, seeded_env):
        runner, _saver = _runner(seeded_env)
        out = runner.submit(task_no="O1", user_request="选品出报表发给 ops@example.com")
        assert out.executed_nodes[0] == "plan"
        assert out.to_dict()["waiting_approval"] is True

    def test_current_reads_without_running(self, seeded_env):
        """前端刷新页面时用这个 —— 不该触发任何计算。"""
        runner, _saver = _runner(seeded_env)
        runner.submit(task_no="O2", user_request="选品出报表发给 ops@example.com")
        before = len(read_events("O2"))
        snap = runner.current("O2")
        assert snap["status"] == "waiting_approval"
        assert snap["plan"]["steps"]
        assert len(read_events("O2")) == before  # 读操作不产生事件


class TestBuildPlanRepairLoop:
    def test_invalid_then_valid_is_repaired(self, seeded_env):
        """第一次给非法计划、带着问题回喂后给出合法计划。"""
        ctx, _ = _build_context(
            model=MockModelClient(
                script=[
                    # 非法：编造 Agent
                    _plan_reply({"step_id": "a", "agent": "selection", "goal": "g"}),
                    _plan_reply(*PLAN_2),
                ]
            )
        )
        state = new_state(task_no="P1", user_request="选品并出报表")
        # 第一份其实合法（只有一个 selection），构造一个真非法的：依赖不存在
        bad = TaskPlan(
            reasoning="bad",
            steps=[
                PlannedStep(step_id="a", agent="selection", goal="g"),
                PlannedStep(step_id="b", agent="analytics", goal="g", depends_on=["nope"]),
            ],
        )
        ctx.model = MockModelClient(
            script=[
                MockReply(content=bad.model_dump_json()),
                MockReply(content=TaskPlan(
                    reasoning="fixed",
                    steps=[PlannedStep(**s) for s in PLAN_2],
                ).model_dump_json()),
            ]
        )
        outcome = build_plan(ctx, state, max_repair=2)
        assert outcome.used_fallback is False
        assert outcome.attempts == 2
        assert [s.agent for s in outcome.plan.steps] == ["selection", "analytics"]

    def test_all_attempts_fail_uses_fallback(self, seeded_env):
        ctx, _ = _build_context(model=MockModelClient(script=[MockReply(content="{")] * 4))
        state = new_state(task_no="P2", user_request="帮我选 3 款女装")
        outcome = build_plan(ctx, state, max_repair=2)
        assert outcome.used_fallback is True
        assert outcome.issues
        assert [s.agent for s in outcome.plan.steps] == ["selection"]
