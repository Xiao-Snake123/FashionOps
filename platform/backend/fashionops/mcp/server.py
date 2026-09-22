"""把工具网关暴露成 MCP 服务器。

## 最重要的一件事：这一层**不重新实现任何工具**

`tools/list` 的 schema 直接来自 `registry.openai_schemas(granted_scopes)`，
`tools/call` 直接走 `ToolGateway.call()`。所以外部 MCP 客户端（Claude Desktop /
Cursor / DeerFlow）拿到的工具，和平台内部 Agent 用的工具是**同一份定义、同一条
调用链** —— 权限、限流、熔断、幂等、重试、审计一项不少。

如果这里给 12 个工具各写一个 Python 函数（用 `@server.tool()` 那种写法），
参数模型就要抄第二遍。抄完之后两边必然漂移：
改了 `ToolSpec.args_model` 却忘了改 MCP 那边的函数签名，
表现是外部客户端传的参数被静默拒绝，而本地 Agent 一切正常。

**绕过治理的入口只要存在一个，治理就等于没有** —— 这条原则在这个项目里
通篇都在，MCP 这一层也不例外。

## 凭据：每次请求现解析

`resolve()` 不开缓存。理由和 token 里不放权限一样：
管理员撤销一把 key 之后，必须**立即**生效，而不是等到进程重启。
（代价是每个请求多一次查库 —— MCP 调用本来就比 HTTP 重，这笔开销可以忽略。）
"""
from __future__ import annotations

import json
import logging
from typing import Any

import anyio
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from fashionops import __version__
from fashionops.db import session_scope
from fashionops.gateway.audit import SqlAuditSink
from fashionops.gateway.core import CallContext, ToolGateway
from fashionops.gateway.registry import from_llm_name, registry
from fashionops.mcp.auth import resolve
from fashionops.tools import register_all_tools

logger = logging.getLogger("fashionops.mcp.server")

#: 服务器名。客户端配置里用它来标识。
SERVER_NAME = "fashionops"

#: 凭据没配好时也要说清楚 —— 空的工具列表如果不解释，
#: 客户端只会以为"这个服务器没提供工具"，然后一直用不了。
_BAD_CREDENTIAL_HINT = (
    "设置环境变量 FASHIONOPS_MCP_API_KEY（用 "
    "`fashionops-user apikey-create <用户名>` 签发一把），然后重启本服务。"
)


def _ensure_registry() -> None:
    if not len(registry):
        register_all_tools(registry)


# ---------------------------------------------------------------------------
# 工具清单
# ---------------------------------------------------------------------------
def exposed_tools(granted_scopes: frozenset[str]) -> list[Tool]:
    """把注册表里**这个凭据能用的**工具转成 MCP 的 Tool。

    用 `granted_scopes` 过滤是刻意的：不给模型看见它被拒的工具。
    （和 `build_catalog` 同一条理由：看见了就会去调，调了被拒浪费一整轮。）
    """
    _ensure_registry()
    tools: list[Tool] = []
    for schema in registry.openai_schemas(granted_scopes):
        function = schema.get("function", {})
        llm_name = str(function.get("name", ""))
        tools.append(
            Tool(
                # 对外用**点号名**（`erp.query_inventory`）。
                #
                # 注册表 `openai_schemas()` 给的是 `__` 形状
                # （`erp__query_inventory`）—— 那是为 OpenAI function name 准备的，
                # 它不允许点号。而 **MCP 没有这个限制**，点号在客户端里明显更好读。
                # 所以这里换回去；`_resolve_tool` 两种形状都收。
                name=from_llm_name(llm_name),
                description=str(function.get("description", "")),
                input_schema=dict(function.get("parameters") or {"type": "object"}),
            )
        )
    return tools


def _resolve_tool(name: str) -> str:
    """把客户端传来的工具名映射回内部名。

    同时接受 `erp.query_inventory` 和 `erp__query_inventory`：
    后者是 OpenAI function name 的形状，别的 MCP 实现可能照样传它过来。
    """
    if name in registry:
        return name
    internal = from_llm_name(name)
    if internal in registry:
        return internal
    raise KeyError(name)


# ---------------------------------------------------------------------------
# 请求处理（同步部分，跑在线程里）
# ---------------------------------------------------------------------------
def _list_tools_sync() -> tuple[list[Tool], str]:
    """返回 ``(工具列表, 失败原因)``。原因非空表示凭据有问题。"""
    _ensure_registry()
    try:
        with session_scope() as session:
            identity, reason = resolve(session)
            if identity is None:
                return [], reason
            return exposed_tools(identity.scopes), ""
    except Exception as exc:  # noqa: BLE001
        # 数据库挂了也要让客户端看到原因，而不是一个"服务器已断开"
        logger.warning("MCP 解析身份失败：%s: %s", type(exc).__name__, exc)
        return [], f"暂时无法校验凭据（{type(exc).__name__}）"


def _build_gateway() -> ToolGateway:
    """给 MCP 用的网关。

    ## 为什么这里显式传 `SqlAuditSink`

    网关默认是 `MemoryAuditSink`（重启即丢）。对内部 Agent 那只是"审计查不到"，
    对 **MCP 这个全新的对外入口**则是"外部系统干了什么，我们一重启就什么都不知道"。
    所以这条不能省。

    抽成一个函数而不是内联，是为了让测试能直接断言"审计 sink 是落库的" ——
    有人为了省事换回内存 sink，那条测试会失败。
    """
    return ToolGateway(registry, audit_sink=SqlAuditSink())


def _call_tool_sync(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """经网关调用一个工具。返回 `ToolResult.to_dict()` 的形状。"""
    _ensure_registry()

    with session_scope() as session:
        identity, reason = resolve(session)

        # 先鉴权再找工具：凭据不对时**连工具名都不该确认** ——
        # "这个工具有没有"本身也是信息。
        if identity is None:
            return {
                "ok": False,
                "tool": name,
                "error": {
                    "code": "unauthorized",
                    "message": reason,
                    "hint": _BAD_CREDENTIAL_HINT,
                    "retryable": False,
                },
            }

        try:
            internal = _resolve_tool(name)
        except KeyError:
            # 只列**这把 key 看得见**的工具，和 `tools/list` 保持一致。
            # 把注册表全列出来等于告诉调用方"还有这些工具，只是你现在用不了"，
            # 那和网关里"不给模型看见没权限的工具"是同一条原则的两面。
            visible = "、".join(tool.name for tool in exposed_tools(identity.scopes))
            return {
                "ok": False,
                "tool": name,
                "error": {
                    "code": "tool_not_found",
                    "message": f"没有名为 {name!r} 的工具",
                    "hint": f"你这把 key 可用的工具：{visible}",
                    "retryable": False,
                },
            }

        gateway = _build_gateway()
        result = gateway.call(
            internal,
            arguments or {},
            ctx=CallContext(actor=identity.actor, scopes=identity.scopes),
        )
        payload = result.to_dict()
        # 把审计里的调用方补进返回值：MCP 客户端的日志里能一眼看出是谁调的
        payload.setdefault("actor", identity.actor)
        return payload


# ---------------------------------------------------------------------------
# MCP 处理器
# ---------------------------------------------------------------------------
async def handle_list_tools(ctx: Any, params: Any = None) -> ListToolsResult:
    tools, reason = await anyio.to_thread.run_sync(_list_tools_sync)
    if reason:
        logger.warning("MCP 未暴露任何工具：%s", reason)
    return ListToolsResult(tools=tools)


async def handle_call_tool(ctx: Any, params: Any) -> CallToolResult:
    name = str(getattr(params, "name", "") or "")
    arguments = dict(getattr(params, "arguments", None) or {})

    payload = await anyio.to_thread.run_sync(lambda: _call_tool_sync(name, arguments))
    ok = bool(payload.get("ok"))

    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structured_content=payload,
        is_error=not ok,
    )


def build_server() -> Server:
    """造一个 MCP 服务器实例。"""
    _ensure_registry()
    return Server(
        name=SERVER_NAME,
        version=__version__,
        title="服装电商运营中台",
        description="ERP 查询、知识库检索、报表导出与发信能力，全部经工具网关治理",
        instructions=(
            "这是服装电商运营中台的工具服务。所有对外会产生影响的动作"
            "（发信、上架、改预算）都需要人工确认，被拦下时会返回明确原因。"
            "数字一律以工具返回为准，不要凭印象编造。"
        ),
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
    )


async def serve() -> None:
    """以 stdio 方式跑起来（MCP 客户端拉起服务端的标准方式）。"""
    server = build_server()
    # raise_exceptions=False：单个请求出错不该让整个服务器退出，
    # 否则模型一次传错参数就把 MCP 连接弄断，后面全部调用都失败。
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
            raise_exceptions=False,
        )


__all__ = [
    "SERVER_NAME",
    "build_server",
    "exposed_tools",
    "handle_call_tool",
    "handle_list_tools",
    "serve",
]
