"""对话域测试。

## 重点覆盖的是"不报错但结果是错的"那几类

1. **工具名重放**（本文件最重要的一条）：库里存内部名 `erp.query_inventory`，
   但模型只认 `erp__query_inventory`。重放时不转回去的话，模型会从历史里
   学到"工具名长这样、写在正文里就行"，于是把工具调用当文本吐出来 ——
   表现是"模型突然不会用工具了"，而根因在我们喂给它的历史。
   这个 bug 只在**第二轮之后**发作，特别像模型不稳定。

2. **孤立 tool 消息**：裁剪历史时一刀切在 assistant(tool_calls) 和它的
   tool 结果之间，会留下一条没有父请求的 tool 消息 → OpenAI 兼容接口直接 400。

3. **审批策略**：写操作必须拦、只读操作不能拦（拦多了人会无脑点"同意"）。
   而且**未知工具默认为危险** —— 漏拦的代价是无人值守改线上数据。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from fashionops.config import Settings
from fashionops.db import base as db_base
from fashionops.db.base import Base, reset_engine
from fashionops.db.models_chat import (
    APPROVAL_STATUS_PENDING,
    MSG_ROLE_ASSISTANT,
    MSG_ROLE_TOOL,
    MSG_ROLE_USER,
    Conversation,
)
from fashionops.security.permissions import sync_identity


def conversation_id_of(session: Session, conversation_no: str) -> int:
    """按对外编号取主键 id。

    不用 `create_conversation` 的返回值：它返回的是**对外的字典**，
    里面刻意没有自增 id（对外暴露 id 会泄漏"系统里总共有多少会话"）。
    """
    value = session.scalar(
        select(Conversation.id).where(Conversation.conversation_no == conversation_no)
    )
    assert value is not None
    return int(value)


@pytest.fixture
def db(tmp_path, monkeypatch):  # noqa: ANN001, ANN201
    """临时 SQLite 库（含对话三张表）。"""
    from fashionops.db.base import build_engine

    settings = Settings(data_dir=tmp_path, db_host=None)
    reset_engine()
    monkeypatch.setattr(db_base, "get_settings", lambda: settings)

    engine = build_engine(settings)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        sync_identity(s)
        s.commit()

    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()
    reset_engine()


@pytest.fixture
def session(db):  # noqa: ANN001, ANN201
    with Session(db) as s:
        yield s


@pytest.fixture(autouse=True)
def tools_registered():
    """把业务工具注册进进程内注册表（`needs_approval` / `build_catalog` 依赖它）。"""
    from fashionops.gateway.registry import registry
    from fashionops.tools import register_all_tools

    if not len(registry):
        register_all_tools(registry)


# ===========================================================================
# 1. 工具名重放（最重要）
# ===========================================================================
class TestToolNameReplay:
    def _seed(self, session: Session, tool_name: str) -> int:
        """造一场"已调过一次工具"的对话。"""
        from fashionops.chat import store

        conversation = store.create_conversation(username="zhang", user_id=None, title="t")
        conversation_id = conversation_id_of(session, conversation["conversation_no"])
        store.append_message(conversation_id, role=MSG_ROLE_USER, content="查库存")
        store.append_message(
            conversation_id,
            role=MSG_ROLE_ASSISTANT,
            content="",
            tool_call_id="call_1",
            tool_name=tool_name,
            tool_arguments={"sku_codes": ["A1"]},
            finish_reason="tool_calls",
        )
        store.append_message(
            conversation_id,
            role=MSG_ROLE_TOOL,
            content='{"ok": true}',
            tool_call_id="call_1",
            tool_name=tool_name,
        )
        return conversation_id

    def test_internal_dotted_name_is_converted_for_the_model(self, session):
        """``erp.query_inventory`` 必须以 ``erp__query_inventory`` 喂给模型。

        转不转的区别：模型看到的历史里，assistant 调用的函数名**必须和
        本轮声明的函数名一致**。不一致的话它学到的是一组不存在的函数名，
        接着就会把工具调用写成正文 —— 一个非常难定位的表现。
        """
        from fashionops.chat import store

        conversation_id = self._seed(session, "erp.query_inventory")
        rows = store.history(session, conversation_id)
        messages = store.to_llm_messages(rows, system_prompt="sys")

        assistant = [m for m in messages if m.role == "assistant" and m.tool_calls]
        assert assistant, "应该有带 tool_calls 的 assistant 消息"
        assert assistant[0].tool_calls[0].name == "erp__query_inventory"

        tool_messages = [m for m in messages if m.role == "tool"]
        assert tool_messages
        assert tool_messages[0].name == "erp__query_inventory"

    def test_dotted_name_never_leaks_to_the_model(self, session):
        """整个上下文里**不允许出现**点号形式的名字。"""
        from fashionops.chat import store

        conversation_id = self._seed(session, "mail.send")
        messages = store.to_llm_messages(
            store.history(session, conversation_id), system_prompt="sys"
        )
        raw = str([m.model_dump() for m in messages])
        assert "mail.send" not in raw
        assert "mail__send" in raw

    def test_orphan_tool_message_is_dropped(self):
        """没有父请求的 tool 消息必须丢掉 —— 留着会让整个请求 400。"""
        from fashionops.chat.store import _drop_orphan_tools
        from fashionops.llm import Message

        messages = [
            Message.system("s"),
            Message.tool(tool_call_id="ghost", content="{}"),
            Message.user("hi"),
        ]
        out = _drop_orphan_tools(messages)
        assert [m.role for m in out] == ["system", "user"]

    def test_paired_tool_message_is_kept(self):
        from fashionops.chat.store import _drop_orphan_tools
        from fashionops.llm import Message, ToolCall

        messages = [
            Message.assistant("", tool_calls=[ToolCall(id="c1", name="t", arguments={})]),
            Message.tool(tool_call_id="c1", content="{}"),
        ]
        assert len(_drop_orphan_tools(messages)) == 2


# ===========================================================================
# 2. 审批策略
# ===========================================================================
class TestApprovalPolicy:
    def test_write_tool_needs_approval(self):
        """发邮件收不回来 —— 必须拦。"""
        from fashionops.chat.tools import needs_approval

        assert needs_approval("mail.send") is True

    def test_readonly_tool_does_not_need_approval(self):
        """只读工具拦下来纯属添堵，而且会让"需要审批"这件事贬值。"""
        from fashionops.chat.tools import needs_approval

        assert needs_approval("mail.policy") is False
        assert needs_approval("erp.query_inventory") is False

    def test_task_submit_is_exempt(self):
        """建任务只是入队，任务内部还有一道闸口。

        两道闸口叠在一起会让人开始无脑点"同意" —— 闸口就退化成形式主义了。
        """
        from fashionops.chat.tools import needs_approval

        assert needs_approval("task.submit") is False

    def test_unknown_tool_defaults_to_dangerous(self):
        """**未知的一律按危险处理。**

        漏拦一个写工具的代价是"无人值守地改了线上数据"；
        误拦一个只读工具的代价只是"多点一次确认"。代价不对等。
        """
        from fashionops.chat.tools import needs_approval

        assert needs_approval("brand.new.write.thing") is True

    def test_catalog_respects_granted_scopes(self):
        """用户权限 ∩ Agent 权限集：给不出作用域的工具**在目录里就不出现**。

        让模型看见再被拒会浪费一整轮 token，还容易陷入重试循环。
        """
        from fashionops.chat.tools import build_catalog

        catalog = build_catalog(
            granted_scopes=frozenset({"erp:read"}), permission_codes=frozenset()
        )
        names = {item["function"]["name"] for item in catalog}
        assert "erp__query_inventory" in names
        # mail:send 没给 → 发信工具不该出现
        assert "mail__send" not in names
        # 没有 task:submit 权限 → 提交任务的工具也不该出现
        assert "task__submit" not in names

    def test_catalog_adds_task_submit_only_with_permission(self):
        from fashionops.chat.tools import build_catalog

        with_perm = build_catalog(
            granted_scopes=frozenset({"erp:read"}), permission_codes=frozenset({"task:submit"})
        )
        assert "task__submit" in {i["function"]["name"] for i in with_perm}


# ===========================================================================
# 3. 给人看的摘要
# ===========================================================================
class TestApprovalPreview:
    def test_mail_preview_is_human_readable(self):
        """**不能把 JSON 甩给用户。**

        让运营去读 `{"to":["a@b.com"],"body":"..."}` 然后点"同意"，
        那不叫审批，叫背锅。
        """
        from fashionops.chat.tools import describe_preview

        preview = describe_preview(
            "mail.send",
            {
                "to_addrs": ["ops@example.com", "boss@example.com"],
                "subject": "今日库存提醒",
                "body_text": "SKU00042 库存 128 件",
            },
        )
        assert "收件人：ops@example.com、boss@example.com" in preview
        assert "主题：今日库存提醒" in preview
        assert "正文" in preview
        # 不是原始 JSON
        assert not preview.lstrip().startswith("{")

    def test_long_body_is_truncated(self):
        from fashionops.chat.tools import describe_preview

        preview = describe_preview(
            "mail.send", {"to_addrs": ["a@b.com"], "subject": "s", "body_text": "字" * 500}
        )
        assert "…" in preview
        assert len(preview) < 600

    def test_generic_preview_falls_back(self):
        from fashionops.chat.tools import describe_preview

        preview = describe_preview("erp.query_inventory", {"sku_codes": ["A1", "B2"]})
        assert "erp.query_inventory" in preview
        assert "SKU：A1、B2" in preview


# ===========================================================================
# 4. 持久化
# ===========================================================================
class TestStore:
    def test_conversation_no_is_not_the_primary_key(self, session):
        """对外编号不能是自增 id —— 那会泄漏"系统里总共有多少会话"。"""
        from fashionops.chat import store

        row = store.create_conversation(username="zhang", user_id=None, title="测试")
        assert row["conversation_no"].startswith("CH")
        assert row["title"] == "测试"
        assert "id" not in row

    def test_seq_increments_within_conversation(self, session):
        """seq 是**会话内**递增的。用全局自增 id 做游标会让前端收到别的会话的空洞。"""
        from fashionops.chat import store

        created = store.create_conversation(username="z", user_id=None)
        cid = conversation_id_of(session, created["conversation_no"])
        first = store.append_message(cid, role=MSG_ROLE_USER, content="a")
        second = store.append_message(cid, role=MSG_ROLE_USER, content="b")
        assert (first["seq"], second["seq"]) == (1, 2)

    def test_bump_aggregates_on_conversation(self, session):
        """汇总值随消息累加（会话列表页靠它，不用 JOIN 聚合几十万行消息）。"""
        from fashionops.chat import store

        created = store.create_conversation(username="z", user_id=None)
        cid = conversation_id_of(session, created["conversation_no"])
        store.append_message(
            cid, role=MSG_ROLE_ASSISTANT, content="x", prompt_tokens=10, completion_tokens=5
        )
        session.expire_all()
        row = session.get(Conversation, cid)
        assert row is not None
        assert row.message_count == 1
        assert row.total_tokens == 15

    def test_history_returns_in_order(self, session):
        from fashionops.chat import store

        created = store.create_conversation(username="z", user_id=None)
        cid = conversation_id_of(session, created["conversation_no"])
        for text in ("a", "b", "c"):
            store.append_message(cid, role=MSG_ROLE_USER, content=text)
        session.expire_all()
        rows = store.history(session, cid)
        assert [r.content for r in rows] == ["a", "b", "c"]


class TestApprovalLifecycle:
    def _conversation(self, session) -> int:  # noqa: ANN001
        from fashionops.chat import store

        created = store.create_conversation(username="z", user_id=None)
        return conversation_id_of(session, created["conversation_no"])

    def test_create_and_list_pending(self, session):
        from fashionops.chat import store

        cid = self._conversation(session)
        approval = store.create_approval(
            conversation_id=cid,
            message_id=None,
            tool_call_id="c1",
            tool_name="mail.send",
            arguments={"to_addrs": ["a@b.com"]},
            preview="收件人：a@b.com",
            reason="写操作需要人工确认",
        )
        assert approval["status"] == APPROVAL_STATUS_PENDING

        session.expire_all()
        pending = store.pending_for_conversation(session, cid)
        assert len(pending) == 1
        assert pending[0].approval_no == approval["approval_no"]

    def test_approval_no_is_not_sequential(self, session):
        """审批单号不能是连续自增的 —— 那会让人能猜出"系统里有多少待办"。"""
        from fashionops.chat import store

        cid = self._conversation(session)
        a1 = store.create_approval(
            conversation_id=cid,
            message_id=None,
            tool_call_id="c1",
            tool_name="mail.send",
            arguments={},
        )
        a2 = store.create_approval(
            conversation_id=cid,
            message_id=None,
            tool_call_id="c2",
            tool_name="mail.send",
            arguments={},
        )
        assert a1["approval_no"] != a2["approval_no"]
        assert a1["approval_no"].startswith("AP")
