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
from mcp.server.sse import SseServerTransport
from mcp.server.stdio import stdio_server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route

from fashionops import __version__
from fashionops.approval.policy import describe_preview, needs_approval
from fashionops.db import session_scope
from fashionops.gateway.audit import SqlAuditSink
from fashionops.gateway.core import CallContext, ToolGateway
from fashionops.gateway.registry import ToolNotFound, from_llm_name, registry
from fashionops.mcp.auth import resolve
from fashionops.tools import register_all_tools

logger = logging.getLogger("fashionops.mcp.server")

#: 服务器名。客户端配置里用它来标识。
SERVER_NAME = "fashionops"

#: 凭据没配好时也要说清楚 —— 空的工具列表如果不解释，
#: 客户端只会以为"这个服务器没提供工具"，然后一直用不了。
_BAD_CREDENTIAL_HINT = (
    "设置环境变量 FASHIONOPS_MCP_API_KEY（用 `fashionops-user apikey-create <用户名>` 签发一把），然后重启本服务。"
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
def _list_tools_sync(raw_key: str | None = None) -> tuple[list[Tool], str]:
    """返回 ``(工具列表, 失败原因)``。原因非空表示凭据有问题。

    ``raw_key`` 是 HTTP/SSE 传输时从请求头拿到的 Bearer token；为 None 时回退到
    环境变量（stdio 模式）。
    """
    _ensure_registry()
    try:
        with session_scope() as session:
            identity, reason = resolve(session, raw_key=raw_key)
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


def _reject_invalid_arguments(internal: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    """外部调用方的入参预检。通过返回 ``None``，不通过返回要回给客户端的错误载荷。

    错误体由 ``ToolGateway.invalid_args_error`` 统一构造 —— 和网关真正执行时
    报的是**同一份错误**，不会出现"预检说 A、真调用说 B"。
    """
    try:
        spec = registry.get(internal)
    except ToolNotFound:
        # 理论上到不了这里（``_resolve_tool`` 已经确认注册过了）。
        # 真到了也别在这儿编一个错误，交给网关去报它自己的 TOOL_NOT_FOUND。
        return None

    try:
        ToolGateway.validate_args(spec, arguments)
    except ValidationError as exc:
        error = ToolGateway.invalid_args_error(spec, exc)
        return {"ok": False, "tool": internal, "error": error.to_dict()}
    return None


def _call_tool_sync(name: str, arguments: dict[str, Any], raw_key: str | None = None) -> dict[str, Any]:
    """经网关调用一个工具。返回 `ToolResult.to_dict()` 的形状。

    ``raw_key`` 非 None 表示这是**外部调用方（如 DeerFlow）**发起的：这种情况下，
    需要人工确认的工具**不执行**，而是返回 ``needs_approval``，由编排层去弹审批卡。
    这把"谁能发邮件"的闸口保留在平台侧，而不是交给外部 Agent 自己决定。
    """
    _ensure_registry()

    with session_scope() as session:
        identity, reason = resolve(session, raw_key=raw_key)

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

        # ---- 外部调用方：参数先校验，再谈审批 ----
        #
        # 顺序不能反过来。外部调用方是模型驱动的（DeerFlow），它**会**传错参数；
        # 如果先弹审批卡，人就会被拉去看一个必然失败的操作：点了"同意"，
        # 工具立刻以 INVALID_ARGUMENTS 失败。审批的价值建立在
        # "人能看懂、且看了有用"之上 —— 拿这种卡片消耗人的注意力，
        # 闸口很快就会退化成无脑点"同意"（这个项目通篇在防这件事）。
        #
        # 只对**外部**调用方做这一步：平台内部 Agent 走 gateway.call()，
        # 那边本来就会校验，重复一次纯属浪费。
        if raw_key is not None:
            invalid = _reject_invalid_arguments(internal, arguments)
            if invalid is not None:
                return invalid

        # 外部调用方（DeerFlow 等）发起、且需要人工确认的工具：不执行，
        # 返回 needs_approval，让编排层去弹审批卡并把这一轮暂停。
        # 平台侧永远掌握"哪些动作需要人点确认"的权威，不会被外部 Agent 绕过。
        if raw_key is not None and needs_approval(internal):
            return {
                "ok": False,
                "needs_approval": True,
                "tool": internal,
                "arguments": arguments,
                "preview": describe_preview(internal, arguments),
                "error": {
                    "code": "NEEDS_APPROVAL",
                    "message": "该操作需要人工确认后才能执行",
                    "hint": "请在平台的审批卡片上确认或驳回，确认后编排层会继续这一轮。",
                    "retryable": False,
                },
            }

        # 说明一下**权限**为什么不在这段里判（这里只补了参数校验）：
        #
        # 调用方看不见自己没权限的工具（``tools/list`` 已按 scopes 过滤，
        # 见 ``exposed_tools``），所以正常路径根本走不到这行。只有当调用方
        # 硬报一个它没见过的工具名时，才会先拿到审批卡、人点了同意之后
        # 才被网关以 PERMISSION_DENIED 拒掉 —— 结果绕不过去（网关始终是
        # 最后一道），代价只是白费人一次点击。
        #
        # **刻意保持和平台内部一致**：``chat/agent.py`` 与 ``agents/loop.py``
        # 也是「先判审批、再进网关」这个顺序，而且它们同样靠"目录过滤"来
        # 承担权限。三处顺序统一，比在这里单独聪明一次更有价值 ——
        # 哪天要收口成"网关内统一前置"，三处一起改。
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


def build_server(raw_key: str | None = None) -> Server:
    """造一个 MCP 服务器实例。

    ``raw_key`` 是 HTTP/SSE 传输时从请求头里拿到的 Bearer token（stdio 模式留空，
    走环境变量）。它只收窄权限、不放大（见 `auth.resolve`）。
    """
    _ensure_registry()

    async def _on_list_tools(ctx: Any, params: Any = None) -> ListToolsResult:
        tools, reason = await anyio.to_thread.run_sync(lambda: _list_tools_sync(raw_key=raw_key))
        if reason:
            logger.warning("MCP 未暴露任何工具：%s", reason)
        return ListToolsResult(tools=tools)

    async def _on_call_tool(ctx: Any, params: Any) -> CallToolResult:
        name = str(getattr(params, "name", "") or "")
        arguments = dict(getattr(params, "arguments", None) or {})
        payload = await anyio.to_thread.run_sync(lambda: _call_tool_sync(name, arguments, raw_key=raw_key))
        ok = bool(payload.get("ok"))
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
            structured_content=payload,
            is_error=not ok,
        )

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
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
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


# ---------------------------------------------------------------------------
# SSE / HTTP 传输（走法 B：让 DeerFlow 这类外部 Agent 经 MCP 调平台工具）
# ---------------------------------------------------------------------------
def _extract_bearer(request: Request) -> str | None:
    """从 Authorization 头里抠出 Bearer token；没有或格式不对返回 None。"""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


class _AlreadySentResponse(Response):
    """一个**什么都不发**的响应。

    ## 为什么需要它

    ``SseServerTransport.connect_sse`` 内部已经用 ``EventSourceResponse``
    把整个 SSE 响应（``http.response.start`` + body）发出去了。而 Starlette 的
    ``Route`` 在端点函数返回后，**还会再执行一次**
    ``await response(scope, receive, send)``。

    于是同一个请求上出现了第二次 ``http.response.start``。uvicorn 的判定是
    "响应已经发出去了，你又发一次" —— 客户端看到的就是 **502 Bad Gateway**，
    服务端只留下一句 ``Unexpected ASGI message``，极难往 SSE 上联想。

    ## 为什么不用官方示例的 Mount 写法

    MCP 官方示例把处理函数直接 ``Mount`` 成 ASGI app，那样确实不会重复发送。
    但 ``Mount`` 会把 ``scope["root_path"]`` 改写成挂载前缀，而 ``connect_sse``
    是拿 ``root_path + self._endpoint`` 算"客户端该往哪儿 POST 消息"的：挂到
    ``/mcp/sse`` 之后，它算出来的地址会变成 ``/mcp/sse/messages/``，
    跟我们实际注册的 ``/mcp/messages/`` 对不上，客户端第二轮就 404。

    用 ``Route`` 能保住路径推导正确（``root_path`` 保持为空），代价就是必须
    自己把第二次发送吞掉 —— 就是这个类存在的唯一理由。
    """

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:  # noqa: ARG002
        return


def create_sse_app() -> Starlette:
    """返回一个挂在 ``/mcp`` 下的 Starlette 子应用，暴露 MCP over SSE。

    * ``GET  /mcp/sse``       —— SSE 通道（服务端 → 客户端）
    * ``POST /mcp/messages/`` —— 客户端 → 服务端消息（由 ``SseServerTransport`` 自带）

    DeerFlow 侧用 ``type: sse`` + ``url: http://<host>:8100/mcp/sse`` +
    ``headers: { Authorization: Bearer <平台签发的 apikey> }`` 接入。
    """
    # 这是平台内部的 sidecar 调用（DeerFlow → 平台工具），不需要 DNS 重绑防护；
    # 关掉它既能避免误拦，也规避了它「先回错误再抛异常」导致的重复响应（421/502）。
    sse = SseServerTransport(
        "/mcp/messages/",
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def handle_sse(request: Request) -> Response:
        # 每个连接单独建一个 Server 实例，凭据（Bearer token）只作用于这一个会话，
        # 不会串到别的连接。
        token = _extract_bearer(request)
        server = build_server(raw_key=token)
        async with sse.connect_sse(request.scope, request.receive, request._send) as (
            read_stream,
            write_stream,
        ):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
                raise_exceptions=False,
            )
        # 注意这里必须返回 _AlreadySentResponse 而不是空的 Response()：
        # connect_sse 已经发过响应了，再发一次就是 502（原因见 _AlreadySentResponse）。
        return _AlreadySentResponse()

    # 端点固定为 /mcp/sse 与 /mcp/messages/，无论是挂载进主应用还是独立 uvicorn 都一致。
    return Starlette(
        routes=[
            Route("/mcp/sse", handle_sse, methods=["GET"]),
            Mount("/mcp/messages/", app=sse.handle_post_message),
        ]
    )


__all__ = [
    "SERVER_NAME",
    "build_server",
    "create_sse_app",
    "exposed_tools",
    "handle_call_tool",
    "handle_list_tools",
    "serve",
]
