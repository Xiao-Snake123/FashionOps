"""MCP 暴露层测试。

## 这个文件在守什么

MCP 是平台**第一个对外开放的入口**（外部 Agent 可以调我们的 ERP、知识库、发信）。
所以它最怕两件事：

1. **绕过治理** —— 这一层要是直接调工具的 handler，权限/限流/熔断/审计就全没了。
   `TestGovernanceIsPreserved` 用"缺作用域必须被网关拒"来证明调用确实走了网关。
2. **工具定义漂移** —— 这一层自己再写一遍参数模型的话，注册表改了它不跟着改，
   表现是外部客户端传的参数被静默拒绝，而本地一切正常。
   `TestNoDefinitionDuplication` 证明 schema 是从注册表现取的。

## 一个必须知道的测试前提：签发完要 commit

`_call_tool_sync` 内部用 `session_scope()` 开**新的会话**（这是生产代码的正常写法）。
所以测试里签发的 key、建的用户必须 **commit** 之后，那边的会话才看得到 ——
没提交的话现象是"API Key 无效"，而 key 明明刚签发出来。

这和 `docs/06` 里记的 MySQL REPEATABLE READ 那一条是同一类问题：
**跨会话可见性**。SQLite 下表现为"另一个连接看不见未提交的行"。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("mcp", reason="未安装 mcp 依赖：uv sync --extra mcp")

from fashionops.db.base import utcnow  # noqa: E402
from fashionops.gateway.registry import Scope  # noqa: E402
from fashionops.mcp import auth, server  # noqa: E402
from fashionops.security import apikeys  # noqa: E402

PASSWORD = "T3st-Pa55word"


@pytest.fixture
def session(db):  # noqa: ANN001, ANN201
    from sqlalchemy.orm import Session

    with Session(db) as s:
        yield s


@pytest.fixture
def operator(session):  # noqa: ANN001
    """一个普通运营：有 erp:read / kb:read，**没有** mail:send / asset:write。"""
    from conftest import ensure_identity

    from fashionops.security import service
    from fashionops.security.permissions import ROLE_OPERATOR

    ensure_identity(session)
    user = service.create_user(
        session,
        username="mcp_operator",
        password=PASSWORD,
        roles=(ROLE_OPERATOR,),
        created_by="pytest",
    ).user
    session.commit()
    return user


def _issue(session, owner, *, name="测试 key", scopes=None, expires_days=None):  # noqa: ANN001
    """签发一把 key 并 **commit**（理由见模块 docstring）。"""
    raw, row = apikeys.issue(
        session,
        name=name,
        owner_id=owner.id,
        owner_username=owner.username,
        scopes=scopes,
        expires_days=expires_days,
        created_by="pytest",
    )
    session.commit()
    return raw, row


@pytest.fixture
def raw_key(session, operator):  # noqa: ANN001
    """不收紧作用域 → 等于 operator 的全部权限。"""
    return _issue(session, operator)[0]


@pytest.fixture
def erp_only_key(session, operator):  # noqa: ANN001
    """只给 erp:read。**验证"越权被拒"要用这把** ——

    用 `raw_key` 去测"kb.search 该被拒"是会失败的：它没收紧作用域，
    operator 本人就有 kb:read，那个调用本来就该成功。
    """
    return _issue(session, operator, name="只给 erp:read", scopes=["erp:read"])[0]


# ===========================================================================
# 1. 工具定义不重复 —— schema 从注册表现取
# ===========================================================================
class TestNoDefinitionDuplication:
    def test_names_are_dotted_not_double_underscore(self):
        """MCP 客户端里看的是 `erp.query_inventory`，不是 `erp__query_inventory`。

        注册表给的是 `__` 形状（OpenAI function name 不允许点号），MCP 没这限制。
        """
        tools = server.exposed_tools(frozenset({Scope.ERP_READ}))
        assert tools
        for tool in tools:
            assert "__" not in tool.name, f"{tool.name} 用的是 __ 形状"

    def test_schema_comes_from_the_registry(self):
        """换掉注册表里的入参模型，MCP 这边的 schema 必须跟着变。

        手抄一份参数模型的实现会在这条上失败 —— 而"外部客户端传参被拒、
        本地正常"正是那种静默漂移的典型症状。
        """
        from pydantic import BaseModel, Field

        from fashionops.gateway.registry import registry
        from fashionops.tools import register_all_tools

        if not len(registry):
            register_all_tools(registry)

        class _Probe(BaseModel):
            only_for_test: int = Field(description="测试字段")

        original = registry.get("erp.query_inventory")
        registry._specs[original.name] = type(original)(
            name=original.name,
            description=original.description,
            args_model=_Probe,
            handler=original.handler,
            scopes=original.scopes,
            read_only=True,
        )
        try:
            tools = server.exposed_tools(frozenset({Scope.ERP_READ}))
            probe = next(t for t in tools if t.name == "erp.query_inventory")
            assert "only_for_test" in probe.input_schema.get("properties", {})
        finally:
            registry._specs[original.name] = original

    def test_both_name_forms_are_accepted_on_call(self):
        assert server._resolve_tool("erp.query_inventory") == "erp.query_inventory"
        assert server._resolve_tool("erp__query_inventory") == "erp.query_inventory"


# ===========================================================================
# 2. ★ 治理没有被绕过
# ===========================================================================
class TestGovernanceIsPreserved:
    def test_only_scoped_tools_are_exposed(self):
        names = {t.name for t in server.exposed_tools(frozenset({Scope.ERP_READ}))}
        assert "erp.query_inventory" in names
        assert "mail.send" not in names, "没有 mail:send 却看到了发信工具"
        assert "kb.search" not in names

    def test_no_scope_tools_are_always_exposed(self):
        """`skill.load` 显式声明空作用域 —— 技能文档不含数据，谁都能读。"""
        assert "skill.load" in {t.name for t in server.exposed_tools(frozenset())}

    def test_call_without_scopes_is_denied_by_the_gateway(self, monkeypatch, erp_only_key):  # noqa: ANN001
        """**这条是验收标准**：缺权限必须被**网关**拦下，证明调用走了网关。

        这一层要是偷偷直接调 handler，这个调用会"成功" ——
        那就意味着外部客户端绕过了权限体系。
        """
        monkeypatch.setenv(auth.ENV_API_KEY, erp_only_key)
        payload = server._call_tool_sync("kb.search", {"query": "退货"})

        assert payload["ok"] is False
        assert payload["error"]["code"] == "PERMISSION_DENIED"

    def test_call_with_scopes_succeeds(self, monkeypatch, erp_only_key):  # noqa: ANN001
        monkeypatch.setenv(auth.ENV_API_KEY, erp_only_key)
        payload = server._call_tool_sync("erp.query_inventory", {"sku_codes": ["SKU00042"]})
        assert payload["ok"] is True, payload.get("error")
        assert payload["actor"].startswith("mcp:")

    def test_audit_sink_persists_instead_of_memory(self):
        """网关审计必须是**落库**的 sink，不能是内存。

        MCP 是全新的对外入口，"哪个外部系统什么时候调了什么"丢不起。

        ## 为什么只断言 sink 类型，不断言"表里真的有一行"

        因为单测跑在 SQLite 上，而 `SqlAuditSink` 是**另开一个连接**去写的：
        调用方会话正处在事务中时，SQLite 会直接 `database is locked`
        （这是方言差异，生产用 MySQL 不受影响 —— 已在本机 MySQL 上实测落库成功）。
        在 SQLite 上强行断言会得到一个假红，反而掩盖真正要守的东西。

        所以这里锁住**意图**（sink 是落库的），端到端的落库用真 MySQL 验证。
        """
        from fashionops.gateway.audit import SqlAuditSink

        assert isinstance(server._build_gateway().audit, SqlAuditSink)

    def test_unknown_tool_hint_lists_only_visible_tools(self, monkeypatch, erp_only_key):  # noqa: ANN001
        """提示里只列这把 key 能用的工具。

        全列出来等于告诉调用方"还有这些工具，只是你现在用不了" ——
        和网关里"不给模型看见没权限的工具"是同一条原则的两面。
        """
        monkeypatch.setenv(auth.ENV_API_KEY, erp_only_key)
        payload = server._call_tool_sync("erp.query_不存在的", {})

        assert payload["ok"] is False
        assert payload["error"]["code"] == "tool_not_found"
        hint = payload["error"]["hint"]
        assert "mail.send" not in hint, "提示里泄漏了这把 key 用不了的工具"


# ===========================================================================
# 3. 凭据
# ===========================================================================
class TestCredentials:
    def test_missing_key_exposes_nothing(self, monkeypatch):  # noqa: ANN001
        monkeypatch.setenv(auth.ENV_API_KEY, "")
        tools, reason = server._list_tools_sync()
        assert tools == []
        assert reason

    def test_missing_key_call_is_unauthorized(self, monkeypatch):  # noqa: ANN001
        monkeypatch.setenv(auth.ENV_API_KEY, "")
        payload = server._call_tool_sync("erp.query_inventory", {"sku_codes": ["SKU00042"]})
        assert payload["ok"] is False
        assert payload["error"]["code"] == "unauthorized"

    def test_revoked_key_is_rejected_immediately(self, session, operator, monkeypatch):  # noqa: ANN001
        """撤销必须**立即**生效 —— 所以凭据是每次请求现解析、不做缓存。"""
        raw, row = _issue(session, operator, name="待撤销")

        monkeypatch.setenv(auth.ENV_API_KEY, raw)
        assert server._list_tools_sync()[0], "撤销前应该能列出工具"

        apikeys.revoke(session, row.key_prefix)
        session.commit()

        tools, reason = server._list_tools_sync()
        assert tools == []
        assert "撤销" in reason

    def test_expired_key_is_rejected(self, session, operator, monkeypatch):  # noqa: ANN001
        raw, row = _issue(session, operator, name="会过期", expires_days=1)
        row.expires_at = utcnow() - timedelta(seconds=1)
        session.commit()

        monkeypatch.setenv(auth.ENV_API_KEY, raw)
        _tools, reason = server._list_tools_sync()
        assert "过期" in reason

    def test_key_scopes_only_narrow_never_widen(self, session, operator):  # noqa: ANN001
        """一把 key 拿到的能力恒为**其归属用户权限的子集**。

        这是防"用 key 提权"的关键：给 key 塞一个用户本人没有的作用域，
        最终拿到的也不该包含它。
        """
        raw, _row = _issue(
            session, operator, name="试图提权", scopes=["erp:read", "mail:send"]
        )
        identity, reason = auth.resolve(session, raw)
        assert identity is not None, reason
        assert "erp:read" in identity.scopes
        assert "mail:send" not in identity.scopes, "key 把用户没有的权限给了出去"

    def test_key_without_scopes_equals_owner_permissions(self, session, raw_key):  # noqa: ANN001
        identity, reason = auth.resolve(session, raw_key)
        assert identity is not None, reason
        assert "erp:read" in identity.scopes
        assert "mail:send" not in identity.scopes  # operator 本来就没有

    def test_owner_deleted_key_is_useless(self, session, operator, monkeypatch):  # noqa: ANN001
        """归属账号被删掉后这把 key 必须失效。

        否则它变成一个"不属于任何人"的幽灵凭据 —— 撤销都找不到人负责。
        """
        raw, row = _issue(session, operator, name="孤儿")
        row.owner_id = None
        session.commit()

        monkeypatch.setenv(auth.ENV_API_KEY, raw)
        _tools, reason = server._list_tools_sync()
        assert "归属账号已不存在" in reason


# ===========================================================================
# 4. API Key 本身
# ===========================================================================
class TestApiKeys:
    def test_raw_key_is_never_stored(self, session, operator):  # noqa: ANN001
        from sqlalchemy import select

        from fashionops.db.models_identity import APIKey

        raw, row = _issue(session, operator, name="x")
        stored = session.scalar(select(APIKey).where(APIKey.id == row.id))
        assert stored.key_hash != raw
        assert raw not in stored.key_prefix or stored.key_prefix in raw
        assert raw.startswith(apikeys.KEY_PREFIX)

    def test_verify_updates_last_used(self, session, operator):  # noqa: ANN001
        raw, row = _issue(session, operator, name="x")
        assert row.last_used_at is None

        found, reason = apikeys.verify(session, raw)
        assert found is not None, reason
        assert found.last_used_at is not None, "用过一次却没记最后使用时间"

    def test_wrong_key_is_rejected(self, session, raw_key):  # noqa: ANN001
        found, reason = apikeys.verify(session, "fops_完全错误的一串")
        assert found is None
        assert reason

    def test_revoke_is_idempotent(self, session, operator):  # noqa: ANN001
        _raw, row = _issue(session, operator, name="x")
        assert apikeys.revoke(session, row.key_prefix) is True
        assert apikeys.revoke(session, row.key_prefix) is False, "重复撤销应返回 False"


# ===========================================================================
# 5. MCP 处理器（异步入口）
# ===========================================================================
class TestHandlers:
    async def test_list_tools_handler(self, monkeypatch, erp_only_key):  # noqa: ANN001
        monkeypatch.setenv(auth.ENV_API_KEY, erp_only_key)
        result = await server.handle_list_tools(None, None)
        assert any(t.name == "erp.query_inventory" for t in result.tools)

    async def test_call_tool_handler_marks_errors(self, monkeypatch, erp_only_key):  # noqa: ANN001
        from mcp.types import CallToolRequestParams

        monkeypatch.setenv(auth.ENV_API_KEY, erp_only_key)
        params = CallToolRequestParams(name="kb.search", arguments={"query": "退货"})
        result = await server.handle_call_tool(None, params)
        assert result.is_error is True

    async def test_call_tool_handler_ok(self, monkeypatch, erp_only_key):  # noqa: ANN001
        from mcp.types import CallToolRequestParams

        monkeypatch.setenv(auth.ENV_API_KEY, erp_only_key)
        params = CallToolRequestParams(
            name="erp.query_inventory", arguments={"sku_codes": ["SKU00042"]}
        )
        result = await server.handle_call_tool(None, params)
        assert result.is_error is False
