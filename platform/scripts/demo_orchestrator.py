"""端到端演示：总控编排 Agent（LangGraph 图 + HITL 闸口 + 任务落库）。

和 ``demo_pipeline.py`` 的区别：

| | demo_pipeline | demo_orchestrator |
| --- | --- | --- |
| 怎么跑 | 脚本顺序调用 + 手写状态合并 | **LangGraph StateGraph** |
| 执行顺序 | 写死 | **完全由模型拆出的计划决定** |
| 人工确认 | 无 | **真 interrupt + resume** |
| 落库 | 不落 | **写 tasks / task_events / agent_runs** |
| 上游失败 | 继续跑 | **阻断下游，任务判失败** |

默认写**真实数据库**（`.env` 配的那个），这样前端任务列表能看到真任务。
想不污染开发库就加 ``--tmp``。

运行::

    uv run python scripts/demo_orchestrator.py
    uv run python scripts/demo_orchestrator.py --mock --auto-approve
    uv run python scripts/demo_orchestrator.py --tmp "帮我选 3 款秋季女装，出报表发给 ops@example.com"
"""

from __future__ import annotations

import sys
import tempfile
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_REQUEST = "帮我选 3 款秋季女装，再出一份近 30 天的经营分析报表发给 ops@example.com"


def main() -> int:
    argv = list(sys.argv[1:])
    force_mock = "--mock" in argv
    use_tmp = "--tmp" in argv
    auto_approve = "--auto-approve" in argv
    reject = "--reject" in argv
    rest = [a for a in argv if not a.startswith("--")]
    request = " ".join(rest) or DEFAULT_REQUEST

    if use_tmp:
        _use_temp_env()
    else:
        _prefer_sqlite_if_configured()

    from fashionops.agents.runner import TaskRunner
    from fashionops.agents.state import RunContext
    from fashionops.config import get_settings
    from fashionops.gateway import (
        MemoryAuditSink,
        ToolGateway,
        get_circuit_breaker,
        get_rate_limiter,
        registry,
    )
    from fashionops.llm import MockModelClient, get_model_client
    from fashionops.tools import AGENT_SCOPES, register_all_tools

    settings = get_settings()
    model = MockModelClient() if force_mock else get_model_client(settings)

    _header("环境")
    print(f"  数据源    : {settings.dependency_summary()['database']}")
    print(f"  模型      : {settings.model_provider} / {settings.model_name} -> {type(model).__name__}")
    print(f"  人工确认  : {'自动批准（--auto-approve）' if auto_approve else '真停下来等你确认'}")

    register_all_tools(registry)
    audit = MemoryAuditSink()
    gateway = ToolGateway(
        registry,
        rate_limiter=get_rate_limiter(settings.cache_backend),
        breaker=get_circuit_breaker(settings.cache_backend),
        audit_sink=audit,
    )
    ctx = RunContext(
        gateway=gateway,
        model=model,
        actor="orchestrator",
        scopes=AGENT_SCOPES["orchestrator"],
    )
    runner = TaskRunner(ctx)
    task_no = f"ORCH-DEMO-{_stamp()}"
    print(f"  任务编号  : {task_no}")

    _header("用户需求")
    print(f"  {request}")

    # ---------------- 第一次执行 ----------------
    outcome = runner.submit(task_no=task_no, user_request=request, task_type="daily_report")

    _section_plan(task_no)
    _section_steps(task_no)

    if outcome.waiting_approval:
        _header("⏸  停在人工确认闸口")
        ap = outcome.approval or {}
        print(f"  待确认动作: {ap.get('action') or ap.get('question')}")
        print(f"  理由      : {ap.get('reason')}")
        print(f"  风险      : {(ap.get('payload') or {}).get('risk', 'medium')}")
        print(f"  挂起节点  : {outcome.pending}")
        print()
        print("  这时任务状态是 waiting_approval，状态已经 checkpoint 落盘。")
        print("  人工确认后**从断点继续**，前面跑过的节点不会重跑。")

        decision: Any
        if reject:
            decision = {"approved": False, "by": "李四", "reason": "收件人名单还没核对"}
            print("\n  >>> 模拟驳回：", decision)
        elif auto_approve:
            decision = {"approved": True, "by": "张三（自动）"}
            print("\n  >>> 自动批准：", decision)
        else:
            decision = _prompt_decision()
            if decision is None:
                print("\n  已跳过确认。任务保持 waiting_approval，可从界面继续。")
                _section_db(task_no)
                return 0

        outcome = runner.resume(task_no=task_no, decision=decision)

    _header("执行结果")
    print(f"  状态        : {outcome.status}")
    print(f"  中断        : {outcome.interrupted}")
    print(f"  本次跑过节点: {' → '.join(outcome.executed_nodes)}")
    if outcome.error:
        print(f"  错误        : {outcome.error}")

    result = outcome.result or {}
    _header("产出摘要")
    print(f"  计划        : {result.get('plan')}")
    print(f"  完成步骤    : {result.get('completed_steps')}")
    if result.get("failed_steps"):
        print(f"  失败步骤    : {result['failed_steps']}")
    if result.get("skipped_steps"):
        print(f"  被跳过的    : {result['skipped_steps']}")
    print(f"  产出文件    : {result.get('artifact_refs') or '（无）'}")
    print(f"  模型调用    : {result.get('llm_calls')} 次 / {result.get('total_tokens')} tokens")
    print(f"  总成本      : ¥{result.get('total_cost', 0):.6f}")
    print(f"  总结        : {result.get('summary')}")

    _section_steps(task_no)
    _section_db(task_no)

    _header("审计（工具网关）")
    for entry in audit.entries:
        print(f"  {entry.tool_name:<28}[{entry.status}]  {entry.duration_ms} ms")

    print()
    return 0


# ---------------------------------------------------------------------------
def _section_plan(task_no: str) -> None:
    from fashionops.agents.recorder import task_snapshot

    snap = task_snapshot(task_no) or {}
    dag = snap.get("dag") or {}
    steps = dag.get("steps") or []
    if not steps:
        return
    _header("模型拆出的 DAG（写进了 tasks.dag）")
    print(f"  reasoning: {dag.get('reasoning', '')[:100]}")
    print()
    print(f"  {'step_id':<14}{'agent':<14}{'依赖':<16}{'需确认'}")
    for s in steps:
        deps = ",".join(s.get("depends_on") or []) or "—"
        mark = "是" if s.get("approval_required") else ""
        print(f"  {s.get('step_id', ''):<14}{s.get('agent', ''):<14}{deps:<16}{mark}")
        if s.get("goal"):
            print(f"      └ {s['goal']}")


def _section_steps(task_no: str) -> None:
    from fashionops.agents.checkpoint import get_checkpointer, latest_state

    state = latest_state(get_checkpointer(), task_no) or {}
    records = state.get("steps") or []
    if not records:
        return
    _header("节点执行轨迹（存在 LangGraph checkpoint 里）")
    print(f"  {'节点':<14}{'状态':<12}{'耗时':>9}{'tokens':>9}")
    for r in records:
        print(f"  {r.node:<14}{r.status:<12}{r.duration_ms:>7} ms{r.tokens:>9}")
        if r.summary:
            print(f"      {r.summary[:88]}")


def _section_db(task_no: str) -> None:
    """展示落库结果 —— 这是本次改动的核心价值。"""
    from sqlalchemy import func, select

    from fashionops.agents.recorder import read_events, task_snapshot
    from fashionops.db import session_scope
    from fashionops.db.models import AgentRun, Task

    _header("数据库里的任务记录（前端读的就是这些）")
    snap = task_snapshot(task_no) or {}
    print(f"  tasks.status      : {snap.get('status')}")
    print(f"  tasks.progress    : {snap.get('progress')}")
    print(f"  tasks.current_node: {snap.get('current_node')}")
    print(f"  tasks.dag         : {'有' if snap.get('dag') else '无'}")
    print(f"  tasks.approval    : {'有' if snap.get('approval_payload') else '无'}")

    events = read_events(task_no)
    print(f"\n  task_events（{len(events)} 条，SSE 的数据源）:")
    for e in events[:14]:
        node = e.get("node") or "-"
        msg = (e.get("message") or "")[:56]
        print(f"    #{e['seq']:<3} {e['event_type']:<16}{node:<14}{msg}")

    with session_scope() as session:
        task = session.scalar(select(Task).where(Task.task_no == task_no))
        rows = []
        if task:
            rows = session.scalars(
                select(AgentRun).where(AgentRun.task_id == task.id).order_by(AgentRun.id)
            ).all()
        print(f"\n  agent_runs（{len(rows)} 条，评测与成本归因的数据源）:")
        for r in rows:
            print(
                f"    {r.agent_name:<14}{r.status:<10}"
                f"{r.latency_ms or 0:>7} ms{r.total_tokens:>7} tokens"
                f"  工具 {r.tool_call_count} 个"
            )
        total = session.scalar(select(func.count()).select_from(AgentRun)) or 0
        print(f"\n  （全库 agent_runs 共 {total} 条）")


def _prompt_decision() -> dict[str, Any] | None:
    print()
    print("  [y] 批准   [n] 驳回   [其他] 先不管，退出（任务会停在 waiting_approval）")
    try:
        answer = input("  请选择: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    if answer in {"y", "yes"}:
        return {"approved": True, "by": "命令行用户"}
    if answer in {"n", "no"}:
        return {"approved": False, "by": "命令行用户", "reason": "命令行驳回"}
    return None


def _use_temp_env() -> None:
    """切到临时 SQLite + 临时存储，不碰开发库。

    用**改环境变量 + 清缓存**而不是替换 ``get_settings`` 引用 —— 后者只能
    改到少数几个模块的引用，而 ``config.get_settings`` 是 ``lru_cache`` 单例，
    别处拿到的还是旧对象。表现是"打印说连 MySQL、实际写 SQLite"，
    这种不一致比直接报错难查得多。
    """
    import os

    import fashionops.storage as storage_mod
    from fashionops.config import get_settings
    from fashionops.db import base as db_base

    tmp = tempfile.mkdtemp()
    os.environ["FASHIONOPS_DATA_DIR"] = tmp
    os.environ["FASHIONOPS_DB_HOST"] = ""
    os.environ["FASHIONOPS_CACHE_BACKEND"] = "memory"
    get_settings.cache_clear()

    db_base.reset_engine()
    storage_mod.reset_storage()
    from fashionops.db.base import Base, build_engine

    Base.metadata.create_all(build_engine(get_settings()))


def _prefer_sqlite_if_configured() -> None:
    """开发库是 MySQL 时直接用；但如果连不上就退到 SQLite，别让演示起不来。"""
    from sqlalchemy import text

    from fashionops.config import get_settings

    settings = get_settings()
    if settings.is_sqlite:
        return
    try:
        from fashionops.db.base import build_engine

        with build_engine(settings).connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        print(f"  [警告] MySQL 连不上（{type(exc).__name__}），本次演示改用临时 SQLite")
        _use_temp_env()


def _stamp() -> str:
    from fashionops.db.base import utcnow

    return utcnow().strftime("%m%d%H%M%S")


def _header(text: str) -> None:
    print()
    print("=" * 74)
    print(f"  {text}")
    print("=" * 74)


if __name__ == "__main__":
    raise SystemExit(main())
