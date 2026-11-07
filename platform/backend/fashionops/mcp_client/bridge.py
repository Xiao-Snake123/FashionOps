"""把一条 MCP 连接里的工具，注册成平台网关的 ToolSpec。

## 为什么不复刻一份参数模型

办公服务器用 FastMCP 的 ``@mcp.tool`` 装饰器，**自动**从函数签名 + docstring
生成 JSON schema。如果在网关这层再手抄一遍 Pydantic 模型，两边必然漂移
（这正是 P2 暴露层刻意避开的坑）。

做法：注册一个**透传**的 `args_model`（只放行任意字段），但把服务器给的
`inputSchema` 原样挂到 `McpToolSpec` 上，让 `openai_schema()` 对外暴露真实的
参数描述。真正的参数校验交给办公服务器自己做。

## 治理怎么接上

每个 ToolSpec 都带 `scopes`：没有对应权限的 Agent 在 `tools/list` 阶段就看不到
这个工具，看到了也调不动（网关在 call 时拦）。office 工具默认映射到
`office:read` / `office:write`（见 `integrations/office.py`），因此「谁能读邮件、
谁能发邮件」由平台 RBAC 统一管，而不是每个 office 服务器各搞一套。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict

from fashionops.gateway.registry import ToolSpec

logger = logging.getLogger("fashionops.mcp_client.bridge")


class _AllowAnyArgs(BaseModel):
    """透传参数模型：不校验、只放行。真实校验在办公服务器端。"""

    model_config = ConfigDict(extra="allow")


@dataclass(frozen=True)
class McpToolSpec(ToolSpec):
    """从 MCP 服务器来的工具规格：schema 用服务器给的那份，不重写。"""

    tool_schema: dict = field(default_factory=dict)

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.llm_name,
                "description": self.description,
                "parameters": self.tool_schema or {"type": "object"},
            },
        }


def register_server_tools(
    registry: Any,
    connection: Any,
    *,
    server_id: str,
    scopes: frozenset[str] | None = None,
    description: str | None = None,
    read_only: bool = False,
    tool_scopes: dict[str, frozenset[str]] | None = None,
    call_timeout: float = 30.0,
) -> list[str]:
    """把一个 MCP 服务器里的所有工具注册进 ``registry``。

    返回注册成功的工具名列表。任何单个工具注册失败都不会中断其余工具。
    """
    from fashionops.mcp_client.connection import McpStdioConnection

    if not isinstance(connection, McpStdioConnection):
        raise TypeError("connection 必须是 McpStdioConnection")

    names: list[str] = []
    tools = connection.list_tools()
    for tool in tools:
        tool_name = f"{server_id}.{tool.name}"
        eff_scopes = (tool_scopes or {}).get(tool.name) or scopes or frozenset()
        handler = _make_handler(connection, tool.name)
        try:
            registry.register(
                McpToolSpec(
                    name=tool_name,
                    description=(description or "") + (tool.description or ""),
                    args_model=_AllowAnyArgs,
                    handler=handler,
                    scopes=frozenset(eff_scopes),
                    read_only=read_only,
                    timeout_s=call_timeout,
                    source=f"mcp:{server_id}",
                    tool_schema=dict(_tool_input_schema(tool) or {"type": "object"}),
                    tags=(server_id,),
                )
            )
            names.append(tool_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("注册 office 工具 %s 失败：%s", tool_name, exc)
    return names


def _tool_input_schema(tool: Any) -> dict | None:
    """兼容 mcp 1.x（``inputSchema``）与 2.x（``input_schema``）两种字段名。"""
    schema = getattr(tool, "input_schema", None)
    if schema is None:
        schema = getattr(tool, "inputSchema", None)
    return schema


def _make_handler(connection: Any, tool_name: str) -> Callable[..., Any]:
    def handler(**kwargs: Any) -> Any:
        return connection.call_tool(tool_name, kwargs)

    return handler
