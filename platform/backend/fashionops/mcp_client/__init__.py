"""MCP 客户端工具集 —— 平台作为客户端去调外部 MCP 服务器。

P2 把平台的工具**暴露**成 MCP 服务器（对外）。这一层是反方向：
平台作为 **客户端**，把 `mcp-servers/` 里的办公服务器（office-docs / email /
calendar）**接入**进来，再注册进工具网关 —— 于是外部能力自动获得平台的
权限 / 限流 / 熔断 / 幂等 / 审计。

核心两件套：

* `connection.py` —— `McpStdioConnection`：把服务器当 stdio 子进程拉起，
  在一条常驻后台线程里跑 event loop 持有 MCP 会话，对外提供**同步**的
  `list_tools` / `call_tool`（内部用 `run_coroutine_threadsafe` 把协程
  丢给那个 loop，再阻塞等地结果）。
* `bridge.py` —— `register_server_tools`：把一个服务器里的工具，逐个注册成
  平台的 `ToolSpec`，handler 代理转发到该连接。schema 直接用服务器给的那份，
  **不重写参数模型**（重写必然漂移）。
"""

from __future__ import annotations

from fashionops.mcp_client.bridge import McpToolSpec, register_server_tools
from fashionops.mcp_client.connection import (
    McpConnectionError,
    McpStdioConnection,
    McpToolError,
)

__all__ = [
    "McpConnectionError",
    "McpStdioConnection",
    "McpToolError",
    "McpToolSpec",
    "register_server_tools",
]
