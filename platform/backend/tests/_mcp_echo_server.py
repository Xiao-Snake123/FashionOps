"""给测试用的极简 MCP 服务器（stdio）。

故意不依赖任何业务代码，也不依赖 mcp-servers 那个独立项目，保证单测零外部依赖。
用 ``MCPServer``（mcp 2.x 的高层服务器类）写，和真实的 office 服务器同一种风格。

它只暴露两个工具：

* ``echo``  —— 回显参数（正常返回 ``{"ok": true, "echo": {...}}``）。
* ``boom``  —— 抛异常，让 MCP 以 ``is_error=True`` 返回，用来测「协议层失败」路径。

用 ``python _mcp_echo_server.py`` 即可被 stdio 拉起（默认 stdio 传输）。
"""

from __future__ import annotations

import sys

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("echo-test")


@mcp.tool()
def echo(x: int) -> dict:
    """原样回显参数。"""
    return {"ok": True, "echo": {"x": x}}


@mcp.tool()
def boom() -> dict:
    """协议层失败：抛异常会让 MCP 以 is_error=True 返回。"""
    raise RuntimeError("boom on purpose")


if __name__ == "__main__":
    sys.exit(mcp.run())
