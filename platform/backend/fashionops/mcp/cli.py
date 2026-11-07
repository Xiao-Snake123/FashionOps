"""MCP 命令行。

用法::

    fashionops-mcp serve                 # 以 stdio 方式跑起来（给 MCP 客户端拉起用）
    fashionops-mcp tools                 # 打印这把 key 能看到哪些工具（不启动服务）

## `tools` 这条命令为什么单独存在

配 MCP 客户端时最容易卡在"配好了但一个工具都没有"。原因通常是：
key 没配、key 过期、或者那把 key 的归属用户根本没有对应权限。

`tools` 让你**不用打开 Claude Desktop** 就能看到这把 key 究竟能暴露什么，
把"客户端那边看不见工具"这个问题在命令行里就定位掉。
"""

from __future__ import annotations

import sys
from argparse import ArgumentParser, Namespace

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def cmd_serve(_args: Namespace) -> int:
    import anyio

    from fashionops.logging_setup import setup_logging
    from fashionops.mcp.server import serve

    # 必须显式配置日志：**stdio 模式下 stdout 是 JSON-RPC 通道**，
    # 日志只能去 stderr。项目的 setup_logging 已经是 stderr，
    # 但这里要靠它把可能被别的库装上的 stdout handler 清掉。
    setup_logging()
    anyio.run(serve)
    return 0


def cmd_tools(_args: Namespace) -> int:
    from fashionops.db import session_scope
    from fashionops.mcp.auth import ENV_API_KEY, resolve
    from fashionops.mcp.server import exposed_tools

    with session_scope() as session:
        identity, reason = resolve(session)
        if identity is None:
            print(f"✗ 凭据不可用：{reason}")
            print()
            print(f"  设置环境变量 {ENV_API_KEY}，用 `fashionops-user apikey-create <用户名>` 签发一把。")
            return 1

        print(f"身份   : {identity.actor}（API Key：{identity.key_name}）")
        scope_text = "、".join(sorted(identity.scopes)) or "（无任何作用域 → 看不到任何工具）"
        print(f"作用域 : {scope_text}")
        print()

        tools = exposed_tools(identity.scopes)
        print(f"可暴露工具 {len(tools)} 个：")
        for tool in tools:
            print(f"  - {tool.name}")
        if not tools:
            print()
            print("  一个都没有，说明这个用户的角色没授予任何网关作用域。")
            print("  用 `fashionops-user show <username>` 看他实际有哪些权限。")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(
        prog="fashionops-mcp",
        description="把工具网关暴露成 MCP 服务器",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="以 stdio 方式启动 MCP 服务器")
    sub.add_parser("tools", help="打印当前 API Key 能看到哪些工具（不启动服务）")

    args = parser.parse_args(argv)
    return cmd_serve(args) if args.command == "serve" else cmd_tools(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["main"]
