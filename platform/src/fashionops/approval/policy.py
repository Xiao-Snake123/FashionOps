"""人工审批策略 —— 对话 Agent 与任务 Agent 共用的唯一实现。

从 `chat/tools.py` 抽出来的（见 docs/12-AI协作交接.md T1）。
为什么抽出：新架构里任务侧也要判"这个工具要不要过人工闸口"，
两侧必须用**同一份判据** —— 两份实现迟早分叉，分叉之后
"对话里要确认、任务里不要确认"这种事没人说得清。

## 目录与策略是两件事

1. **目录**：模型能看到哪些工具（受调用者权限约束）—— 留在 `chat/tools.py`
2. **策略**：哪些工具在真正执行前必须过人工闸口 —— 在这里

分开是因为它们的判据完全不同：目录看"这个人有没有资格"，
策略看"这个动作能不能撤销"。一个运营专员完全有资格发邮件（目录里有），
但每一次发出去仍然要人点确认（策略要求）。

## 审批的判据不是"是不是写操作"，而是"做完能不能撤销"

* `mail.send` —— 邮件发出去**收不回来**。必须确认。
* `task.submit` —— 只是入队，而且任务自己还有一道闸口。**不确认**。
  两道闸口叠在一起，人会开始无脑点"同意"，闸口就退化成形式主义 ——
  这正是这个项目一直在反对的东西。
* 其余 `read_only=False` 的工具 —— **默认确认**。

## 默认从严

漏了一个新加的写工具没进确认名单，代价是"无人值守地改了线上数据"；
反过来，把一个只读工具误判成写工具，代价只是"多点一次确认"。
这两个代价不对等，所以默认值选"要确认"。
"""

from __future__ import annotations

from typing import Any

from fashionops.gateway.registry import ToolNotFound, registry

#: 写工具名（网关内部命名，点号分隔）
MAIL_SEND = "mail.send"

#: **不需要**人工确认的写工具。见模块顶部对判据的说明。
#:
#: 这是一个**例外名单**，不是"写工具名单"。
#: 新增写工具时，什么都不用做就会自动进入确认流程 —— 这是刻意的。
APPROVAL_EXEMPT: frozenset[str] = frozenset({"task.submit"})


def needs_approval(tool_name: str) -> bool:
    """这个工具在真正执行前要不要人工确认。"""
    if tool_name in APPROVAL_EXEMPT:
        return False
    try:
        spec = registry.get(tool_name)
    except ToolNotFound:
        # 没注册的工具根本执行不了（网关会返回 TOOL_NOT_FOUND），
        # 走到这里说明是内部调用出的错。
        # 判成"要确认"而不是"不要" —— 未知的东西按危险处理。
        return True
    return not spec.read_only


# ===========================================================================
# 给人看的摘要
# ===========================================================================
#: 参数名 → 中文标签。只在通用摘要里用。
_FIELD_LABELS: dict[str, str] = {
    "to_addrs": "收件人",
    "cc_addrs": "抄送",
    "bcc_addrs": "密送",
    "subject": "主题",
    "body_text": "正文",
    "body_html": "HTML 正文",
    "attachment_asset_nos": "附件",
    "purpose": "发信原因",
    "sku_codes": "SKU",
    "category": "类目",
    "days": "回溯天数",
    "query": "查询内容",
}

#: 正文在摘要里最多显示多少字
_BODY_PREVIEW_CHARS = 160


def describe_preview(tool_name: str, arguments: dict[str, Any] | None) -> str:
    """把工具参数变成人能快速判断的一段话。

    ## 为什么不能直接把 JSON 甩给用户

    让运营去读 ``{"to":["a@b.com"],"body":"...."}`` 然后点"同意"——
    那不叫审批，那叫**背锅**。审批要成立，前提是人能在几秒内看懂
    "它到底要干什么"。

    所以这里为高风险工具（目前就是发信）写了专门的摘要格式，
    其余工具退回通用格式（字段名 + 值）。
    """
    args = arguments or {}
    if tool_name == MAIL_SEND:
        return _preview_mail(args)
    return _preview_generic(tool_name, args)


def _preview_mail(args: dict[str, Any]) -> str:
    lines: list[str] = []

    def _join(value: Any) -> str:
        if isinstance(value, list):
            return "、".join(str(v) for v in value) or "（无）"
        return str(value) if value else "（无）"

    lines.append(f"收件人：{_join(args.get('to_addrs'))}")
    if args.get("cc_addrs"):
        lines.append(f"抄送：{_join(args['cc_addrs'])}")

    subject = str(args.get("subject") or "").strip() or "（无主题）"
    lines.append(f"主题：{subject}")

    body = str(args.get("body_text") or "").strip().replace("\r\n", "\n")
    if body:
        flat = " ".join(body.split())
        if len(flat) > _BODY_PREVIEW_CHARS:
            flat = flat[:_BODY_PREVIEW_CHARS] + "…"
        lines.append(f"正文（{len(body)} 字）：{flat}")
    if args.get("attachment_asset_nos"):
        lines.append(f"附件：{_join(args['attachment_asset_nos'])}")
    if args.get("purpose"):
        lines.append(f"发信原因：{args['purpose']}")
    return "\n".join(lines)


def _preview_generic(tool_name: str, args: dict[str, Any]) -> str:
    if not args:
        return f"{tool_name}（无参数）"
    parts: list[str] = []
    for key, value in args.items():
        label = _FIELD_LABELS.get(key, key)
        if value is None or value == "":
            continue
        if isinstance(value, list):
            rendered = "、".join(str(v) for v in value)
        elif isinstance(value, dict):
            rendered = ", ".join(f"{k}={v}" for k, v in value.items())
        else:
            rendered = str(value)
        if len(rendered) > _BODY_PREVIEW_CHARS:
            rendered = rendered[:_BODY_PREVIEW_CHARS] + "…"
        parts.append(f"{label}：{rendered}")
    return f"{tool_name}\n" + "\n".join(parts) if parts else f"{tool_name}（无有效参数）"
