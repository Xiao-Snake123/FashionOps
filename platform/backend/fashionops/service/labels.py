"""工单评测集 —— 导出标注、算准确率。

## 为什么必须在处理之前导出

seed 已经往 `tickets` 表里灌了 `intent` / `sentiment` / `escalated` 三列标注。
而 Agent 处理工单时**会覆盖这三列**（它就是要写出自己的判断）。

一旦覆盖：
* 原来的标注没了 → 之后再也算不出"判对了多少"
* 而"判对了多少"正是这个模块唯一能给出的**可验证数字**

所以顺序是死的：**先 `export-labels`，再 handle**。

## 为什么不加几列专门存标注（比如 `label_intent`）

那是更"正规"的做法（标注和预测分列存）。但代价是改表 + 迁移，
而这个表的列是当初特意设计好给这个 Agent 用的，不想为了评测污染它。

导出成一份**版本化的 JSON 文件**还有个额外好处：
评测集能进 git，改了规则之后 diff 里能直接看到"哪几条变了"。
标注放在表里就没有这个能力。

## 关于这个评测集的诚实说明

seed 的 `escalated` 是这么生成的：

    escalated = is_angry or (intent in ("质量投诉", "退换货") and random() < 0.35)

**那个 35% 是随机的** —— 也就是说这部分标注**本身没有规则可依**。
所以"多转/漏转"的数字要分开看：

* **漏转**（规则没转、标注转了）：危险方向，必须逐条查清
* **多转**（规则转了、标注没转）：安全方向，其中相当一部分是"标注随机没转"
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select

from fashionops.db import session_scope
from fashionops.db.models import Ticket

#: 评测集默认路径。放 `data/eval/` —— `data/` 已在 gitignore 里，
#: 但这一份是**故意要进 git 的**（见模块顶部），所以下面会提示手动 `git add -f`。
DEFAULT_LABELS_PATH = Path("data") / "eval" / "ticket_labels.json"


def export_labels(*, path: Path | None = None) -> dict[str, Any]:
    """把当前工单表里的标注导出成评测集。"""
    target = path or DEFAULT_LABELS_PATH
    with session_scope() as session:
        rows = list(session.scalars(select(Ticket).order_by(Ticket.ticket_no)))

    payload = {
        "note": (
            "seed 灌入的工单标注。escalated 里含 35% 随机成分，"
            "所以它只适合看意图/情绪准确率，转人工要看漏转而不是一致率。"
        ),
        "count": len(rows),
        "items": [
            {
                "ticket_no": r.ticket_no,
                "subject": r.subject,
                "content": r.content,
                "intent": r.intent,
                "sentiment": r.sentiment,
                "priority": r.priority,
                "escalated": bool(r.escalated),
                "escalate_reason": r.escalate_reason,
            }
            for r in rows
        ],
    }

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(target), "count": len(rows)}


def load_labels(*, path: Path | None = None) -> dict[str, Any]:
    target = path or DEFAULT_LABELS_PATH
    if not target.exists():
        raise FileNotFoundError(
            f"评测集不存在：{target}\n"
            "必须先跑 fashionops-ticket export-labels（而且要在任何时候处理工单**之前**）"
        )
    return json.loads(target.read_text(encoding="utf-8"))


def evaluate(*, path: Path | None = None) -> dict[str, Any]:
    """拿评测集对照当前工单表，算准确率。

    ## 只统计**已处理**的工单，否则数字是虚的

    未处理的工单还留着 seed 灌的标注，拿它跟评测集比 **必然相等** ——
    等于自己对自己，准确率会虚高。实测就踩到了：只处理了 8 条，
    却报出"意图准确率 100%（60/60）"，因为另外 52 条根本没被碰过。

    判据用 ``agent_handled_at``：只有客服 Agent 会写这一列。
    **不能**用 ``status`` —— seed 把它当"已处理的历史工单"灌了，
    status 分布从 open/processing/closed/transferred/waiting 全都有，
    拿它当判据会得到"已处理 58/60"这种荒谬结果（实测踩过）。
    """
    labels = load_labels(path=path)
    with session_scope() as session:
        rows = list(session.scalars(select(Ticket)))
    current = {r.ticket_no: r for r in rows}
    total_in_db = len(rows)

    total = 0
    intent_ok = 0
    sentiment_ok = 0
    missed: list[str] = []  # 漏转：危险方向
    over: list[str] = []  # 多转：安全方向
    intent_miss: list[dict[str, str]] = []
    lane_fast = 0

    for item in labels["items"]:
        row = current.get(item["ticket_no"])
        if row is None or row.agent_handled_at is None:
            # 没被 Agent 处理过：它身上的值还是 seed 的历史结果，
            # 拿来跟我们自己的输出比就是自己对自己
            continue
        total += 1
        if row.agent_lane == "fast":
            lane_fast += 1
        if row.intent == item["intent"]:
            intent_ok += 1
        else:
            intent_miss.append(
                {
                    "ticket_no": row.ticket_no,
                    "标注": item["intent"] or "",
                    "实际": row.intent or "(空)",
                }
            )
        if row.sentiment == item["sentiment"]:
            sentiment_ok += 1

        want = bool(item["escalated"])
        got = bool(row.escalated)
        if want and not got:
            missed.append(row.ticket_no)
        elif got and not want:
            over.append(row.ticket_no)

    def pct(n: int) -> float:
        return round(n / total * 100, 1) if total else 0.0

    return {
        "total": total,
        "total_in_db": total_in_db,
        "lane_fast": lane_fast,
        "intent_accuracy": pct(intent_ok),
        "intent_ok": intent_ok,
        "sentiment_accuracy": pct(sentiment_ok),
        "escalation_missed": missed,
        "escalation_over": over,
        "intent_misses": intent_miss,
        "labels_note": labels.get("note", ""),
    }


__all__ = ["DEFAULT_LABELS_PATH", "evaluate", "export_labels", "load_labels"]
