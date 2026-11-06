"""写操作接口：广告投放处置 + 邮件闸门运维。

## 为什么单独一个 router，而不是塞进 `business.py`

`business.py` 的前提是"**不改变任何状态**"——它的路由级依赖只要求登录，
具体权限按资源挂在端点上，读起来是一张"能看什么"的清单。
在里面加一个会改 `ad_campaigns.status` 的接口，那个前提就没了：
读的人再也不能靠"它在 business.py 里"判断它是不是安全。

写操作还有三件读操作不需要的事：**权限更严、必须审计、可能冲突**。
集中在一个文件里，review 时只要看这一个文件就够了 ——
"系统里哪些接口会改状态"这个问题有唯一答案。

## 人做的 vs Agent 做的

同样是"改预算"：

* **人**在界面上点 → 这一下点击本身**就是**审批，直接落库，记 `approved_by`
* **Agent** 通过工具网关发起 → 落到 `pending_approval` 等人点

所以这里不需要"改预算申请单"这种新概念：人的操作自带审批属性，
再让他审一遍自己的操作只是多一次点击。反过来，Agent 发起的
一律进 `pending_approval`，由 `approve` / `reject` 这两个端点处置。
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Final

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from fashionops.db import get_db
from fashionops.db.base import utcnow
from fashionops.db.models import AdCampaign, Asset, EmailMessage
from fashionops.gateway.rate_limit import get_rate_limiter
from fashionops.mail import AttachmentInfo, MailGate, OutboundMessage, get_sender
from fashionops.mail.policy_store import (
    OVERRIDABLE,
    clear_overrides,
    current_policy,
    load_overrides,
    patch_overrides,
    validate_patch,
)
from fashionops.security.deps import get_principal, record_audit, require_permission
from fashionops.security.permissions import PERM_AD_WRITE, PERM_MAIL_ADMIN, PERM_MAIL_SEND
from fashionops.security.service import Principal
from fashionops.storage import get_storage

logger = logging.getLogger("fashionops.api.actions")

router = APIRouter(
    prefix="/api/v1",
    tags=["actions"],
    dependencies=[Depends(get_principal)],
)

#: 单日预算上限。不是业务规则，是防手滑多敲一个 0 ——
#: 广告预算打错一位数的代价是按天累计的，不是一次性的。
MAX_DAILY_BUDGET: Final = Decimal("1000000")
#: 止损备注 / 驳回原因的长度上限（和表里 reject_reason 的 String(255) 对齐）
MAX_REASON_LEN: Final = 255


# ===========================================================================
# 广告投放
# ===========================================================================
class CampaignActionOut(BaseModel):
    campaign_no: str
    status: str
    budget_daily: Decimal | None = None
    approved_by: str | None = None
    message: str


class BudgetChangeRequest(BaseModel):
    budget_daily: Decimal = Field(gt=0, description="新的日预算（元）")
    reason: str | None = Field(default=None, max_length=MAX_REASON_LEN, description="为什么改")


class DecisionRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=MAX_REASON_LEN, description="备注/原因")


def _require_campaign(session: Session, campaign_no: str) -> AdCampaign:
    row = session.scalar(select(AdCampaign).where(AdCampaign.campaign_no == campaign_no))
    if row is None:
        raise HTTPException(status_code=404, detail=f"投放计划 {campaign_no} 不存在")
    return row


def _conflict(campaign: AdCampaign, expected: tuple[str, ...], action: str) -> None:
    """状态不对就 409。

    **用 409 而不是 400**：请求本身完全合法，是资源当前的状态和它冲突。
    前端据此能区分"我传错了参数"（400，要改代码）和"别人已经处理过了"（409，刷新列表即可）。
    """
    if campaign.status not in expected:
        raise HTTPException(
            status_code=409,
            detail=(
                f"计划 {campaign.campaign_no} 当前状态是 {campaign.status}，"
                f"不能执行「{action}」。可执行的状态：{' / '.join(expected)}"
            ),
        )


@router.post(
    "/campaigns/{campaign_no}/approve",
    response_model=CampaignActionOut,
    summary="批准 Agent 发起的投放/改预算申请（pending_approval → running）",
)
def approve_campaign(
    campaign_no: str,
    request: Request,
    payload: DecisionRequest | None = None,
    principal: Principal = Depends(require_permission(PERM_AD_WRITE)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> CampaignActionOut:
    campaign = _require_campaign(session, campaign_no)
    _conflict(campaign, ("pending_approval",), "批准")

    campaign.status = "running"
    campaign.approved_by = principal.username
    campaign.approved_at = utcnow()
    campaign.reject_reason = None

    record_audit(
        session,
        principal=principal,
        action="ad.approve",
        resource=f"/api/v1/campaigns/{campaign_no}/approve",
        detail={"campaign_no": campaign_no, "note": (payload.reason if payload else None)},
        request=request,
    )
    return CampaignActionOut(
        campaign_no=campaign.campaign_no,
        status=campaign.status,
        budget_daily=campaign.budget_daily,
        approved_by=campaign.approved_by,
        message="已批准，计划开始投放",
    )


@router.post(
    "/campaigns/{campaign_no}/reject",
    response_model=CampaignActionOut,
    summary="驳回 Agent 发起的申请（pending_approval → rejected）",
)
def reject_campaign(
    campaign_no: str,
    request: Request,
    payload: DecisionRequest | None = None,
    principal: Principal = Depends(require_permission(PERM_AD_WRITE)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> CampaignActionOut:
    campaign = _require_campaign(session, campaign_no)
    _conflict(campaign, ("pending_approval",), "驳回")

    reason = (payload.reason if payload else None) or ""
    if not reason.strip():
        raise HTTPException(status_code=400, detail="驳回必须写原因 —— 申请人要靠它知道该怎么改")

    campaign.status = "rejected"
    campaign.reject_reason = reason[:MAX_REASON_LEN]
    campaign.approved_by = principal.username
    campaign.approved_at = utcnow()

    record_audit(
        session,
        principal=principal,
        action="ad.reject",
        resource=f"/api/v1/campaigns/{campaign_no}/reject",
        detail={"campaign_no": campaign_no, "reason": campaign.reject_reason},
        request=request,
    )
    return CampaignActionOut(
        campaign_no=campaign.campaign_no,
        status=campaign.status,
        budget_daily=campaign.budget_daily,
        approved_by=campaign.approved_by,
        message="已驳回",
    )


@router.post(
    "/campaigns/{campaign_no}/pause",
    response_model=CampaignActionOut,
    summary="一键止损：立刻暂停投放（running/pending_approval → paused）",
)
def pause_campaign(
    campaign_no: str,
    request: Request,
    payload: DecisionRequest | None = None,
    principal: Principal = Depends(require_permission(PERM_AD_WRITE)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> CampaignActionOut:
    """止损是**唯一一个允许跳过审批的写操作**。

    亏损计划的代价按分钟累计，走一轮审批等于把闸口变成了亏损放大器。
    但它仍然是写操作，所以照样落审计、照样要求 `ad:write`。
    """
    campaign = _require_campaign(session, campaign_no)
    _conflict(campaign, ("running", "pending_approval"), "止损暂停")

    previous_status = campaign.status
    campaign.status = "paused"
    campaign.approved_by = principal.username
    campaign.approved_at = utcnow()

    record_audit(
        session,
        principal=principal,
        action="ad.pause",
        resource=f"/api/v1/campaigns/{campaign_no}/pause",
        detail={
            "campaign_no": campaign_no,
            "from_status": previous_status,
            "reason": (payload.reason if payload else None),
            "roi": str(campaign.roi) if campaign.roi is not None else None,
            "spend": str(campaign.spend),
        },
        request=request,
    )
    return CampaignActionOut(
        campaign_no=campaign.campaign_no,
        status=campaign.status,
        budget_daily=campaign.budget_daily,
        approved_by=campaign.approved_by,
        message="已暂停投放，止损完成",
    )


@router.post(
    "/campaigns/{campaign_no}/budget",
    response_model=CampaignActionOut,
    summary="改日预算（人操作 = 已审批，直接生效并留痕）",
)
def change_budget(
    campaign_no: str,
    payload: BudgetChangeRequest,
    request: Request,
    principal: Principal = Depends(require_permission(PERM_AD_WRITE)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> CampaignActionOut:
    campaign = _require_campaign(session, campaign_no)
    _conflict(campaign, ("draft", "pending_approval", "running", "paused", "rejected"), "改预算")

    if payload.budget_daily > MAX_DAILY_BUDGET:
        raise HTTPException(
            status_code=400,
            detail=f"日预算 {payload.budget_daily} 超过上限 {MAX_DAILY_BUDGET}，请确认是不是多输了位数",
        )

    previous = campaign.budget_daily
    campaign.budget_daily = payload.budget_daily
    campaign.approved_by = principal.username
    campaign.approved_at = utcnow()

    record_audit(
        session,
        principal=principal,
        action="ad.budget",
        resource=f"/api/v1/campaigns/{campaign_no}/budget",
        detail={
            "campaign_no": campaign_no,
            "from": str(previous),
            "to": str(payload.budget_daily),
            "reason": payload.reason,
        },
        request=request,
    )
    return CampaignActionOut(
        campaign_no=campaign.campaign_no,
        status=campaign.status,
        budget_daily=campaign.budget_daily,
        approved_by=campaign.approved_by,
        message=f"日预算已由 {previous} 改为 {payload.budget_daily}",
    )


# ===========================================================================
# 邮件闸门运维
# ===========================================================================
class MailPolicyPatch(BaseModel):
    """可改的字段。**传 null = 恢复 .env 的值**（不是"清空"）。"""

    mail_enabled: bool | None = None
    mail_dry_run: bool | None = None
    mail_send_allowlist: str | None = None
    mail_send_denylist: str | None = None
    mail_max_recipients: int | None = None
    mail_rate_limit_per_minute: int | None = None


class MailRetryOut(BaseModel):
    message_no: str
    status: str
    sent: bool
    decision: str
    reason: str
    hint: str
    backend: str | None = None
    retry_count: int


@router.get("/mail/policy", summary="当前生效的邮件策略（含哪些值被运行时覆盖）")
def get_mail_policy(
    _: Principal = Depends(require_permission(PERM_MAIL_SEND, PERM_MAIL_ADMIN)),  # noqa: B008
) -> dict[str, Any]:
    summary = current_policy().mail_policy_summary()
    summary["overridable_keys"] = sorted(OVERRIDABLE)
    return summary


@router.patch("/mail/policy", summary="改邮件策略：白名单/黑名单、演练转真发")
def update_mail_policy(
    payload: MailPolicyPatch,
    request: Request,
    principal: Principal = Depends(require_permission(PERM_MAIL_ADMIN)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """关掉 dry_run 是**对外通道的开闸动作**，所以每一次都要留痕：
    事后被问"这封信是谁改成真发的"必须能答出来。

    只接受 `mail:admin`，不接受 `mail:send`：能发信的人若能自己放宽白名单，
    白名单就只是装饰。
    """
    patch = payload.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(status_code=400, detail="没有要改的字段")

    accepted, errors = validate_patch(patch)
    if accepted:
        patch_overrides(accepted)

    # 关 dry_run（转真发）是这里最危险的一个动作，单独打一条 warning 级日志：
    # 它意味着"从这一刻起 Agent 真的能对外发信了"
    if accepted.get("mail_dry_run") is False:
        logger.warning(
            "邮件策略：dry_run 已被 %s 关闭（转为真发），白名单=%s",
            principal.username,
            accepted.get("mail_send_allowlist") or "（沿用 .env）",
        )

    record_audit(
        session,
        principal=principal,
        action="mail.policy",
        resource="/api/v1/mail/policy",
        detail={"accepted": accepted, "rejected": errors},
        status="success" if not errors else "error",
        request=request,
    )

    summary = current_policy().mail_policy_summary()
    summary["accepted"] = accepted
    summary["errors"] = errors
    return summary


@router.delete("/mail/policy/overrides", summary="清空运行时覆盖，全部回到 .env")
def reset_mail_policy(
    request: Request,
    principal: Principal = Depends(require_permission(PERM_MAIL_ADMIN)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    cleared = load_overrides()
    clear_overrides()
    record_audit(
        session,
        principal=principal,
        action="mail.policy_reset",
        resource="/api/v1/mail/policy/overrides",
        detail={"cleared": cleared},
        request=request,
    )
    return {"ok": True, "cleared": cleared, "policy": current_policy().mail_policy_summary()}


@router.post(
    "/emails/{message_no}/retry",
    response_model=MailRetryOut,
    summary="重发一封邮件（重新过一遍闸门，被拦仍然记原因）",
)
def retry_email(
    message_no: str,
    request: Request,
    principal: Principal = Depends(require_permission(PERM_MAIL_SEND)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> MailRetryOut:
    """重发**必须重新过闸门**，不能"照着上一次的名单直接投"。

    典型场景正是"上次因为不在白名单被拦，管理员把域名加进白名单后重发"——
    跳过闸门的话，这封信会绕开白名单直接出去，管理员加白名单这个动作
    反而成了绕过审计的通道。

    另一个不能跳过的理由是限流：重发是自己触发的第二次投递，
    不重新计数就等于"点几次重发就能突破每分钟上限"。
    """
    row = session.scalar(select(EmailMessage).where(EmailMessage.message_no == message_no))
    if row is None:
        raise HTTPException(status_code=404, detail=f"邮件 {message_no} 不存在")
    if row.direction != "outbound":
        raise HTTPException(status_code=409, detail=f"邮件 {message_no} 是收到的信（inbound），不能重发")

    attachments = _load_email_attachments(session, row.attachments)
    policy = current_policy()
    gate = MailGate(policy, limiter=get_rate_limiter(policy.cache_backend))
    decision = gate.evaluate(
        to=list(row.to_addrs or []),
        cc=list(row.cc_addrs or []),
        bcc=list(row.bcc_addrs or []),
        attachments=attachments,
    )

    row.retry_count = int(row.retry_count or 0) + 1
    row.from_addr = row.from_addr or policy.mail_from
    row.gate_decision = decision.code
    row.gate_reason = decision.reason[:255] or None

    if decision.blocked:
        row.status = "blocked"
        row.error_message = decision.reason[:255]
        record_audit(
            session,
            principal=principal,
            action="mail.retry_blocked",
            resource=f"/api/v1/emails/{message_no}/retry",
            detail={"message_no": message_no, "decision": decision.code, "reason": decision.reason},
            status="blocked",
            request=request,
        )
        return MailRetryOut(
            message_no=message_no,
            status=row.status,
            sent=False,
            decision=decision.code,
            reason=decision.reason,
            hint=decision.hint,
            backend=None,
            retry_count=row.retry_count,
        )

    payloads: list[tuple[str, bytes]] = []
    for att in attachments:
        try:
            if att.storage_key:
                payloads.append((att.filename, get_storage().read(att.storage_key)))
        except (OSError, ValueError) as exc:
            logger.warning("重发 %s：附件 %s 读不出来，跳过：%s", message_no, att.filename, exc)

    sender = get_sender(policy)
    result = sender.send(
        OutboundMessage(
            from_addr=row.from_addr,
            to=decision.to,
            cc=decision.cc,
            bcc=decision.bcc,
            subject=row.subject or "（无主题）",
            body_text=row.body_text or "",
            body_html=None,
            attachments=tuple(payloads),
        )
    )

    if decision.code == "dry_run":
        row.status = "draft"
    elif result.ok:
        row.status = "sent"
        row.sent_at = utcnow()
    else:
        row.status = "failed"
    row.error_message = None if result.ok else (result.error or "投递失败")[:255]

    record_audit(
        session,
        principal=principal,
        action="mail.retry",
        resource=f"/api/v1/emails/{message_no}/retry",
        detail={
            "message_no": message_no,
            "decision": decision.code,
            "status": row.status,
            "recipients": list(decision.recipients),
            "retry_count": row.retry_count,
        },
        status="success" if result.ok else "error",
        request=request,
    )
    return MailRetryOut(
        message_no=message_no,
        status=row.status,
        sent=result.ok,
        decision=decision.code,
        reason=decision.reason,
        hint=decision.hint,
        backend=result.backend,
        retry_count=row.retry_count,
    )


def _load_email_attachments(session: Session, stored: list | None) -> list[AttachmentInfo]:
    """把库里存的附件元信息还原成闸门需要的 ``AttachmentInfo``。

    库里存的是 ``[{filename, asset_no, size}]``（**不含路径**）——
    重发时按 ``asset_no`` 回查 `assets` 表拿存储路径。
    为什么不直接存路径：路径是可变的（换存储后端就变了），
    而"这封信带了哪个素材"是不变的事实。
    """
    if not stored:
        return []
    asset_nos = [str(item.get("asset_no")) for item in stored if isinstance(item, dict) and item.get("asset_no")]
    if not asset_nos:
        return []
    rows = {
        a.asset_no: a
        for a in session.scalars(select(Asset).where(Asset.asset_no.in_(asset_nos))).all()
    }
    out: list[AttachmentInfo] = []
    for item in stored:
        if not isinstance(item, dict):
            continue
        asset = rows.get(str(item.get("asset_no") or ""))
        if asset is None:
            continue
        out.append(
            AttachmentInfo(
                filename=asset.name,
                size_bytes=int(asset.size_bytes or item.get("size") or 0),
                asset_no=asset.asset_no,
                storage_key=asset.storage_path,
            )
        )
    return out


__all__ = ["router"]
