"""演示：跑一遍选品 Agent。

用**开发库里的真实假数据** + 真实的工具网关（含限流/熔断/审计）+ mock 模型。
不用填任何 API Key，但走的是和真实模型完全一样的代码路径。

运行::

    uv run python scripts/demo_selection.py
    uv run python scripts/demo_selection.py "帮我选 5 款秋季女装，成本别超过 200"
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import func, select  # noqa: E402

from fashionops.agents.selection import make_selection_node  # noqa: E402
from fashionops.agents.state import RunContext, budget_status, new_state  # noqa: E402
from fashionops.config import get_settings  # noqa: E402
from fashionops.db import session_scope  # noqa: E402
from fashionops.db.models import Product  # noqa: E402
from fashionops.gateway import (  # noqa: E402
    MemoryAuditSink,
    ToolGateway,
    get_circuit_breaker,
    get_rate_limiter,
    registry,
)
from fashionops.llm import MockModelClient, get_model_client  # noqa: E402
from fashionops.tools import AGENT_SCOPES, register_all_tools  # noqa: E402

DEFAULT_REQUEST = "帮我选 3 款秋季女装，库存要够，别亏本卖"


def main() -> int:
    argv = list(sys.argv[1:])
    # --mock 强制用假模型，方便和真实模型做对照
    force_mock = "--mock" in argv
    argv = [a for a in argv if a != "--mock"]
    request = " ".join(argv) or DEFAULT_REQUEST
    settings = get_settings()

    # 默认用 .env 里配的模型；provider=mock 或没填 key 时会自动降级为假模型
    model = MockModelClient() if force_mock else get_model_client(settings)

    _header("环境")
    print(f"  数据源    : {settings.dependency_summary()['database']}")
    print(f"  模型      : {settings.model_provider} / {settings.model_name} -> {type(model).__name__}")
    print(f"  限流后端  : {settings.cache_backend}")

    with session_scope() as session:
        product_count = session.scalar(select(func.count()).select_from(Product))
    if not product_count:
        print("\n  开发库还没有数据。请先执行： uv run fashionops-seed\n")
        return 1
    print(f"  商品总数  : {product_count}")

    # ---- 组装真实的网关 ----
    register_all_tools(registry)
    audit = MemoryAuditSink()
    gateway = ToolGateway(
        registry,
        rate_limiter=get_rate_limiter(settings.cache_backend),
        breaker=get_circuit_breaker(settings.cache_backend),
        audit_sink=audit,
    )
    print(f"  已注册工具: {len(registry)} 个 -> {', '.join(registry.names())}")

    ctx = RunContext(
        gateway=gateway,
        model=model,
        actor="selection_agent",
        scopes=AGENT_SCOPES["selection"],
    )

    # ---- 跑节点 ----
    _header("用户需求")
    print(f"  {request}")

    state = new_state(task_no="DEMO-SELECTION-001", user_request=request)
    node = make_selection_node(ctx)
    update = node(state)

    step = update["steps"][0]
    _header("执行结果")
    print(f"  状态      : {step.status}")
    print(f"  耗时      : {step.duration_ms} ms")
    print(f"  工具调用  : {', '.join(step.tool_calls) or '（无）'}")
    print(f"  模型调用  : {update['llm_calls']} 次 / {update['total_tokens']} tokens")
    print(f"  本轮成本  : ¥{Decimal(str(update['total_cost'])):.6f}")
    if step.error:
        print(f"  错误      : {step.error}")

    findings = update.get("findings", {}).get("selection")
    if findings:
        _header("需求解析")
        criteria = findings.get("criteria", {})
        for key in ("category", "season", "count", "min_stock", "min_margin_rate"):
            print(f"  {key:<16}: {criteria.get(key)}")

        _header("硬约束过滤（代码判定，无商量余地）")
        for rule in findings.get("constraints", []):
            print(f"  · {rule}")
        print()
        print(f"  候选总数  : {findings['total_input']}")
        print(f"  通过      : {findings['passed']}")
        print(f"  被挡      : {findings['filtered_out']}")

        samples = findings.get("rejected_samples") or []
        if samples:
            print("\n  被挡掉的（前几个）：")
            for item in samples[:5]:
                print(f"    ✗ {item['spu_code']}  {item['title'][:22]:<24} {item['detail']}")

    selected = update.get("selected") or []
    if selected:
        _header(f"最终选出的 {len(selected)} 款（按综合得分降序）")
        for item in selected:
            print(f"\n  #{item['rank']}  {item['spu_code']}  {item['title']}")
            print(
                f"      综合得分 {item['total_score']:.2f}"
                f"  |  吊牌价 ¥{item['list_price']}"
                f"  |  毛利率 {float(item['margin_rate']):.1%}"
                f"  |  库存 {item['available_stock']} 件"
                f"  |  近30天 {item['sold_30d']} 件"
            )
            dims = item["dimension_scores"]
            print(
                "      分项: "
                + "  ".join(f"{k}={v:.2f}" for k, v in dims.items())
            )
            print(f"      理由: {item['reason']}")

    _header("预算")
    for key, value in budget_status({**state, **update}).items():
        print(f"  {key:<16}: {value}")

    _header("审计（工具网关自动记录，已脱敏）")
    for entry in audit.entries:
        print(
            f"  {entry.actor} -> {entry.tool_name}  [{entry.status}]  {entry.duration_ms} ms"
        )
        print(f"    入参快照: {json.dumps(entry.args_digest, ensure_ascii=False)}")
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
