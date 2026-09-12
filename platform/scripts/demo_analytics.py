"""演示：跑一遍数据分析 Agent，产出一份真实的 Excel 报表。

走的是和真实模型完全一样的代码路径（真实 SQL + 真实网关 + 真实存储 + 真实 openpyxl），
只把模型换成 mock 或读 ``.env`` 里配的智谱模型。

运行::

    uv run python scripts/demo_analytics.py
    uv run python scripts/demo_analytics.py --mock
    uv run python scripts/demo_analytics.py --days 60
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import func, select  # noqa: E402

from fashionops.agents.analytics import make_analytics_node  # noqa: E402
from fashionops.agents.state import RunContext, budget_status, new_state  # noqa: E402
from fashionops.config import get_settings  # noqa: E402
from fashionops.db import session_scope  # noqa: E402
from fashionops.db.models import Order  # noqa: E402
from fashionops.gateway import (  # noqa: E402
    MemoryAuditSink,
    ToolGateway,
    get_circuit_breaker,
    get_rate_limiter,
    registry,
)
from fashionops.llm import MockModelClient, get_model_client  # noqa: E402
from fashionops.storage import get_storage  # noqa: E402
from fashionops.tools import AGENT_SCOPES, register_all_tools  # noqa: E402

DEFAULT_REQUEST = "出一份近 30 天的经营分析报表，重点看退货率和渠道效率"


def main() -> int:
    argv = list(sys.argv[1:])
    force_mock = "--mock" in argv
    argv = [a for a in argv if a != "--mock"]
    days = 30
    if "--days" in argv:
        idx = argv.index("--days")
        days = int(argv[idx + 1])
        del argv[idx : idx + 2]
    request = " ".join(argv) or DEFAULT_REQUEST

    settings = get_settings()
    model = MockModelClient() if force_mock else get_model_client(settings)

    _header("环境")
    print(f"  数据源    : {settings.dependency_summary()['database']}")
    print(f"  模型      : {settings.model_provider} / {settings.model_name} -> {type(model).__name__}")
    print(f"  存储根    : {settings.local_storage_dir}")
    print(f"  统计窗口  : 近 {days} 天")

    with session_scope() as session:
        order_count = session.scalar(select(func.count()).select_from(Order))
    if not order_count:
        print("\n  开发库还没有订单数据。请先执行： uv run fashionops-seed\n")
        return 1
    print(f"  订单总数  : {order_count}")

    # ---- 真实网关 ----
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
        actor="analytics_agent",
        scopes=AGENT_SCOPES["analytics"],
    )

    # ---- 跑节点 ----
    _header("用户需求")
    print(f"  {request}")

    state = new_state(task_no="DEMO-ANALYTICS-001", user_request=request)
    update = make_analytics_node(ctx, days=days)(state)

    step = update["steps"][0]
    _header("执行结果")
    print(f"  状态      : {step.status}")
    print(f"  耗时      : {step.duration_ms} ms")
    print(f"  工具调用  : {', '.join(step.tool_calls) or '（无）'}")
    print(f"  模型调用  : {update['llm_calls']} 次 / {update['total_tokens']} tokens")
    print(f"  本轮成本  : ¥{Decimal(str(update['total_cost'])):.6f}")
    if step.error:
        print(f"  错误      : {step.error}")

    findings = update.get("findings", {}).get("analytics")
    if not findings:
        print("\n  没有产出分析结论。\n")
        return 1

    _header("整体指标（代码算的，不是模型算的）")
    totals = findings["totals"]
    for key, label in (
        ("gmv", "总 GMV"),
        ("order_count", "订单数"),
        ("avg_order_value", "客单价"),
        ("refund_amount", "退款金额"),
    ):
        print(f"  {label:<10}: {totals.get(key)}")

    _header("类目明细")
    print(f"  {'类目':<12}{'GMV':>12}{'订单':>8}{'客单价':>11}{'退货率':>10}")
    for row in findings["by_category"]:
        print(
            f"  {row['group']:<12}{row['gmv']:>12,.2f}{row['order_count']:>8}"
            f"{row['avg_order_value']:>11,.2f}{row['return_rate']:>9.1%}"
        )

    _header("模型总结")
    print(f"  {findings['summary']}")

    _header(f"代码告警（阀值判定，共 {len(findings['alerts'])} 条）")
    icon = {"critical": "!!", "warning": " *", "info": "  "}
    if not findings["alerts"]:
        print("  没有指标越线。")
    for item in findings["alerts"]:
        print(f"\n  {icon.get(item['level'], '  ')} [{item['level']}] {item['dimension']} / {item['metric']}")
        print(f"      {item['message']}")
        if item["threshold"] is not None:
            print(f"      实测 {item['value']} vs 阈值 {item['threshold']}")

    _header(f"模型解读（{len(findings['insights'])} 条建议，严重程度以告警为准）")
    for item in findings["insights"]:
        print(f"\n  · {item['dimension']}")
        print(f"      观察: {item['observation']}")
        print(f"      建议: {item['action'] or '（无）'}")

    if findings.get("data_quality_notes"):
        _header("数据质量提示")
        for note in findings["data_quality_notes"]:
            print(f"  · {note}")

    # ---- 产物 ----
    _header("产物")
    report = findings.get("report")
    if report:
        print(f"  素材编号  : {report['asset_no']}")
        print(f"  文件名    : {report['filename']}")
        print(f"  存储 key  : {report['storage_key']}")
        print(f"  体积      : {report['size_bytes']:,} 字节")
        print(f"  sha256    : {report['sha256'][:16]}…")
        print(f"  工作表    : {', '.join(report['sheet_names'])}")
        print(f"  访问地址  : {report['url']}")
        if report["truncated"]:
            print(f"  被截断    : {report['truncated']}")

        local = Path(get_storage().absolute_path(report["storage_key"]))  # type: ignore[attr-defined]
        print(f"  本地路径  : {local}")
        print(f"  文件存在  : {local.is_file()}  ({local.stat().st_size:,} 字节)")
        print("\n  用 Excel 直接打开上面这个路径就能看到报表。")
    else:
        print(f"  报表导出失败：{findings.get('report_error')}")
        print("  但分析结论仍然产出 —— 结论本身有价值，不该因为附件生成失败而丢掉。")

    for artifact in update.get("artifacts", []):
        print(f"\n  Artifact  : kind={artifact.kind} ref={artifact.ref} by={artifact.produced_by}")

    _header("预算")
    for key, value in budget_status({**state, **update}).items():
        print(f"  {key:<16}: {value}")

    _header("审计（工具网关自动记录，已脱敏）")
    for entry in audit.entries:
        print(f"  {entry.actor} -> {entry.tool_name}  [{entry.status}]  {entry.duration_ms} ms")
        print(f"    结果摘要: {entry.result_summary}")

    _header("统计")
    print(json.dumps(gateway.stats_snapshot()["tools"], ensure_ascii=False, indent=2))
    print()
    return 0


def _header(text: str) -> None:
    print()
    print("=" * 72)
    print(f"  {text}")
    print("=" * 72)


if __name__ == "__main__":
    raise SystemExit(main())
