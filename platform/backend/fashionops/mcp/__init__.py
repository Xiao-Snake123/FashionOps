"""MCP（Model Context Protocol）服务端 —— 把工具网关暴露给外部 Agent。

## 它给这个项目补上了什么

在这之前，平台的 12 个工具（ERP 查询 / 知识库检索 / 报表导出 / 发信 / 选品打分 /
阈值告警 / 技能加载）**只能在平台内部用**。而 `mcp-servers/` 那三个办公服务器
又是完全独立的一套、和平台没有任何联系 —— 仓库里其实一直是两条平行线。

接上之后，Claude Desktop / Cursor / DeerFlow 这类 MCP 客户端可以直接调用
平台的能力，而且调用链和内鬼 Agent 完全一致。

## 三条不能破的约束

**1. 工具定义只有一份。** schema 从 `registry` 现取，不在这里重写函数签名。

**2. 调用必须经过 `ToolGateway`。** 权限、限流、熔断、幂等、重试、审计都在网关里。
这一层直接调 handler 的话，外部客户端就变成了一个绕过治理的后门 ——
而"绕过治理的入口只要存在一个，治理就等于没有"。

**3. 凭据只能收窄权限，不能放大。** 一把 key 拿到的能力恒为
**其归属用户权限的子集**（见 `mcp/auth.py`）。所以不存在"用一把 key 提权"这回事。

## 一个必须记住的坑：stdio 模式下 stdout 是协议通道

MCP 客户端把服务端当子进程拉起，JSON-RPC 帧走**标准输出**。
任何 `print()`、任何打到 stdout 的日志都会插进协议流里，
表现是客户端报 "invalid JSON" 之类的错，而原因看起来和日志毫无关系。

所以：
* 服务进程里**只能用 logger，不能用 print**；
* 日志必须走 stderr —— 项目的 `setup_logging()` 已经是这样（`_utf8_stream()`），
  所以入口处调一次 `setup_logging()` 就够了；
* `fashionops-mcp tools`（打印可见工具）这种**非服务**命令才可以往 stdout 写。

## 审计为什么必须是落库的 sink

网关默认的 `MemoryAuditSink` 重启即丢。MCP 是一个**全新的对外入口**，
"哪个外部系统、什么时候、调了什么"这种记录丢不起 —— 所以服务进程里
显式传 `SqlAuditSink()`，让 MCP 调用落到 `audit_logs` 表。
"""

from __future__ import annotations

from fashionops.mcp.auth import ENV_API_KEY, McpIdentity, resolve
from fashionops.mcp.server import (
    SERVER_NAME,
    build_server,
    create_sse_app,
    exposed_tools,
    serve,
)

__all__ = [
    "ENV_API_KEY",
    "McpIdentity",
    "SERVER_NAME",
    "build_server",
    "create_sse_app",
    "exposed_tools",
    "resolve",
    "serve",
]
