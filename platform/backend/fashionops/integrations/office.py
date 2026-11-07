"""接入 mcp-servers 里的三个办公 MCP 服务器。

## 它在 P2 基础上把方向反过来

P2 是「平台的工具 → 暴露成 MCP 服务器」（对外）。
这里是「外部办公 MCP 服务器 → 注册进平台网关」（对内）。
于是平台的 Agent 能像调 `erp.query_inventory` 一样调 `office.email.send_email`，
而且同样过权限 / 限流 / 熔断 / 审计。

## 默认接哪几个、要什么权限

office-docs / office-email / office-calendar 各自一台子进程。
权限是**显式**按工具列的（不用字符串猜读/写），见 `_OFFICE_SERVERS`。
原则：只读动作给 `office:read`，会改变外部世界的（发信 / 建事件 / 写文件）给
`office:write`。这样「谁能发邮件、谁能改日程」由平台 RBAC 统一决定，
而不是办公服务器自己拍板。

## 怎么开

默认**关闭**（`office_mcp_enabled=False`）。打开方式二选一：

1. 在 `.env` 写 `FASHIONOPS_OFFICE_MCP_SERVERS` = JSON 数组（最灵活，可指向任意
   stdio 服务器）；
2. 或设 `FASHIONOPS_OFFICE_MCP_CWD` 指向 mcp-servers 目录，由这里按约定拼出
   三个 office 模块的启动命令（用 `FASHIONOPS_OFFICE_MCP_PYTHON` 指定解释器，
   默认 `python`）。

两者都没有时，接入层是空操作 —— 不会起任何子进程，不影响启动。
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from fashionops.gateway.registry import Scope

logger = logging.getLogger("fashionops.integrations.office")


@dataclass(frozen=True)
class OfficeServerSpec:
    server_id: str
    module: str
    scopes: frozenset[str] = frozenset()
    read_only: bool = False
    tool_scopes: dict[str, frozenset[str]] = field(default_factory=dict)
    description: str = ""


# 三个办公服务器：server_id 决定注册后的工具名前缀（office.email.send_email）。
# tool_scopes 逐工具列权限，避免「按名字猜读/写」的脆弱启发式。
_OFFICE_SERVERS: tuple[OfficeServerSpec, ...] = (
    OfficeServerSpec(
        server_id="office.docs",
        module="docs",
        tool_scopes={
            "list_files": frozenset({Scope.OFFICE_READ}),
            "read_document": frozenset({Scope.OFFICE_READ}),
            "get_workspace_info": frozenset({Scope.OFFICE_READ}),
            "write_text_file": frozenset({Scope.OFFICE_WRITE}),
            "create_word_document": frozenset({Scope.OFFICE_WRITE}),
            "create_excel_workbook": frozenset({Scope.OFFICE_WRITE}),
            "create_presentation": frozenset({Scope.OFFICE_WRITE}),
        },
        description="办公文档服务器（Word/Excel/PPT/Markdown 读写）",
    ),
    OfficeServerSpec(
        server_id="office.email",
        module="email",
        tool_scopes={
            "get_mail_status": frozenset({Scope.OFFICE_READ}),
            "list_mail_folders": frozenset({Scope.OFFICE_READ}),
            "search_emails": frozenset({Scope.OFFICE_READ}),
            "read_email": frozenset({Scope.OFFICE_READ}),
            "send_email": frozenset({Scope.OFFICE_WRITE}),
            "save_email_draft": frozenset({Scope.OFFICE_WRITE}),
            "mark_email": frozenset({Scope.OFFICE_WRITE}),
        },
        description="办公邮件服务器（IMAP 收信 + SMTP 发信）",
    ),
    OfficeServerSpec(
        server_id="office.calendar",
        module="calendar",
        tool_scopes={
            "get_calendar_status": frozenset({Scope.OFFICE_READ}),
            "list_calendars": frozenset({Scope.OFFICE_READ}),
            "list_events": frozenset({Scope.OFFICE_READ}),
            "find_free_slots": frozenset({Scope.OFFICE_READ}),
            "create_event": frozenset({Scope.OFFICE_WRITE}),
            "update_event": frozenset({Scope.OFFICE_WRITE}),
            "delete_event": frozenset({Scope.OFFICE_WRITE}),
            "import_ics_file": frozenset({Scope.OFFICE_WRITE}),
            "export_ics_file": frozenset({Scope.OFFICE_WRITE}),
        },
        description="办公日历服务器（日程 CRUD + 空闲时间 + ICS）",
    ),
)


# 进程内常驻连接，避免被 GC 回收导致会话断开。
_CONNECTIONS: dict[str, Any] = {}


def _resolve_specs(settings: Any) -> list[dict[str, Any]]:
    """把配置翻译成「要拉起哪些子进程」的列表。"""
    raw = getattr(settings, "office_mcp_servers_json", None)
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("office_mcp_servers_json 解析失败：%s", exc)
            return []
        return [_normalize_custom(d) for d in data]

    cwd = getattr(settings, "office_mcp_cwd", None)
    if not cwd:
        return []
    python = getattr(settings, "office_mcp_python", "python")
    workspace = getattr(settings, "office_mcp_workspace", None)
    env = {"PYTHONPATH": cwd}
    if workspace:
        env["OFFICE_MCP_WORKSPACE_ROOT"] = workspace
    out = []
    for spec in _OFFICE_SERVERS:
        out.append(
            {
                "server_id": spec.server_id,
                "command": python,
                "args": ["-m", f"office_mcp.{spec.module}.server"],
                "cwd": cwd,
                "env": env,
                "scopes": spec.scopes,
                "read_only": spec.read_only,
                "tool_scopes": spec.tool_scopes,
                "description": spec.description,
            }
        )
    return out


def _normalize_custom(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "server_id": d["server_id"],
        "command": d["command"],
        "args": d.get("args", []),
        "cwd": d.get("cwd"),
        "env": d.get("env"),
        "scopes": frozenset(d.get("scopes", [])),
        "read_only": d.get("read_only", False),
        "tool_scopes": {k: frozenset(v) for k, v in (d.get("tool_scopes") or {}).items()},
        "description": d.get("description", ""),
    }


def register_office_tools(registry: Any) -> int:
    """把配置的办公 MCP 服务器接入 ``registry``。

    失败**不抛异常**：接入是「锦上添花」，不能因为某个办公服务器起不来
    就让整个平台启动失败。每个服务器独立 try，挂了记 warning 继续。
    """
    from fashionops.config import get_settings
    from fashionops.mcp_client.bridge import register_server_tools
    from fashionops.mcp_client.connection import McpStdioConnection

    settings = get_settings()
    if not getattr(settings, "office_mcp_enabled", False):
        return 0

    total = 0
    for spec in _resolve_specs(settings):
        server_id = spec["server_id"]
        if server_id in _CONNECTIONS:
            continue
        try:
            conn = McpStdioConnection(
                name=server_id,
                command=spec["command"],
                args=spec["args"],
                cwd=spec.get("cwd"),
                env=spec.get("env"),
            )
            conn.connect()
            registered = register_server_tools(
                registry,
                conn,
                server_id=server_id,
                scopes=spec.get("scopes") or frozenset(),
                read_only=spec.get("read_only", False),
                tool_scopes=spec.get("tool_scopes") or {},
                description=spec.get("description"),
            )
            _CONNECTIONS[server_id] = conn
            total += len(registered)
            logger.info("已接入 office 服务器 %s（%d 个工具）", server_id, len(registered))
        except Exception as exc:  # noqa: BLE001
            logger.warning("接入 office 服务器 %s 失败：%s", server_id, exc)
    return total


def close_office_connections() -> None:
    """进程退出时调用，关掉所有常驻连接。"""
    for conn in _CONNECTIONS.values():
        with contextlib.suppress(Exception):
            conn.close()
    _CONNECTIONS.clear()
