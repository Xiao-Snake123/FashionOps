"""邮件通知 Agent —— 把上游结论组织成一封信，经闸门后投递。

## 分工（与其他 Agent 一致）

| 环节 | 谁做 | 为什么 |
| --- | --- | --- |
| 收件人解析 | **模型** | 用户可能说"发给运营组那几个人"，只有模型能理解 |
| 主题与正文措辞 | **模型** | 自然语言表达是它的主场 |
| 附件选择 | **代码** | 只能挂系统真的产出过的素材（``assets`` 表里的编号） |
| 该不该发、能不能发 | **代码** | 白名单/黑名单/大小/限流是策略，不是创作 |
| 落库记账 | **代码** | 放行和被拦**都要记**，否则"被拦的邮件"完全不可见 |

## 这个 Agent 最关键的设计：被拦不是失败

邮件被闸门拦下来时，节点**仍然是 succeeded**。这一点必须说清楚：

* 从"节点执行"的角度看，它完成了自己的工作 —— 正确调用工具、正确处理结果
* 从"业务"角度看，这封信没发出去，需要**人工接手**
* 所以它产出的是一个 ``ApprovalRequest``（提醒人去做），而不是一个异常

把"被策略拦截"当成异常会让上游重试 —— 而重试一万次也不会让白名单多出一条配置。
**这是最典型的"重试解决不了的问题"，必须走人工。**
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from fashionops.agents.state import (
    ApprovalRequest,
    RunContext,
    TaskState,
    check_budget,
    step_update,
)
from fashionops.gateway import CallContext
from fashionops.llm import Message, structured_call

#: 邮箱地址。刻意写得宽松 —— 目的是从自然语言里"捞"出地址，
#: 严格校验留给闸门（那里失败会落库并说明原因，比在正则里拒掉更好排查）。
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


# ---------------------------------------------------------------------------
# 结构化输出
# ---------------------------------------------------------------------------
class EmailDraft(BaseModel):
    """模型负责写的部分：收件人、主题、正文。"""

    recipients: list[str] = Field(
        default_factory=list,
        description=(
            "收件人地址列表，从用户需求里提取。例如「发给 ops@example.com」→ "
            "['ops@example.com']。需求里没提到任何地址就留空"
        ),
    )
    cc: list[str] = Field(default_factory=list, description="抄送地址，没提到就留空")
    subject: str = Field(
        max_length=120,
        description="邮件主题，要能一眼看出是什么事。带【】前缀，如「【日报】昨日经营数据」",
    )
    body_text: str = Field(
        description=(
            "纯文本正文。要求：\n"
            "1. 开头一句结论，不要用「您好，以下是我们分析的结果」这种废话开场\n"
            "2. 关键数字必须与给定数据完全一致，不要自己算、不要四舍五入成整十整百\n"
            "3. 有 critical 告警就先说告警，再说常规数据\n"
            "4. 结尾说明附件是什么、需要收件人做什么\n"
            "5. 不要出现 markdown 语法符号（**、##、|表格），这是纯文本邮件"
        ),
    )
    tone_note: str = Field(default="", description="一句话说明你为什么这么措辞，给人看的备注")


_SYSTEM = (
    "你是服装电商运营团队的中台助手，负责把系统产出的分析结论写成一封能直接发出去的邮件。\n"
    "硬性要求：\n"
    "1. 所有数字必须来自给定的数据，**不要重新计算、不要改动**\n"
    "2. 系统已经算好了 alerts（告警）。有 critical 就先讲 critical，再讲常规数据\n"
    "3. 不要虚构任何未在数据中出现的指标、商品或渠道\n"
    "4. 正文是纯文本，不要用 markdown 标记\n"
    "5. 如果上游结论里有「样本不足」的提示，不要把它当趋势来写\n"
    "6. 收件人只能来自用户需求里明确提到的地址，不要自己编造邮箱\n"
)


# ---------------------------------------------------------------------------
# 节点工厂
# ---------------------------------------------------------------------------
def make_notification_node(
    ctx: RunContext,
    *,
    recipients: list[str] | None = None,
    cc: list[str] | None = None,
    purpose: str = "经营分析日报",
    progress_done: int = 100,
) -> Callable[[TaskState], dict[str, Any]]:
    """构造邮件通知节点。

    ``recipients`` 显式给定时就不再让模型从需求里猜 ——
    编排层（总控 Agent）通常已经知道该发给谁，让它猜反而多一次失败机会。
    """

    def notification_node(state: TaskState) -> dict[str, Any]:
        started = time.perf_counter()
        check_budget(state)

        tool_names: list[str] = []
        tokens = 0
        cost = Decimal("0")
        llm_calls = 0

        analytics = state.get("findings", {}).get("analytics")
        if not analytics:
            return step_update(
                state, node="notification", agent="notification", status="failed",
                summary="上游没有产出分析结论，无法起草邮件",
                tool_calls=tool_names, duration_ms=_ms(started),
                error="缺少 findings['analytics']",
                extra={"errors": ["notification 缺少上游分析结论"]},
            )

        draft, response = _draft(ctx, state, analytics, recipients)
        if response is not None:
            tokens += response.usage.total_tokens
            cost += response.cost
            llm_calls += 1

        if draft is None:
            return step_update(
                state, node="notification", agent="notification", status="failed",
                summary="模型未能产出合规的邮件草稿（结构化输出多次校验失败）",
                tool_calls=tool_names, duration_ms=_ms(started),
                tokens=tokens, cost=cost, llm_calls=llm_calls,
                error="结构化输出失败",
                extra={"errors": ["notification 结构化输出失败"]},
            )

        # 收件人优先级：调用方显式指定 > 模型提取 > 正则兜底
        #
        # 为什么要有第三层：实测 glm-4-flash 对「发给 ops@example.com」
        # 这种**明确写在需求里**的地址都没有稳定地提取出来。
        # 邮箱是完全规则的东西，正则能 100% 提对 —— 让它去靠模型的"理解"
        # 属于自找不确定性。这符合项目一以贯之的分工：
        # **能用代码可靠解决的，就不要交给模型。**
        model_picked = list(draft.recipients)
        fallback = _regex_recipients(state) if not (recipients or model_picked) else []
        to_addrs = list(recipients or model_picked or fallback)

        # ---- 附件：只能挂系统真的产出过的素材 ----
        asset_nos = _report_asset_nos(state, analytics)

        # ---- 没有收件人：在调工具之前就拦下来 ----
        #
        # 不拦的话会把空列表传给 ``mail.send``，网关回一个
        # ``[INVALID_ARGUMENTS] 参数校验失败：1 处不合法`` —— 看不出是哪个参数、
        # 更看不出"其实是你没给收件人"。这类"能跑但报错信息没用"的失败
        # 是真实事故排查里最耗时的一种，所以宁可在这里多写几行。
        #
        # 注意这**不是**重试能解决的问题（重试一百次需求里还是没地址），
        # 所以顺手产出一个审批请求，把已经写好的草稿一起带上 ——
        # 人只要补一个地址就能发出去，不用从头再来。
        if not to_addrs:
            return step_update(
                state, node="notification", agent="notification", status="failed",
                summary="缺少收件人地址：需求里没提到邮箱，调用方也没指定 recipients",
                tool_calls=tool_names, duration_ms=_ms(started),
                tokens=tokens, cost=cost, llm_calls=llm_calls,
                error="缺少收件人地址",
                extra={
                    "errors": ["notification 缺少收件人地址，邮件未发送"],
                    "approval": ApprovalRequest(
                        node="notification",
                        action=f"补上收件人后发送邮件：{draft.subject}",
                        reason=(
                            "模型没有从需求里提取到任何邮箱地址，调用方也未显式指定 "
                            "recipients。草稿已写好，补齐收件人即可发送。"
                        ),
                        risk="low",
                        payload={
                            "subject": draft.subject,
                            "body_text": draft.body_text,
                            "attachment_asset_nos": asset_nos,
                            "hint": "在 make_notification_node(..., recipients=[...]) 传入地址即可",
                        },
                        requested_by="notification_agent",
                    ),
                },
            )

        # ---- 走闸门 + 投递 ----
        send = ctx.gateway.call(
            "mail.send",
            {
                "to_addrs": to_addrs,
                "cc_addrs": list(cc or draft.cc),
                "subject": draft.subject,
                "body_text": draft.body_text,
                "attachment_asset_nos": asset_nos or None,
                "purpose": purpose,
                "task_id": ctx.task_id,
            },
            ctx=CallContext(
                actor=ctx.actor,
                scopes=ctx.scopes,
                task_id=ctx.task_id,
                trace_id=f"{state.get('task_no', '')}:notification",
                idempotency_key=(
                    f"{state.get('task_no', '')}:notification" if state.get("task_no") else None
                ),
            ),
        )
        tool_names.append("mail.send")

        if not send.ok:
            return step_update(
                state, node="notification", agent="notification", status="failed",
                summary=f"邮件工具调用失败：{send.error}",
                tool_calls=tool_names, duration_ms=_ms(started),
                tokens=tokens, cost=cost, llm_calls=llm_calls,
                error=str(send.error),
                extra={"errors": [f"mail.send 失败：{send.error}"]},
            )

        result = send.data
        sent = bool(result.get("sent"))
        decision = result.get("decision", "")

        findings = {
            "draft": {
                "subject": draft.subject,
                "recipients": result.get("to", to_addrs),
                "cc": result.get("cc", []),
                "body_chars": len(draft.body_text),
                "tone_note": draft.tone_note,
                # 记下收件人是怎么来的，排查时能一眼看出该不该信
                "recipient_source": (
                    "caller" if recipients else ("model" if model_picked else "regex_fallback")
                ),
            },
            "message_no": result.get("message_no"),
            "decision": decision,
            "status": result.get("status"),
            "sent": sent,
            "backend": result.get("backend"),
            "reason": result.get("reason"),
            "hint": result.get("hint"),
            "attachment_asset_nos": asset_nos,
            "missing_assets": result.get("missing_assets", []),
        }

        extra: dict[str, Any] = {
            "findings": {**state.get("findings", {}), "notification": findings}
        }

        if sent:
            summary = f"邮件已投递（{result.get('backend')}），主题：{draft.subject}"
        elif decision == "dry_run":
            summary = (
                f"邮件已生成但未投递（dry_run），编号 {result.get('message_no')}，"
                f"主题：{draft.subject}"
            )
        else:
            # 被闸门拦下：节点本身没有失败，但业务上需要人工接手。
            # 这里产出一个审批请求，让"要发但发不出去"这件事在界面上可见。
            summary = f"邮件被策略拦截（{decision}）：{result.get('reason')}"
            extra["approval"] = ApprovalRequest(
                node="notification",
                action=f"人工发送邮件：{draft.subject}",
                reason=(
                    f"自动发送被策略拦截（{decision}）。原因：{result.get('reason')}。"
                    f"提示：{result.get('hint')}"
                ),
                risk="low",
                payload={
                    "message_no": result.get("message_no"),
                    "to": result.get("to", to_addrs),
                    "subject": draft.subject,
                    "body_text": draft.body_text,
                    "attachment_asset_nos": asset_nos,
                    "gate_decision": decision,
                },
                requested_by="notification_agent",
            )
            extra["pending_approval"] = False
            extra["errors"] = [
                *state.get("errors", []),
                f"邮件未发出：{result.get('reason')}",
            ]

        return step_update(
            state, node="notification", agent="notification",
            # 不论发没发出去，节点都算执行成功 —— 策略拦截不是技术故障，
            # 重试一万次也不会让白名单多一条配置
            status="succeeded",
            summary=summary,
            tool_calls=tool_names, duration_ms=_ms(started),
            tokens=tokens, cost=cost, llm_calls=llm_calls,
            progress=progress_done,
            extra=extra,
        )

    return notification_node


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _report_asset_nos(state: TaskState, analytics: dict[str, Any]) -> list[str]:
    """找出要挂的报表素材编号。

    优先信状态里的 ``artifacts``（那是编排层认可过的产出）；
    退而查 ``findings['analytics']['report']``。
    两条路都只拿 ``asset_no``，绝不拿路径。
    """
    out: list[str] = []
    for artifact in state.get("artifacts", []) or []:
        ref = getattr(artifact, "ref", None)
        if getattr(artifact, "kind", None) == "report" and ref:
            out.append(str(ref))
    if not out:
        report = analytics.get("report") or {}
        if report.get("asset_no"):
            out.append(str(report["asset_no"]))
    # 去重但保序
    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def _draft(
    ctx: RunContext,
    state: TaskState,
    analytics: dict[str, Any],
    recipients: list[str] | None,
) -> tuple[EmailDraft | None, Any]:
    """让模型起草邮件。只喂精炼结论，不喂原始数据。"""
    payload = {
        "user_request": state.get("user_request", ""),
        "given_recipients": recipients or [],
        "window_days": analytics.get("window_days"),
        "totals": analytics.get("totals"),
        "by_category": analytics.get("by_category", []),
        "by_channel": analytics.get("by_channel", []),
        # 告警是代码算的，直接当已知事实给模型
        "alerts": analytics.get("alerts", []),
        "critical_count": analytics.get("critical_count", 0),
        "system_summary": analytics.get("summary", ""),
        "attachment": (analytics.get("report") or {}).get("filename"),
        # 上游选品结论只给摘要，不给完整候选明细
        "upstream_selection": _compact(state.get("findings", {}).get("selection")),
    }
    result = structured_call(
        ctx.model,
        [
            Message.system(_SYSTEM),
            Message.user(
                "请根据下面的数据写一封通知邮件。数据已由系统算好，不要重新计算。\n"
                f"{_json(payload)}"
            ),
        ],
        EmailDraft,
    )
    return (result.value if result.ok else None), result.response  # type: ignore[return-value]


def _regex_recipients(state: TaskState) -> list[str]:
    """从用户需求里正则提取邮箱，作为模型没提取出来时的兜底。"""
    seen: set[str] = set()
    out: list[str] = []
    for address in _EMAIL_RE.findall(state.get("user_request", "") or ""):
        key = address.lower()
        if key not in seen:
            seen.add(key)
            out.append(address)
    return out


def _compact(prior: Any) -> Any:
    """上游结论压成摘要字段，避免把完整候选明细塞进上下文。"""
    if not isinstance(prior, dict):
        return None
    keys = ("criteria", "total_input", "passed", "filtered_out", "overall")
    return {k: prior[k] for k in keys if k in prior}


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


__all__ = [
    "EmailDraft",
    "make_notification_node",
]
