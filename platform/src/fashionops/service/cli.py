"""客服工单命令行 —— ``fashionops-ticket``。

用法::

    fashionops-ticket export-labels          # ★ 处理工单**之前**必须先跑一次
    fashionops-ticket handle TK2608250001    # 处理一条，打印完整判断过程
    fashionops-ticket handle-pending --limit 10
    fashionops-ticket eval                   # 和评测集对照，算准确率

## 为什么必须有一个 `export-labels`

因为 seed 的标注会被处理覆盖掉。这个命令存在的唯一目的就是在覆盖前把它存下来，
所以它的输出会明确提示"这一步要在任何时候处理工单之前执行"。
"""

from __future__ import annotations

import sys
from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import Any

from sqlalchemy import select

from fashionops.db import session_scope
from fashionops.db.models import Ticket

#: `handle-pending` 默认处理多少条。
#: 默认 10 而不是"全部"：一条工单要几秒，60 条就是几分钟，
#: 而 CLI 上你只能干等着。要全跑就显式给 `--limit 0`。
DEFAULT_BATCH = 10


def _ok(text: str) -> None:
    print(f"✓ {text}")


def _warn(text: str) -> None:
    print(f"⚠ {text}")


def _print_outcome(outcome: Any) -> None:
    print()
    print("─" * 72)
    print(f"工单 {outcome.ticket_no}")
    print("─" * 72)
    print(f"  通道      : {outcome.lane}  （{outcome.lane_reason}）")
    print(f"  意图      : {outcome.intent or '(未判出)'}")
    print(f"  情绪      : {outcome.sentiment}")
    print(f"  置信度    : {outcome.confidence:.2f}")
    print(f"  转人工    : {'是' if outcome.escalated else '否'}")
    if outcome.escalate_reason:
        print(f"  转人工原因: {outcome.escalate_reason}")
    print(f"  知识库依据: {outcome.cited_article_nos or '（无）'}")
    print(f"  开销      : {outcome.steps} 轮 / {outcome.tool_calls} 次工具 / "
          f"{outcome.tokens} tokens / ¥{outcome.cost} / {outcome.duration_ms}ms")
    if outcome.error:
        print(f"  错误      : {outcome.error}")
    print()
    print("  回复草稿：")
    for line in (outcome.reply_draft or "(空)").splitlines():
        print(f"    {line}")


def cmd_export_labels(args: Namespace) -> int:
    from fashionops.service.labels import export_labels

    result = export_labels(path=Path(args.path) if args.path else None)
    _ok(f"评测集已导出：{result['path']}（{result['count']} 条）")
    _warn(
        "这份文件是**故意要进 git 的**（评测集要能版本化），"
        "但 data/ 在 gitignore 里，所以需要手动：git add -f " + result["path"]
    )
    return 0


def cmd_handle(args: Namespace) -> int:
    from fashionops.service.agent import handle_ticket

    try:
        outcome = handle_ticket(args.ticket_no, persist=not args.dry_run)
    except LookupError as exc:
        print(f"✗ {exc}")
        return 1
    _print_outcome(outcome)
    if args.dry_run:
        _warn("--dry-run：结果没有写回数据库")
    else:
        _ok(f"已写回 {outcome.ticket_no}（status={outcome.status if hasattr(outcome, 'status') else ''}）")
    return 0


def cmd_handle_pending(args: Namespace) -> int:
    from fashionops.service.agent import build_service_context, handle_ticket

    with session_scope() as session:
        stmt = select(Ticket).order_by(Ticket.created_at)
        if not args.all:
            stmt = stmt.where(Ticket.status.in_(("open", "processing")))
        if args.limit:
            stmt = stmt.limit(args.limit)
        ticket_nos = [r.ticket_no for r in session.scalars(stmt)]

    if not ticket_nos:
        _ok("没有待处理的工单（要重跑全部加 --all）")
        return 0

    # 复用同一个上下文：网关带回的限流/熔断状态要在这一批里共享，
    # 不然"批处理"就看不出下游的真实压力
    ctx = build_service_context()
    print(f"开始处理 {len(ticket_nos)} 条工单…")

    fast = 0
    agent = 0
    escalated = 0
    failed = 0
    tokens = 0
    for index, ticket_no in enumerate(ticket_nos, 1):
        try:
            outcome = handle_ticket(ticket_no, ctx=ctx, persist=not args.dry_run)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  [{index}/{len(ticket_nos)}] {ticket_no}  ✗ {type(exc).__name__}: {exc}")
            continue
        if outcome.lane == "fast":
            fast += 1
        else:
            agent += 1
        if outcome.escalated:
            escalated += 1
        if outcome.error:
            failed += 1
        tokens += outcome.tokens
        mark = "→人工" if outcome.escalated else "自动"
        print(
            f"  [{index}/{len(ticket_nos)}] {ticket_no}  "
            f"{outcome.lane:<5} {outcome.intent or '未判出':<5} {mark}  "
            f"{outcome.confidence:.2f}  {outcome.duration_ms}ms"
        )

    print()
    print("─" * 72)
    print(
        f"快车道 {fast} / Agent {agent} / 转人工 {escalated} / 失败 {failed} "
        f"/ 合计 {tokens} tokens"
    )
    if args.dry_run:
        _warn("--dry-run：结果没有写回数据库")
    return 0


def cmd_eval(args: Namespace) -> int:
    from fashionops.service.labels import evaluate

    try:
        report = evaluate(path=Path(args.path) if args.path else None)
    except FileNotFoundError as exc:
        print(f"✗ {exc}")
        return 1

    print()
    print("=" * 72)
    print(f"工单评测（已处理 {report['total']} / 库里共 {report['total_in_db']} 条）")
    print("=" * 72)
    if report["total"] == 0:
        _warn("还没有处理过任何工单，没有可比的东西。先跑 handle-pending")
        return 0
    print(f"  意图准确率   : {report['intent_accuracy']}%  ({report['intent_ok']}/{report['total']})")
    print(f"  情绪准确率   : {report['sentiment_accuracy']}%")
    print(f"  **漏转**（危险）: {len(report['escalation_missed'])} 条 {report['escalation_missed'][:10]}")
    print(f"  多转（安全） : {len(report['escalation_over'])} 条")
    print(
        "  注：只统计**已处理**的。没处理过的还留着 seed 标注，"
        "拿它算准确率等于自己对自己"
    )

    if report["intent_misses"]:
        print()
        print("  意图判错的：")
        for item in report["intent_misses"][:15]:
            print(f"    {item['ticket_no']}  标注={item['标注']:<6} 实际={item['实际']}")
    if report["escalation_missed"]:
        print()
        _warn("有漏转 —— 这是危险方向，必须逐条看为什么没转")
    else:
        print()
        _ok("漏转 0 条（危险方向为零）")
    print()
    print(f"  评测集说明：{report['labels_note']}")
    return 0


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="fashionops-ticket", description="客服工单：两层处理 + 评测")
    parser.add_argument("--path", default=None, help="评测集路径（默认 data/eval/ticket_labels.json）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "export-labels",
        help="把当前工单表的标注导出成评测集（★ 处理工单之前必须先跑）",
    ).set_defaults(func=cmd_export_labels)

    p_handle = sub.add_parser("handle", help="处理一条工单")
    p_handle.add_argument("ticket_no")
    p_handle.add_argument("--dry-run", action="store_true", help="只算不写库")
    p_handle.set_defaults(func=cmd_handle)

    p_pending = sub.add_parser("handle-pending", help="批量处理待处理工单")
    p_pending.add_argument("--all", action="store_true", help="连已处理的也重跑一遍")
    p_pending.add_argument("--limit", type=int, default=DEFAULT_BATCH, help="最多处理几条（0 = 不限）")
    p_pending.add_argument("--dry-run", action="store_true", help="只算不写库")
    p_pending.set_defaults(func=cmd_handle_pending)

    sub.add_parser("eval", help="和评测集对照，算准确率").set_defaults(func=cmd_eval)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # --path 既能放在子命令前也能放在后面，统一处理一次
    if not hasattr(args, "path"):
        args.path = None
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
