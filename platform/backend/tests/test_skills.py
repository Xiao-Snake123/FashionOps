"""技能（SKILL.md）测试。

## 这个文件在守什么

技能体系对外的承诺有三条，每条都容易在后续改动里悄悄失效：

1. **文档里的数字来自代码。** 一旦有人图省事在 md 里直接写 `30`，
   承诺就没了 —— 而失效的表现是"文档和代码各说各的"，没有任何报错。
   `TestFactsComeFromCode` 直接改代码常量来锁这条。
2. **索引里只有名字和说明，没有正文。** 渐进式披露的全部意义就在这：
   索引是几十个 token，正文是几百个。正文漏进索引 = 每轮都付这个开销。
3. **技能里声明的工具真实存在。** 写错一个工具名，模型会照着调、
   拿到 `TOOL_NOT_FOUND` —— 报错点在运行时，离原因（md 里打错一个字）很远。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from fashionops.skills import loader
from fashionops.skills.facts import facts
from fashionops.skills.loader import (
    SkillError,
    _parse_frontmatter,
    all_skills,
    get_skill,
    index_block,
    reload,
    render,
    skill_for_kind,
    validate_skill_tools,
)


@pytest.fixture(autouse=True)
def _clean_caches():
    """每个用例前后都清掉缓存。

    技能和事实都是 `lru_cache` 的（生产路径就该缓存）。测试里要能在
    "改完代码常量"之后重新渲染，也必须防止一个用例的 monkeypatch 漏给下一个。
    """
    facts.cache_clear()
    reload()
    yield
    # teardown **只清缓存、不重新加载**。
    #
    # `monkeypatch` 夹具比这个自动夹具**先建立**，所以它还原补丁发生在本夹具
    # teardown 之后 —— 此刻 `CONTENT_DIR` 可能还指向临时目录里那份"故意写坏"
    # 的技能。重新加载会在这里抛错，而那个错误会把用例真正的失败原因盖掉
    # （实测表现就是一堆莫名其妙的 ERROR，而用例本身是过的）。
    facts.cache_clear()
    loader.all_skills.cache_clear()


def _write_skill(tmp_path: Path, name: str, text: str) -> Path:
    (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path / name


# ===========================================================================
# 1. 加载与解析
# ===========================================================================
class TestLoading:
    def test_ships_four_skills(self):
        names = {s.name for s in all_skills()}
        assert names == {"sku-selection", "business-review", "task-notification", "customer-reply"}

    def test_required_fields_present(self):
        for skill in all_skills():
            assert skill.name and skill.title and skill.summary
            assert skill.body.strip()
            assert skill.source.suffix == ".md"

    def test_names_are_unique(self):
        names = [s.name for s in all_skills()]
        assert len(names) == len(set(names))

    def test_all_placeholders_rendered(self):
        """渲染完就不该再有占位符。有残留说明有人手滑写了 ``{{ xx }}``。"""
        for skill in all_skills():
            assert "{{" not in skill.body, f"{skill.name} 的正文里还有没渲染的占位符"

    def test_task_chain_skills_map_to_todo_kinds(self):
        """任务链三个 kind 都要有对应技能，否则那一步会退回内置短提示。"""
        for kind in ("selection", "analytics", "notification"):
            skill = skill_for_kind(kind)
            assert skill is not None, f"没有 kind={kind} 对应的技能"

    def test_generic_is_deliberately_not_a_skill(self):
        """`generic` 是"关键词一个都没命中"的兜底，没有业务 SOP 可讲。

        它退回 `loop._GENERIC_GUIDE` 而不是一份 md —— 这条断言是在固定那个决定，
        免得以后有人"为了整齐"给它也补一个技能文件。
        """
        assert skill_for_kind("generic") is None
        assert skill_for_kind("") is None
        assert skill_for_kind("不存在的步骤") is None

    def test_get_skill_tolerates_whitespace(self):
        assert get_skill(" sku-selection ") is not None
        assert get_skill("不存在") is None


# ===========================================================================
# 2. frontmatter 解析 —— 坏输入必须报错，不能静默跳过
# ===========================================================================
class TestFrontmatter:
    def test_missing_opening_marker(self, tmp_path):
        path = _write_skill(tmp_path, "a.md", "name: x\n---\nbody")
        with pytest.raises(SkillError, match="必须以 --- 开头"):
            _parse_frontmatter(path.read_text(encoding="utf-8"), path)

    def test_missing_closing_marker(self, tmp_path):
        path = _write_skill(tmp_path, "a.md", "---\nname: x\nbody 没有收尾")
        with pytest.raises(SkillError, match="没有收尾"):
            _parse_frontmatter(path.read_text(encoding="utf-8"), path)

    def test_list_item_without_key(self, tmp_path):
        path = _write_skill(tmp_path, "a.md", "---\n- 裸列表项\n---\nbody")
        with pytest.raises(SkillError, match="列表项前面没有键名"):
            _parse_frontmatter(path.read_text(encoding="utf-8"), path)

    def test_non_key_value_line(self, tmp_path):
        path = _write_skill(tmp_path, "a.md", "---\nname: x\n这行没有冒号\n---\nbody")
        with pytest.raises(SkillError, match="不是 key: value"):
            _parse_frontmatter(path.read_text(encoding="utf-8"), path)

    def test_parses_scalar_and_list(self, tmp_path):
        text = "---\nname: demo\ntitle: 演示\ntools:\n  - a.b\n  - c.d\n---\n正文"
        meta, body = _parse_frontmatter(text, tmp_path / "x.md")
        assert meta["name"] == "demo"
        assert meta["tools"] == ["a.b", "c.d"]
        assert body == "正文"

    def test_missing_required_key_is_fatal(self, tmp_path, monkeypatch):
        """缺字段要抛错。**静默跳过一条技能是最坏的失败方式** ——
        Agent 会退回"没有规范"的状态继续干活，而那个状态看起来完全正常。"""
        monkeypatch.setattr(loader, "CONTENT_DIR", tmp_path)
        _write_skill(tmp_path, "a.md", "---\nname: x\ntitle: 只有两个字段\n---\n正文")
        with pytest.raises(SkillError, match="缺 summary"):
            loader.reload()

    def test_duplicate_name_is_fatal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(loader, "CONTENT_DIR", tmp_path)
        body = "---\nname: same\ntitle: 重名\nsummary: 说明\n---\n正文"
        _write_skill(tmp_path, "a.md", body)
        _write_skill(tmp_path, "b.md", body)
        with pytest.raises(SkillError, match="技能名重复"):
            loader.reload()

    def test_empty_body_is_fatal(self, tmp_path):
        path = _write_skill(tmp_path, "a.md", "---\nname: x\ntitle: t\nsummary: s\n---\n")
        with pytest.raises(SkillError, match="正文是空的"):
            _parse_frontmatter(path.read_text(encoding="utf-8"), path)


# ===========================================================================
# 3. ★ 核心承诺：文档里的数字来自代码
# ===========================================================================
class TestFactsComeFromCode:
    def test_unknown_placeholder_names_the_available_ones(self, tmp_path):
        """拼错占位符要报错并列出可用项。

        静默留空的话，技能里少一个数字，模型会自己补一个 —— 而补出来的没人能核对。
        """
        path = _write_skill(tmp_path, "a.md", "退货率超过 {{ return_rate_critcal }} 算红线")
        with pytest.raises(SkillError) as exc:
            render(path.read_text(encoding="utf-8"), path)
        message = str(exc.value)
        assert "return_rate_critcal" in message
        # 报错要能直接指导修复
        assert "return_rate_critical" in message

    def test_threshold_change_propagates_to_documents(self, monkeypatch):
        """**改代码常量，技能文档跟着变。**

        这条是整个技能体系最容易被破坏的承诺：只要有人图省事在 md 里
        直接写 `30%`，它和 `RETURN_RATE_CRITICAL` 就开始各自漂移，
        而漂移**没有任何症状** —— 模型会照着文档里那个假数字讲给运营听。

        所以这里不测"文档里是不是写了 30%"，而是**把常量改成 50%，
        要求文档跟着变**。写死数字的实现会在这条上失败。
        """
        before = get_skill("business-review")
        assert before is not None
        assert "退货率 ≥ 30% → critical" in before.body

        monkeypatch.setattr(
            "fashionops.agents.analytics_rules.RETURN_RATE_CRITICAL", 0.50
        )
        facts.cache_clear()
        after = reload()
        target = next(s for s in after if s.name == "business-review")
        assert "退货率 ≥ 50% → critical" in target.body

    def test_sample_floor_propagates(self, monkeypatch):
        """样本下限同理 —— 它是"模型看不见的那个数字"里最典型的一个。

        改之前模型只知道"level 以工具返回为准"，解释不了"为什么这个类目不管"。
        """
        monkeypatch.setattr(
            "fashionops.agents.analytics_rules.MIN_ORDERS_FOR_JUDGEMENT", 7
        )
        facts.cache_clear()
        target = next(s for s in reload() if s.name == "business-review")
        assert "分组订单数 < 7 单时" in target.body

    def test_selection_constraints_come_from_dataclass_defaults(self, monkeypatch):
        """选品的约束值取自 `HardConstraints()` 的默认实例，不是另抄一份。"""
        from fashionops.agents import scoring

        # 先把原类抓出来：在 lambda 里再写 `scoring.HardConstraints(...)` 会拿到
        # **被替换后的那个 lambda 自己**，直接递归爆掉
        original = scoring.HardConstraints

        def fake_constraints():  # noqa: ANN202
            return original(min_stock=999)

        monkeypatch.setattr(scoring, "HardConstraints", fake_constraints)
        facts.cache_clear()
        target = next(s for s in reload() if s.name == "sku-selection")
        assert "可用库存 ≥ 999 件" in target.body

    def test_every_fact_is_referenced_by_some_skill(self, monkeypatch):
        """反过来查一遍：注册了却没人引用的事实，说明文档漏写了，或者事实是多余的。

        两边都会漂：事实没人用 → 文档少了一块规范；事实多余 → 白维护。

        ## 为什么要用"哨兵值"而不是直接找值

        初版是拿 `describe()` 的值去正文里搜（"20%" 在不在正文里）。
        那是**假绿**：`20%` 同时是 `return_rate_warning`、`aov_gap_warning`
        和 `weight_price_gap` 的值，任何一个存在都会让另外两个"看起来被引用了"。

        所以这里把每个事实的值换成它自己的名字再渲染 —— 正文里出现
        `FACT<min_stock>` 就说明这个占位符**确实被写了**。
        """
        from fashionops.skills.facts import describe

        sentinel = {name: f"FACT<{name}>" for name, _ in describe()}
        monkeypatch.setattr(loader, "facts", lambda: sentinel)
        reload()

        corpus = "\n".join(s.body for s in all_skills())
        unused = [name for name in sentinel if f"FACT<{name}>" not in corpus]
        assert not unused, f"这些事实没有任何技能引用（写了占位符却没人用）：{unused}"


# ===========================================================================
# 4. 两条链的用法（渐进式披露 vs 直接注入）
# ===========================================================================
class TestTwoChains:
    def test_index_has_names_and_summaries_only(self):
        """索引里**不能有正文**。

        正文漏进索引 = 每个对话回合都付这几百个 token，
        而渐进式披露的全部意义就是避免这件事。
        """
        block = index_block()
        for skill in all_skills():
            assert skill.name in block
            assert skill.summary in block
            # 正文的任意一段都不该出现在索引里
            first_line = skill.body.splitlines()[0]
            assert first_line not in block, f"{skill.name} 的正文漏进了索引"

    def test_index_tells_the_model_how_to_load(self):
        """索引必须告诉模型"正文要用 skill.load 拉"。

        只说"有这些技能"而不给出获取方式，等于一个兑现不了的承诺 ——
        模型会凭技能名猜内容。
        """
        assert "skill.load" in index_block()

    def test_task_chain_injects_full_body(self):
        """任务链注入的是**正文**（题面已知，没有"要不要读"的决策）。"""
        from fashionops.agents.loop import _guide_for

        guide = _guide_for("analytics")
        # 正文里的规范细节必须在
        assert "insufficient_sample" in guide
        assert "detect_alerts" in guide

    def test_task_chain_generic_falls_back(self):
        from fashionops.agents.loop import _GENERIC_GUIDE, _guide_for

        assert _guide_for("generic") == _GENERIC_GUIDE

    def test_task_chain_guide_has_no_placeholder(self):
        from fashionops.agents.loop import _guide_for

        for kind in ("selection", "analytics", "notification", "generic"):
            assert "{{" not in _guide_for(kind)


# ===========================================================================
# 5. 工具对账与 skill.load 工具本身
# ===========================================================================
class TestSkillTool:
    def test_registered_with_empty_scopes(self):
        """`skill.load` 显式声明空作用域：技能文档不含数据、不触碰资源。

        但**读得到规范 ≠ 做得了那件事** —— 能不能做由具体工具的作用域决定。
        这条断言固定的是"它不需要权限"，不是"它绕过了权限"。
        """
        from fashionops.gateway.registry import registry

        spec = registry.get("skill.load")
        assert spec.scopes == frozenset()
        assert spec.read_only is True

    def test_visible_to_every_scope_set(self):
        from fashionops.gateway.registry import registry

        # 空作用域的工具对任何权限集合都可见（用户只有 kb:read 也应该能读技能）
        names = {
            schema["function"]["name"]
            for schema in registry.openai_schemas(frozenset({"kb:read"}))
        }
        assert "skill__load" in names

    def test_load_returns_body(self):
        from fashionops.gateway.registry import registry
        from fashionops.tools import register_all_tools

        if not len(registry):
            register_all_tools(registry)

        payload = registry.get("skill.load").handler("business-review")
        assert payload["found"] is True
        assert "退货率" in payload["content"]
        assert "erp.query_sales_summary" in payload["tools"]

    def test_unknown_skill_is_a_normal_result_not_an_exception(self):
        """和 `kb.search` 的没命中一样：这是正常结果，不是异常。

        抛异常会让模型看到一次"工具失败"，而它其实只是名字写错了 ——
        返回结构化原因 + hint，模型下一步能自己纠正。
        """
        from fashionops.gateway.registry import registry

        payload = registry.get("skill.load").handler("不存在的技能")
        assert payload["found"] is False
        assert payload["reason"] == "skill_not_found"
        assert "sku-selection" in payload["hint"]

    def test_declared_tools_exist(self):
        """技能里声明的工具必须真实存在（CLI `check` 用的就是这个函数）。"""
        from fashionops.gateway.registry import registry
        from fashionops.tools import register_all_tools

        if not len(registry):
            register_all_tools(registry)
        assert validate_skill_tools(registry) == []

    def test_missing_tool_is_reported(self, tmp_path, monkeypatch):
        monkeypatch.setattr(loader, "CONTENT_DIR", tmp_path)
        _write_skill(
            tmp_path,
            "a.md",
            "---\nname: broken\ntitle: 坏的\nsummary: 引用了不存在的工具\n"
            "tools:\n  - erp.query_nothing\n---\n正文",
        )
        loader.reload()

        from fashionops.gateway.registry import registry
        from fashionops.tools import register_all_tools

        if not len(registry):
            register_all_tools(registry)

        problems = validate_skill_tools(registry)
        assert len(problems) == 1
        assert "erp.query_nothing" in problems[0]


# ===========================================================================
# 6. ★ 对话链的接线（"算出来了但没喂给模型"是这里最容易退化的地方）
# ===========================================================================
@pytest.fixture
def session(db):  # noqa: ANN001, ANN201
    from sqlalchemy.orm import Session

    with Session(db) as s:
        yield s


class TestChatChainWiring:
    """技能索引必须**真的出现在发给模型的 system 消息里**。

    ## 为什么单测 `index_block()` 不够

    `index_block()` 返回什么，和"它有没有被拼进系统提示"是两件事。
    `chat/agent.py` 里那行拼接如果被某次重构删掉，**所有其他测试照样绿** ——
    模型只是从此不知道技能存在，`skill.load` 也永远不会被调用。
    这种退化没有任何报错，只是功能静静地没了。

    所以这里直接查 `MockModelClient` 收到的请求（和 `test_chat.py` 里
    "摘要必须真的进 system 消息"那条同一个思路）。
    """

    def test_skill_index_reaches_the_model(self, session):  # noqa: ANN001
        from unittest.mock import patch

        from fashionops.chat import agent, store
        from fashionops.llm import MockModelClient
        from fashionops.security.service import Principal

        created = store.create_conversation(username="zhang", user_id=None, title="技能接线")
        client = MockModelClient()
        principal = Principal(user_id=1, username="zhang", is_superuser=True)

        session.expire_all()
        with patch.object(agent, "get_model_client", return_value=client):
            list(
                agent.stream_turn(
                    conversation_no=created["conversation_no"],
                    principal=principal,
                    user_text="退货怎么处理",
                )
            )

        assert client.calls, "一轮都没跑起来，先看是不是 stream_turn 提前抛了"
        # `to_llm_messages` 的第一条就是 system
        system = client.calls[0][0].content
        for skill in all_skills():
            assert skill.name in system, f"{skill.name} 没进系统提示"
            assert skill.summary in system
        assert "skill.load" in system, "索引没告诉模型怎么读正文"

    def test_index_does_not_carry_full_bodies(self, session):  # noqa: ANN001
        """系统提示里**不能有正文** —— 那是每轮都要付的固定开销。"""
        from unittest.mock import patch

        from fashionops.chat import agent, store
        from fashionops.llm import MockModelClient
        from fashionops.security.service import Principal

        created = store.create_conversation(username="zhang", user_id=None, title="技能接线")
        client = MockModelClient()
        principal = Principal(user_id=1, username="zhang", is_superuser=True)

        session.expire_all()
        with patch.object(agent, "get_model_client", return_value=client):
            list(
                agent.stream_turn(
                    conversation_no=created["conversation_no"],
                    principal=principal,
                    user_text="退货怎么处理",
                )
            )

        system = client.calls[0][0].content
        for skill in all_skills():
            first_line = skill.body.splitlines()[0]
            assert first_line not in system, f"{skill.name} 的正文漏进了系统提示"

    def test_skill_tool_is_in_the_chat_catalog(self):
        """`skill.load` 必须出现在对话的工具目录里，否则索引是个空头承诺。"""
        from fashionops.chat.tools import build_catalog

        catalog = build_catalog(
            granted_scopes=frozenset({"kb:read"}), permission_codes=frozenset()
        )
        names = {item["function"]["name"] for item in catalog}
        assert "skill__load" in names
