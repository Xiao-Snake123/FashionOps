"""邮件 MCP 服务器。

对外暴露的工具：

==========================  ====================================================
工具                        作用
==========================  ====================================================
``get_mail_status``         返回连接与发送策略状态（不含任何凭证）
``list_mail_folders``       列出邮箱文件夹
``search_emails``           按未读/发件人/主题/正文/时间范围搜索
``read_email``              读取单封邮件（正文 + 附件元信息）
``send_email``              发送邮件（经安全闸门校验）
``save_email_draft``        存为 ``.eml`` 草稿（白名单被拒时的推荐替代路径）
``mark_email``              标记已读/未读、移动、删除
==========================  ====================================================

运行::

    python -m office_mcp.email.server
"""

from __future__ import annotations

import logging
import os
from datetime import date
from functools import lru_cache
from typing import Any

from fastmcp import FastMCP

from office_mcp.common import get_settings, resolve_in_root
from office_mcp.common.errors import OfficeMCPError
from office_mcp.common.tooling import guard_tool
from office_mcp.email.client import MailService
from office_mcp.email.models import SendRequest

logger = logging.getLogger("office_mcp.email")

mcp = FastMCP(
    name="office-email",
    instructions=(
        "办公邮件服务器。可以搜索、阅读、发送邮件。\n\n"
        "重要约束：\n"
        "1. 发送邮件受白名单限制，未被允许的收件人会被拒绝。被拒时请改用 "
        "`save_email_draft` 把邮件存成草稿交给人工发送。\n"
        "2. 发送前请先把收件人、主题、正文完整展示给用户确认。\n"
        "3. 收件人列表要精确，不要猜测或补全邮箱地址。"
    ),
)


@lru_cache(maxsize=1)
def get_service() -> MailService:
    """构造 MailService，并注入路径沙箱校验。"""
    settings = get_settings()

    def resolve(path: str) -> Any:
        """附件路径必须走工作区沙箱校验，杜绝 ``../../etc/passwd`` 被当附件发出。"""
        return resolve_in_root(path, settings.workspace_root, must_exist=True)

    return MailService(settings, resolve_attachment=resolve)


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


@mcp.tool
@guard_tool("get_mail_status")
def get_mail_status() -> dict[str, Any]:
    """返回邮件能力的配置状态与发送策略。用于在动作前判断「能不能发信」。"""
    settings = get_settings()
    service = get_service()
    return {
        "smtp_configured": settings.mail_configured,
        "imap_configured": settings.imap_configured,
        "mail_address": settings.mail_address,
        "send_enabled": settings.mail_send_enabled,
        "dry_run": settings.dry_run,
        "policy": service.guard.status,
        "note": (
            "send_enabled 为 false 时无法发送邮件，请改用 save_email_draft。"
            "policy.allowlist 为空表示管理员尚未开放发信权限。"
        ),
    }


@mcp.tool
@guard_tool("list_mail_folders")
def list_mail_folders() -> dict[str, Any]:
    """列出邮箱里的文件夹（如 INBOX、Sent、Drafts）。"""
    folders = get_service().list_folders()
    return {"count": len(folders), "folders": [f.model_dump() for f in folders]}


@mcp.tool
@guard_tool("search_emails")
def search_emails(
    folder: str = "INBOX",
    unread_only: bool = False,
    from_address: str | None = None,
    subject_contains: str | None = None,
    body_contains: str | None = None,
    recent_days: int | None = None,
    since: str | None = None,
    before: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """搜索邮件，返回列表（不含正文，只有摘要字段）。

    拿到 uid 后再用 ``read_email`` 读取具体内容。

    Args:
        folder: 文件夹名，默认 INBOX。
        unread_only: 只看未读。
        from_address: 发件人包含该字符串。
        subject_contains: 主题包含该字符串。
        body_contains: 正文包含该字符串。
        recent_days: 最近 N 天内（与 since 二选一，优先级高于 since）。
        since: 起始日期，格式 YYYY-MM-DD。
        before: 截止日期，格式 YYYY-MM-DD。
        limit: 最多返回多少封（按时间倒序取最新的）。
    """
    emails = get_service().search(
        folder=folder,
        unread_only=unread_only,
        from_address=from_address,
        subject_contains=subject_contains,
        body_contains=body_contains,
        since=_parse_date_arg(since, "since"),
        before=_parse_date_arg(before, "before"),
        recent_days=recent_days,
        limit=max(1, min(limit, 100)),
    )
    return {
        "folder": folder,
        "count": len(emails),
        "emails": [e.model_dump(mode="json") for e in emails],
        "next_step": "用 read_email(folder, uid) 读取具体某封邮件的内容。",
    }


@mcp.tool
@guard_tool("read_email")
def read_email(folder: str, uid: str) -> dict[str, Any]:
    """读取单封邮件的完整内容（正文 + 附件元信息）。

    Args:
        folder: 邮件所在文件夹，来自 search_emails 的返回值。
        uid: 邮件 UID，来自 search_emails 的返回值。
    """
    detail = get_service().read(folder, uid)
    payload = detail.model_dump(mode="json")
    if detail.body_truncated:
        payload["truncation_note"] = (
            f"正文过长已截断到 {len(detail.body_text)} 字符，完整内容请让用户直接查看。"
        )
    return payload


@mcp.tool
@guard_tool("send_email")
def send_email(
    to: list[str],
    subject: str,
    body_text: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    body_html: str | None = None,
    attachments: list[str] | None = None,
    reply_to_uid: str | None = None,
) -> dict[str, Any]:
    """发送邮件。

    会经过安全闸门校验：收件人白名单、附件类型与大小、发送频率。
    被拒绝时返回值里的 ``hint`` 会说明原因，此时应改用 ``save_email_draft``。

    Args:
        to: 收件人邮箱列表（必填，精确地址，不要猜测）。
        subject: 邮件主题。
        body_text: 纯文本正文。
        cc: 抄送列表。
        bcc: 密送列表。
        body_html: HTML 正文（可选）。
        attachments: 附件在工作区内的相对路径，如 ``outputs/周报.docx``。
        reply_to_uid: 若为回复，填原邮件 UID（会用 INBOX 里的原邮件串联会话）。
    """
    request = SendRequest(
        to=to,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        cc=cc or [],
        bcc=bcc or [],
        attachments=attachments or [],
        reply_to_uid=reply_to_uid,
    )
    result = get_service().send(request)
    payload = result.model_dump(mode="json")
    if result.dry_run:
        payload["note"] = "当前为 dry_run 模式，邮件未真实发送。"
    return payload


@mcp.tool
@guard_tool("save_email_draft")
def save_email_draft(
    to: list[str],
    subject: str,
    body_text: str,
    cc: list[str] | None = None,
    attachments: list[str] | None = None,
    body_html: str | None = None,
) -> dict[str, Any]:
    """把邮件存成工作区内的 ``.eml`` 草稿文件，交人工确认后发送。

    这是发信被策略拒绝（或未配置邮箱）时的**推荐替代路径**：
    不发出去，但把工作成果保留下来。

    Args:
        to: 收件人列表。
        subject: 主题。
        body_text: 纯文本正文。
        cc: 抄送列表。
        attachments: 附件路径列表。
        body_html: HTML 正文（可选）。
    """
    request = SendRequest(
        to=to,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        cc=cc or [],
        attachments=attachments or [],
    )
    draft = get_service().save_draft(request)
    return {
        **draft.model_dump(mode="json"),
        "next_step": "草稿已保存在 drafts 目录，可用 read_document 查看，或交用户手动发送。",
    }


@mcp.tool
@guard_tool("mark_email")
def mark_email(
    folder: str,
    uid: str,
    action: str,
    target_folder: str | None = None,
) -> dict[str, Any]:
    """修改邮件状态。

    Args:
        folder: 邮件所在文件夹。
        uid: 邮件 UID。
        action: 取值 ``mark_read`` / ``mark_unread`` / ``move`` / ``delete``。
        target_folder: ``move`` 时的目标文件夹名，其余动作可省略。
    """
    service = get_service()
    if action == "mark_read":
        service.set_seen(folder, uid, True)
    elif action == "mark_unread":
        service.set_seen(folder, uid, False)
    elif action == "move":
        if not target_folder:
            return {
                "ok": False,
                "code": "missing_argument",
                "error": "move 动作必须提供 target_folder。",
                "hint": "请先用 list_mail_folders 查看可用文件夹，再指定 target_folder。",
            }
        service.move(folder, uid, target_folder)
    elif action == "delete":
        service.delete(folder, uid)
    else:
        return {
            "ok": False,
            "code": "invalid_action",
            "error": f"不支持的动作：{action}",
            "hint": "可选动作：mark_read / mark_unread / move / delete。",
        }
    return {"folder": folder, "uid": uid, "action": action, "target_folder": target_folder}


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _parse_date_arg(value: str | None, name: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise OfficeMCPError(
            f"{name} 参数格式不正确：{value}",
            hint="请使用 YYYY-MM-DD 格式，例如 2026-09-01。",
        ) from exc


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("OFFICE_MCP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    transport = os.environ.get("OFFICE_MCP_TRANSPORT", "stdio").lower()
    if transport in {"http", "streamable-http", "sse"}:
        mcp.run(
            transport="http",
            host=os.environ.get("OFFICE_MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("OFFICE_MCP_PORT", "8766")),
        )
    else:
        mcp.run()


if __name__ == "__main__":
    main()
