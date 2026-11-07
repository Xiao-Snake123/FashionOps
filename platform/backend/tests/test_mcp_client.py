"""P3：外部 MCP 服务器接入平台网关（客户端 + 桥接）。

用 ``_mcp_echo_server.py`` 起一个**进程内子进程**的 MCP 服务器，全程不依赖
mcp-servers 那个独立项目，保证单测零外部依赖。

守三件事：
1. 工具被注册进来，且对外暴露的 schema 是服务器给的那份（没丢、没被重写）；
2. 经网关调用能拿到数据，且**没有权限会被网关拦**（证明走了治理）；
3. 协议层失败（is_error）能被网关正确归类成上游错误。

另外用 ``integrations.office.register_office_tools`` 验证整条装配链路。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="未安装 mcp 依赖：uv sync --extra mcp")

from fashionops.gateway.core import CallContext, ToolGateway
from fashionops.gateway.registry import Scope, ToolRegistry

ECHO_SERVER = str(Path(__file__).with_name("_mcp_echo_server.py"))
PYTHON = sys.executable


@pytest.fixture
def registry():
    from fashionops.mcp_client.bridge import register_server_tools
    from fashionops.mcp_client.connection import McpStdioConnection

    reg = ToolRegistry()
    conn = McpStdioConnection(name="echo", command=PYTHON, args=[ECHO_SERVER])
    conn.connect()
    register_server_tools(
        reg,
        conn,
        server_id="test",
        scopes=frozenset({Scope.OFFICE_READ}),
        tool_scopes={"boom": frozenset({Scope.OFFICE_WRITE})},
    )
    yield reg
    conn.close()


# ---------------------------------------------------------------------------
# 1. 注册与 schema
# ---------------------------------------------------------------------------
def test_tools_are_registered_with_real_schema(registry: ToolRegistry) -> None:
    names = {s.name for s in registry.specs()}
    assert "test.echo" in names
    assert "test.boom" in names

    echo = registry.get("test.echo")
    # schema 必须来自服务器，不能丢
    params = echo.openai_schema()["function"]["parameters"]
    assert params["properties"]["x"]["type"] == "integer"


def test_allow_any_args_passes_through(registry: ToolRegistry) -> None:
    # 网关的 args_model 是透传的，不会因为多了字段就拒掉这次调用
    # （真实校验由下游办公服务器做；这里只验证网关不拦截）。
    gw = ToolGateway(registry)
    res = gw.call(
        "test.echo",
        {"x": 3, "extra_field": "keep-me"},
        ctx=CallContext(actor="u", scopes=frozenset({Scope.OFFICE_READ})),
    )
    assert res.ok, res.error
    assert res.data["echo"]["x"] == 3


# ---------------------------------------------------------------------------
# 2. 治理：权限 + 审计
# ---------------------------------------------------------------------------
def test_call_through_gateway_returns_data(registry: ToolRegistry) -> None:
    gw = ToolGateway(registry)
    res = gw.call("test.echo", {"x": 7}, ctx=CallContext(actor="u", scopes=frozenset({Scope.OFFICE_READ})))
    assert res.ok, res.error
    assert res.data["echo"] == {"x": 7}


def test_missing_scope_is_denied_by_gateway(registry: ToolRegistry) -> None:
    gw = ToolGateway(registry)
    res = gw.call("test.echo", {"x": 1}, ctx=CallContext(actor="u", scopes=frozenset()))
    assert not res.ok
    assert res.error.code.value == "PERMISSION_DENIED"


def test_protocol_error_is_surfaced(registry: ToolRegistry) -> None:
    gw = ToolGateway(registry)
    res = gw.call("test.boom", {}, ctx=CallContext(actor="u", scopes=frozenset({Scope.OFFICE_WRITE})))
    assert not res.ok
    # 协议层失败 → 网关按上游错误归类（可重试）
    assert res.error.code.value == "UPSTREAM_ERROR"


# ---------------------------------------------------------------------------
# 3. 整条装配链路（integrations.office）
# ---------------------------------------------------------------------------
def test_office_registration_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """开开关 + 用自定义 servers_json 指向 echo 服务器（不真连 office）。"""
    import fashionops.config as config_mod
    from fashionops.integrations import office

    fake = config_mod.Settings()
    monkeypatch.setattr(fake, "office_mcp_enabled", True, raising=False)
    servers = [{"server_id": "test", "command": PYTHON, "args": [ECHO_SERVER], "scopes": ["office:read"]}]
    monkeypatch.setattr(fake, "office_mcp_servers_json", json.dumps(servers), raising=False)
    monkeypatch.setattr(config_mod, "get_settings", lambda: fake)

    reg = ToolRegistry()
    try:
        n = office.register_office_tools(reg)
        assert n >= 1
        assert "test.echo" in {s.name for s in reg.specs()}
    finally:
        office.close_office_connections()
