"""端到端演示：选品 → 数据分析 → 邮件通知，三个 Agent 串起来跑。

这条链路是阶段 1 的核心验收标准。总控编排 Agent（W4）还没写，
所以这里用**顺序调用 + 手写状态合并**先把链路验证通 ——
等编排层上了，把这里的 `_merge` 换成 LangGraph 的 reducer 即可。

## 为什么状态合并要自己写

LangGraph 里不同字段的合并语义是**不一样**的，很容易搞错：

* ``steps`` / ``artifacts`` / ``errors`` 声明了 reducer → **追加**本次新增的
* ``total_tokens`` / ``total_cost`` / ``llm_calls`` 没有 reducer → 节点回的是**累加后的绝对值**，直接覆盖
* ``findings`` / ``candidates`` 也没有 reducer → 覆盖

照着这个规则写 `_merge`，就等于把 LangGraph 的行为显式表达出来了。
真接上编排层之后这段代码就删掉。

运行::

    uv run python scripts/demo_pipeline.py
    uv run python scripts/demo_pipeline.py --mock
    uv run python scripts/demo_pipeline.py "帮我选 5 款秋季女装，出周报发给 ops@example.com"
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import func, select  # noqa: E402

from fashionops.agents.analytics import make_analytics_node  # noqa: E402
from fashionops.agents.notification import make_notification_node  # noqa: E402
from fashionops.agents.selection import make_selection_node  # noqa: E402
from fashionops.agents.state import RunContext, budget_status, new_state  # noqa: E402
from fashionops.config import get_settings  # noqa: E402
from fashionops.db import session_scope  # noqa: E402
from fashionops.db.models import EmailMessage, Order, Product  # noqa: E402
from fashionops.gateway import (  # noqa: E402
    MemoryAuditSink,
    ToolGateway,
    get_circuit_breaker,
    get_rate_limiter,
    registry,
)
from fashionops.llm import MockModelClient, get_model_client  # noqa: E402
from fashionops.mail import MailGate  # noqa: E402
from fashionops.tools import AGENT_SCOPES, register_all_tools  # noqa: E402

DEFAULT_REQUEST = "帮我选 3 款秋季女装，再出一份近 30 天的经营分析报表发给 ops@example.com"

#: 有 reducer 的字段：更新里给的是"本次新增"，要追加
_APPEND_FIELDS = ("steps", "completed_nodes", "artifacts", "errors")


def main() -> int:
    argv = list(sys.argv[1:])
    force_mock = "--mock" in argv
    argv = [a for a in argv if a != "--mock"]
    request = " ".join(argv) or DEFAULT_REQUEST

    settings = get_settings()
    model = MockModelClient() if force_mock else get_model_client(settings)

    _header("环境")
    print(f"  数据源    : {settings.dependency_summary()['database']}")
    print(f"  模型      : {settings.model_provider} / {settings.model_name} -> {type(model).__name__}")
    print(f"  邮件开关  : enabled={settings.mail_enabled}  dry_run={settings.mail_dry_run}")
    print(f"  发信白名单: {list(settings.mail_allowlist) or '（空 — 按 fail-closed 一律禁发）'}")

    with session_scope() as session:
        products = session.scalar(select(func.count()).select_from(Product)) or 0
        orders = session.scalar(select(func.count()).select_from(Order)) or 0
    print(f"  数据量    : 商品 {products} / 订单 {orders}")
    if not products or not orders:
        print("\n  数据为空。请先执行： uv run fashionops-db init && uv run fashionops-seed\n")
        return 1

    register_all_tools(registry)
    audit = MemoryAuditSink()
    gateway = ToolGateway(
        registry,
        rate_limiter=get_rate_limiter(settings.cache_backend),
        breaker=get_circuit_breaker(settings.cache_backend),
        audit_sink=audit,
    )

    _header("用户需求")
    print(f"  {request}")

    # 每个 Agent 用自己的权限集 —— 最小权限原则，不是给一个 superuser
    def ctx_for(agent: str) -> RunContext:
        return RunContext(
            gateway=gateway, model=model, actor=f"{agent}_agent", scopes=AGENT_SCOPES[agent]
        )

    state = new_state(task_no="DEMO-PIPELINE-001", user_request=request)
    overall_start = _now()

    # ---------------- 节点 1：选品 ----------------
    _run(state, make_selection_node(ctx_for("selection")), "选品 Agent")

    # ---------------- 节点 2：数据分析 ----------------
    _run(state, make_analytics_node(ctx_for("analytics"), days=30), "数据分析 Agent")

    # ---------------- 节点 3：邮件通知 ----------------
    _run(state, make_notification_node(ctx_for("notification"), purpose="经营日报"), "邮件通知 Agent")

    # ---------------- 汇总 ----------------
    total_ms = int((_now() - overall_start) * 1000)

    _header("整条链路")
    print(f"  {'节点':<12}{'状态':<12}{'耗时':>9}{'工具数':>8}{'tokens':>9}")
    for step in state["steps"]:
        print(
            f"  {step.node:<12}{step.status:<12}{step.duration_ms:>7} ms"
            f"{len(step.tool_calls):>8}{step.tokens:>9}"
        )
    print(f"\n  总耗时    : {total_ms} ms")
    print(f"  模型调用  : {state['llm_calls']} 次 / {state['total_tokens']} tokens")
    print(f"  总成本    : ¥{Decimal(str(state['total_cost'])):.6f}")

    _header("Agent 之间传的是什么")
    for key in ("selection", "analytics", "notification"):
        data = state["findings"].get(key) or {}
        print(f"\n  findings['{key}'] 的字段：")
        print(f"    {', '.join(sorted(data)) or '（空）'}")
    print(
        "\n  注意：findings 里存的是**精炼结论**，不是原始数据。\n"
        "  选品的几十条候选明细不会进 analytics 的上下文 —— 这就是长链路不爆 token 的关键。"
    )

    _header("产物")
    for artifact in state.get("artifacts", []):
        print(f"  {artifact.kind:<10} {artifact.ref:<16} {artifact.name}  (by {artifact.produced_by})")

    _header("邮件结果")
    note = state["findings"].get("notification") or {}
    if note:
        draft = note.get("draft", {})
        print(f"  编号      : {note.get('message_no')}")
        print(f"  决策      : {note.get('decision')}   status={note.get('status')}")
        print(f"  投递成功  : {note.get('sent')}   backend={note.get('backend')}")
        print(f"  收件人    : {draft.get('recipients')}")
        print(f"  主题      : {draft.get('subject')}")
        print(f"  正文长度  : {draft.get('body_chars')} 字")
        print(f"  附件素材  : {note.get('attachment_asset_nos') or '（无）'}")
        if note.get("reason"):
            print(f"  闸门理由  : {note['reason']}")
        if note.get("hint"):
            print(f"  怎么办    : {note['hint']}")

    if state.get("approval"):
        _header("需要人工接手")
        ap = state["approval"]
        print(f"  动作      : {ap.action}")
        print(f"  风险      : {ap.risk}")
        print(f"  原因      : {ap.reason}")
        print("\n  被策略拦下的邮件**不是失败** —— 重试一万次也不会让白名单多一条配置，")
        print("  所以这里产出的是审批请求，而不是让上游重试。")

    # 库里能查到"被拦下来的邮件"吗？这才是闸门设计的价值所在
    with session_scope() as session:
        rows = session.execute(
            select(EmailMessage.gate_decision, func.count())
            .group_by(EmailMessage.gate_decision)
            .order_by(func.count().desc())
        ).all()
    _header("数据库里的邮件记录（放行与被拦都记了）")
    for decision, count in rows:
        print(f"  {str(decision):<24}{count:>5} 封")
    print("\n  只记'发成功的'会让被拦的完全不可见 —— 运维看到'这周只发了 N 封'，")
    print("  却不知道还有几封被策略挡住、而业务方以为已经通知了。")

    _header("怎么让它真发出去")
    policy = MailGate(settings).policy
    print(f"  当前：enabled={policy['enabled']}  dry_run={policy['dry_run']}  "
          f"smtp_configured={policy['smtp_configured']}")
    print("  在 platform\\.env 里依次改这三项（**建议分三步，不要一次全开**）：")
    print("    1) FASHIONOPS_MAIL_ENABLED=true")
    print("    2) FASHIONOPS_MAIL_SEND_ALLOWLIST=@yourcompany.com   （先验策略，仍 dry_run）")
    print("    3) 配好 FASHIONOPS_SMTP_HOST/USER/PASSWORD 后，FASHIONOPS_MAIL_DRY_RUN=false")
    print("\n  SMTP 密码填邮箱服务商生成的**授权码**，不是邮箱登录密码。")

    _header("预算")
    for key, value in budget_status(state).items():
        print(f"  {key:<16}: {value}")

    _header("审计（工具网关自动记录）")
    for entry in audit.entries:
        print(f"  {entry.tool_name:<28}[{entry.status}]  {entry.duration_ms} ms")

    _header("网关统计")
    print(json.dumps(gateway.stats_snapshot()["tools"], ensure_ascii=False, indent=2))
    print()
    return 0


# ---------------------------------------------------------------------------
def _run(state: dict, node: Any, label: str) -> None:
    """跑一个节点，并把状态更新合并回 state（模拟 LangGraph 的 reducer）。"""
    update = node(state)
    _merge(state, update)
    step = update["steps"][0]
    flag = "OK " if step.status == "succeeded" else "!! "
    print(f"\n  {flag}{label:<14} {step.duration_ms:>6} ms   {step.summary[:70]}")


def _merge(state: dict, update: dict) -> None:
    """按 LangGraph 的 reducer 语义合并。

    有 reducer 的字段追加，没有的直接覆盖 —— 弄错这个会导致
    ``steps`` 重复两遍，或者 ``total_tokens`` 丢掉前几轮的消耗（预算控制静默失效）。
    """
    for key, value in update.items():
        if key in _APPEND_FIELDS:
            state.setdefault(key, [])
            state[key] = [*state[key], *(value if isinstance(value, list) else [value])]
        elif key == "findings":
            state["findings"] = {**state.get("findings", {}), **value}
        else:
            state[key] = value


def _now() -> float:
    import time

    return time.perf_counter()


def _header(text: str) -> None:
    print()
    print("=" * 74)
    print(f"  {text}")
    print("=" * 74)


if __name__ == "__main__":
    raise SystemExit(main())
