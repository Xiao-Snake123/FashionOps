"""邮件工具：策略闸门 → 投递 → 落库。

## 这个工具的"记账"设计

**不论放行还是被拦，都要往 ``email_messages`` 插一行。** 这是这个模块
最重要的设计决策，也是面试时最能讲清楚的一段：

如果只记录"成功发出去的"，那么"被拦下来的"就完全不可见 ——
运维看到的是"这周只发了 12 封"，却不知道还有 8 封被策略挡住而业务方
以为已经通知了。**静默拦截比报错更危险**，因为没人会去查。

所以 ``gate_decision`` / ``gate_reason`` 一定落库，前端可以直接做一个
"被拦截邮件"列表，说清楚**哪一层拦的、为什么拦的**。

## 为什么标 idempotent=True

网关的幂等键是「任务 + 节点 + 入参摘要」。Agent 节点因为下游失败被重跑时，
同一封信不会发第二次 —— 重复发给客户两封一模一样的经营日报，
比不发更尴尬。代价是同一个入参在同一小时内发不出两封，
对"通知"这个语义来说这个代价是划算的。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from fashionops.config import get_settings
from fashionops.db import session_scope
from fashionops.db.base import utcnow
from fashionops.db.models import Asset, EmailMessage
from fashionops.gateway.rate_limit import RateLimiter, get_rate_limiter
from fashionops.gateway.registry import Scope, ToolRegistry
from fashionops.mail import AttachmentInfo, MailGate, OutboundMessage, get_sender
from fashionops.mail.policy_store import current_policy
from fashionops.storage import get_storage

#: 正文长度上限。不是技术限制，是防止模型把整个上下文倒进邮件正文
MAX_BODY_CHARS = 60_000


class MailPolicyArgs(BaseModel):
    """``mail.policy`` 不需要参数。

    **不能把 ``args_model`` 传 None**：网关会去调 ``args_model.model_json_schema()``，
    拿 `None` 会直接 `TypeError`。用一个空模型既表达"无参数"，
    又让工具对模型暴露的 schema 是个合法的空对象。
    """


class MailSendArgs(BaseModel):
    to_addrs: list[str] = Field(min_length=1, max_length=20, description="收件人列表")
    subject: str = Field(min_length=1, max_length=200, description="邮件主题")
    body_text: str = Field(min_length=1, max_length=MAX_BODY_CHARS, description="纯文本正文")
    cc_addrs: list[str] | None = Field(default=None, description="抄送，可选")
    bcc_addrs: list[str] | None = Field(default=None, description="密送，可选")
    body_html: str | None = Field(
        default=None, max_length=MAX_BODY_CHARS, description="HTML 正文，可选"
    )
    attachment_asset_nos: list[str] | None = Field(
        default=None,
        max_length=5,
        description="附件素材编号列表，如 ['AS2609120001']。**只能传素材编号，不能传路径**",
    )
    purpose: str | None = Field(
        default=None, max_length=200, description="这封信为什么发，便于事后复盘"
    )
    task_id: int | None = None


# ---------------------------------------------------------------------------
# 素材 → 附件
# ---------------------------------------------------------------------------
def _load_attachments(asset_nos: list[str]) -> tuple[list[AttachmentInfo], list[str]]:
    """按素材编号查出附件元信息。返回 (附件列表, 出错说明)。

    **只接受素材编号，不接受文件路径。** 让调用方传路径意味着 Agent
    （或者注入进来的提示词）可以指定服务器上任意文件 ——
    ``.env``、``data/checkpoints.db`` 都可能被打包发出去。
    素材编号只能在 ``assets`` 表里查到，表的写入又受 ``ASSET_WRITE`` 权限管着，
    等于把"能发什么"收敛到了"系统生成过什么"。
    """
    if not asset_nos:
        return [], []

    found: list[AttachmentInfo] = []
    missing: list[str] = []
    with session_scope() as session:
        rows = {
            a.asset_no: a
            for a in session.scalars(select(Asset).where(Asset.asset_no.in_(asset_nos))).all()
        }
    for asset_no in asset_nos:
        asset = rows.get(asset_no)
        if asset is None:
            missing.append(asset_no)
            continue
        found.append(
            AttachmentInfo(
                filename=asset.name,
                size_bytes=int(asset.size_bytes or 0),
                asset_no=asset.asset_no,
                storage_key=asset.storage_path,
            )
        )
    return found, missing


def _read_attachment_bytes(attachment: AttachmentInfo) -> bytes:
    """从对象存储读附件内容。

    注意调用时机：**一定在闸门放行之后**。先读再判的话，
    一个 3GB 的素材会在被拒绝之前就把内存吃光。
    """
    if not attachment.storage_key:
        raise ValueError(f"附件 {attachment.filename} 没有存储路径")
    return get_storage().read(attachment.storage_key)


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------
def _record(
    *,
    decision_code: str,
    decision_reason: str,
    status: str,
    to: list[str],
    cc: list[str] | None,
    bcc: list[str] | None,
    subject: str,
    body_text: str,
    body_html: str | None,
    attachments: list[AttachmentInfo],
    from_addr: str,
    task_id: int | None,
    sent_at: Any = None,
    error_message: str | None = None,
) -> str:
    with session_scope() as session:
        row = EmailMessage(
            message_no="TEMP",
            task_id=task_id,
            direction="outbound",
            to_addrs=list(to),
            cc_addrs=list(cc) if cc else None,
            bcc_addrs=list(bcc) if bcc else None,
            from_addr=from_addr,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            attachments=(
                [
                    {"filename": a.filename, "asset_no": a.asset_no, "size": a.size_bytes}
                    for a in attachments
                ]
                or None
            ),
            status=status,
            gate_decision=decision_code,
            gate_reason=decision_reason[:255] or None,
            sent_at=sent_at,
            error_message=error_message,
        )
        session.add(row)
        session.flush()
        # 用自增 id 拼业务编号，避免查 max 的并发问题
        row.message_no = f"EM{date.today():%y%m%d}{row.id:04d}"
        session.flush()
        return row.message_no


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------
def register(registry: ToolRegistry, limiter: RateLimiter | None = None) -> None:
    settings = get_settings()
    base_limiter = limiter or get_rate_limiter(settings.cache_backend)

    def _gate() -> MailGate:
        """**每次调用现取策略**，不在注册时抓一份。

        白名单 / dry_run 可以在运行时被管理员改掉（见 `mail/policy_store.py`）。
        注册时抓一份的话，改完必须重启进程才生效 ——
        而"界面上明明关了 dry_run，信还是没发出去"是极难定位的一类问题。
        闸门本身是无状态的价格判断，现造一个没有成本。
        """
        # 传 **register 时抓的那份 settings**（测试会替换模块级的 get_settings）：
        # 覆盖值仍然是每次现读的，所以运行时改策略照样即时生效
        return MailGate(current_policy(settings), limiter=base_limiter)

    @registry.tool(
        name="mail.policy",
        description=(
            "查看当前邮件发送策略：开关状态、白名单、黑名单、收件人上限、"
            "附件限制、是否 dry_run。发信前先查这个，避免做无效尝试。"
        ),
        args_model=MailPolicyArgs,
        scopes=(Scope.MAIL_SEND,),
        read_only=True,
        timeout_s=5.0,
        tags=("mail",),
    )
    def mail_policy() -> dict[str, Any]:
        return _gate().policy

    @registry.tool(
        name="mail.send",
        description=(
            "发送邮件通知。返回闸门判定结果与邮件编号。"
            "被策略拦下时不会抛异常，而是返回 decision 以 blocked_ 开头 + reason + hint，"
            "**邮件仍然会被记录**，请把 reason 转述给用户并走人工流程。"
        ),
        args_model=MailSendArgs,
        scopes=(Scope.MAIL_SEND,),
        read_only=False,
        # 重跑节点不该重复发同一封信，理由见模块 docstring
        idempotent=True,
        timeout_s=30.0,
        tags=("mail",),
        rate_limit_per_minute=10,
    )
    def mail_send(
        to_addrs: list[str],
        subject: str,
        body_text: str,
        cc_addrs: list[str] | None = None,
        bcc_addrs: list[str] | None = None,
        body_html: str | None = None,
        attachment_asset_nos: list[str] | None = None,
        purpose: str | None = None,
        task_id: int | None = None,
    ) -> dict[str, Any]:
        attachments, missing = _load_attachments(list(attachment_asset_nos or []))

        # ---- 闸门 ----
        decision = _gate().evaluate(
            to=list(to_addrs),
            cc=list(cc_addrs or []),
            bcc=list(bcc_addrs or []),
            attachments=attachments,
        )

        common = {
            "to": list(to_addrs),
            "cc": cc_addrs,
            "bcc": bcc_addrs,
            "subject": subject,
            "body_text": body_text,
            "body_html": body_html,
            "attachments": attachments,
            "from_addr": settings.mail_from,
            "task_id": task_id,
        }

        # ---- 被拦：记账后返回，不抛异常 ----
        if decision.blocked:
            message_no = _record(
                decision_code=decision.code,
                decision_reason=decision.reason,
                status="blocked",
                **common,
            )
            return {
                "message_no": message_no,
                "decision": decision.code,
                "status": "blocked",
                "sent": False,
                "backend": None,
                "detail": "",
                "error": "",
                "reason": decision.reason,
                "hint": decision.hint,
                "to": list(decision.to),
                "cc": list(decision.cc),
                "bcc": list(decision.bcc),
                "recipients": list(decision.recipients),
                # 被拦时这两个字段固定为空值，让返回结构在分支间保持一致 ——
                # 调用方（模型）不用去猜“这个分支有没有这个键”
                "attachment_count": 0,
                "checks_passed": list(decision.checks),
                "missing_assets": missing,
                "purpose": purpose,
            }

        # ---- 放行：此时才真正读附件字节 ----
        payloads: list[tuple[str, bytes]] = []
        read_errors: list[str] = []
        for att in attachments:
            try:
                payloads.append((att.filename, _read_attachment_bytes(att)))
            except (OSError, ValueError) as exc:
                read_errors.append(f"{att.filename}: {exc}")

        # 投递后端也要按**生效**策略选：dry_run 关掉之后必须真的走 SMTP
        sender = get_sender(current_policy(settings))
        result = sender.send(
            OutboundMessage(
                from_addr=settings.mail_from,
                # 用闸门清洗后的名单：它已经做过跨字段去重，
                # 而 BCC 只进投递名单、不进邮件头部
                to=decision.to,
                cc=decision.cc,
                bcc=decision.bcc,
                subject=subject,
                body_text=body_text,
                body_html=body_html,
                attachments=tuple(payloads),
            )
        )

        # dry_run 标记为 draft：内容写好了但没投递出去。
        # 用 sent 会是谎报，用 queued 又暗示"稍后会发"，都不准确。
        if decision.code == "dry_run":
            status = "draft"
        elif result.ok:
            status = "sent"
        else:
            status = "failed"

        message_no = _record(
            decision_code=decision.code,
            decision_reason=decision.reason if result.ok else (result.error or decision.reason),
            status=status,
            sent_at=utcnow() if status == "sent" else None,
            error_message=None if result.ok else (result.error or "投递失败"),
            **common,
        )

        return {
            "message_no": message_no,
            "decision": decision.code,
            "status": status,
            "sent": status == "sent",
            "backend": result.backend,
            "detail": result.detail,
            "error": result.error,
            "reason": decision.reason,
            "hint": decision.hint,
            "to": list(decision.to),
            "cc": list(decision.cc),
            "bcc": list(decision.bcc),
            "recipients": list(decision.recipients),
            "attachment_count": len(payloads),
            "attachment_read_errors": read_errors,
            "missing_assets": missing,
            "checks_passed": list(decision.checks),
            "purpose": purpose,
        }
