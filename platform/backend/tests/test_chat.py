"""对话域测试。

## 重点覆盖的是"不报错但结果是错的"那几类

1. **工具名重放**（本文件最重要的一条）：库里存内部名 `erp.query_inventory`，
   但模型只认 `erp__query_inventory`。重放时不转回去的话，模型会从历史里
   学到"工具名长这样、写在正文里就行"，于是把工具调用当文本吐出来 ——
   表现是"模型突然不会用工具了"，而根因在我们喂给它的历史。
   这个 bug 只在**第二轮之后**发作，特别像模型不稳定。

2. **tool_calls 与 tool 消息的配对**：两个方向都会 400。
   一是裁剪时留下"没有父请求的 tool 消息"；二是**没有结果的 assistant(tool_calls)**
   —— 后者不是裁剪造成的，是常见的用户行为：写操作挂起等确认时，
   用户没点卡片而是接着说了句别的话（`test_pending_approval_...`）。
   这两种都只在**真实模型**上炸（mock 不校验配对），所以必须专门断言。

3. **审批策略**：写操作必须拦、只读操作不能拦（拦多了人会无脑点"同意"）。
   而且**未知工具默认为危险** —— 漏拦的代价是无人值守改线上数据。
"""

from __future__ import annotations

import pytest
from fashionops.chat import store
from fashionops.config import Settings
from fashionops.db import base as db_base
from fashionops.db.base import Base, reset_engine
from fashionops.db.models_chat import (
    APPROVAL_STATUS_EXPIRED,
    APPROVAL_STATUS_PENDING,
    MSG_ROLE_ASSISTANT,
    MSG_ROLE_TOOL,
    MSG_ROLE_USER,
    Conversation,
)
from fashionops.security.permissions import sync_identity
from sqlalchemy import select
from sqlalchemy.orm import Session


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


def assert_tool_pairs_valid(messages):  # noqa: ANN001, ANN201
    """按 OpenAI 兼容接口的要求检查上下文是否合法。

    mock 客户端**不校验**这些约束，所以这里手工查一遍 —— 不然会得到
    "测试全绿、一接真实模型就 400" 这种最难查的失败。

    两条规则：
    1. 每个 ``assistant(tool_calls)`` 后面必须**紧跟**它全部 call id 的 tool 结果
    2. 每条 tool 消息必须有对应的父请求
    """
    for index, message in enumerate(messages):
        if message.role != "assistant" or not message.tool_calls:
            continue
        wanted = {call.id for call in message.tool_calls}
        answered: set[str | None] = set()
        for follower in messages[index + 1 :]:
            if follower.role != "tool":
                break
            answered.add(follower.tool_call_id)
        assert answered == wanted, f"第 {index} 条的 tool_calls {wanted} 没配齐，只拿到 {answered}"

    for index, message in enumerate(messages):
        if message.role != "tool":
            continue
        parents = {
            call.id
            for earlier in messages[:index]
            if earlier.role == "assistant"
            for call in (earlier.tool_calls or [])
        }
        assert message.tool_call_id in parents, f"第 {index} 条 tool 消息没有父请求"


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
        from fashionops.chat.store import _sanitize_tool_pairs
        from fashionops.llm import Message

        messages = [
            Message.system("s"),
            Message.tool(tool_call_id="ghost", content="{}"),
            Message.user("hi"),
        ]
        out = _sanitize_tool_pairs(messages)
        assert [m.role for m in out] == ["system", "user"]

    def test_paired_tool_message_is_kept(self):
        from fashionops.chat.store import _sanitize_tool_pairs
        from fashionops.llm import Message, ToolCall

        messages = [
            Message.assistant("", tool_calls=[ToolCall(id="c1", name="t", arguments={})]),
            Message.tool(tool_call_id="c1", content="{}"),
        ]
        out = _sanitize_tool_pairs(messages)
        assert len(out) == 2
        assert out[0].tool_calls is not None, "配对成功的调用不该被摘掉"
        assert_tool_pairs_valid(out)

    def test_dangling_tool_call_keeps_content_but_drops_calls(self):
        """**没有结果的 assistant(tool_calls) 要摘掉 tool_calls、保住正文。**

        直接 400 的那条规则：assistant 带 tool_calls 就必须紧跟 tool 结果。
        而它的正文（"我准备发一封邮件给运营组，请确认"）是用户已经看过的内容 ——
        整条丢掉会让模型下一轮不知道自己说过什么，于是重新说一遍。
        """
        from fashionops.chat.store import _sanitize_tool_pairs
        from fashionops.llm import Message, ToolCall

        messages = [
            Message.assistant(
                "我准备发一封邮件给运营组，请确认",
                tool_calls=[ToolCall(id="c1", name="mail__send", arguments={})],
            ),
            Message.user("算了，先看看库存"),
        ]
        out = _sanitize_tool_pairs(messages)
        assert out[0].tool_calls is None
        assert out[0].content == "我准备发一封邮件给运营组，请确认"
        assert_tool_pairs_valid(out)

    def test_tool_result_separated_by_another_message_does_not_count(self):
        """配对判据是"**紧跟**"，不是"后面某处出现过"。

        中间隔了一条 user 消息的 tool 结果不算数 —— 接口就是这么要求的。
        """
        from fashionops.chat.store import _sanitize_tool_pairs
        from fashionops.llm import Message, ToolCall

        messages = [
            Message.assistant("", tool_calls=[ToolCall(id="c1", name="t", arguments={})]),
            Message.user("喂？"),
            Message.tool(tool_call_id="c1", content="{}"),
        ]
        out = _sanitize_tool_pairs(messages)
        assert [m.role for m in out] == ["assistant", "user"]
        assert out[0].tool_calls is None
        assert_tool_pairs_valid(out)

    def test_partially_answered_calls_keep_the_answered_one(self):
        """一次发两个调用、只回了一个：留下有结果的那个，摘掉没结果的。"""
        from fashionops.chat.store import _sanitize_tool_pairs
        from fashionops.llm import Message, ToolCall

        messages = [
            Message.assistant(
                "",
                tool_calls=[
                    ToolCall(id="c1", name="a", arguments={}),
                    ToolCall(id="c2", name="b", arguments={}),
                ],
            ),
            Message.tool(tool_call_id="c1", content="{}"),
        ]
        out = _sanitize_tool_pairs(messages)
        assert [call.id for call in out[0].tool_calls or []] == ["c1"]
        assert_tool_pairs_valid(out)

    def test_pending_approval_then_new_user_message_stays_valid(self, session):
        """**这条是真实用户行为触发的 bug，不是裁剪造成的。**

        写操作挂起等确认时用户没点卡片、直接接着说了句别的 ——
        历史里就留下一条没有结果的 assistant(tool_calls)。
        下一次请求会 400，表现是"聊到一半突然报错"。
        """
        from fashionops.chat import store

        conversation_id = self._seed_pending_approval(session)
        # 用户没点卡片，直接问了别的
        store.append_message(conversation_id, role=MSG_ROLE_USER, content="算了，先看看库存")

        session.expire_all()
        messages = store.to_llm_messages(
            store.history(session, conversation_id), system_prompt="sys"
        )
        assert_tool_pairs_valid(messages)

        # 正文还在：模型得知道自己提过这件事、但没执行
        assistant_texts = [m.content for m in messages if m.role == "assistant"]
        assert any("请确认" in text for text in assistant_texts)

    @staticmethod
    def _seed_pending_approval(session):  # noqa: ANN001, ANN205
        """造一场"发信挂起等确认"的对话。"""
        from fashionops.chat import store

        conversation = store.create_conversation(username="zhang", user_id=None, title="t")
        conversation_id = conversation_id_of(session, conversation["conversation_no"])
        store.append_message(conversation_id, role=MSG_ROLE_USER, content="给运营组发份报表")
        store.append_message(
            conversation_id,
            role=MSG_ROLE_ASSISTANT,
            content="我准备发一封邮件给运营组，请确认",
            tool_call_id="call_mail",
            tool_name="mail.send",
            tool_arguments={"to_addrs": ["ops@example.com"]},
            finish_reason="approval_required",
        )
        return conversation_id


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


# ===========================================================================
# 5. 追问（模型不确定时问用户）
# ===========================================================================
class TestAskUser:
    """和审批是**两件事**，测试也要分开：

    * 审批 = 策略判定"这个动作能不能做"，模型是被动的
    * 追问 = 模型自己承认"我不知道用户要什么"

    合并成一套的代价在留痕上 —— 审批台账会混进"回答问题"的记录，
    "现在有几个闸口卡着人"这条运维查询就废了。
    """

    def test_ask_user_is_always_in_catalog(self):
        """追问**不看权限**：让用户把需求说清楚不触碰任何资源。

        反过来（没有这个工具）的代价是模型只能猜 —— 而它猜错的成本
        是"发出一份渠道弄错、收件人也弄错的报表"，比多问一句大得多。
        """
        from fashionops.chat.tools import ASK_USER, build_catalog

        catalog = build_catalog(granted_scopes=frozenset(), permission_codes=frozenset())
        assert ASK_USER in {item["function"]["name"] for item in catalog}

    def test_ask_user_pauses_the_turn_as_a_plain_message(self, session):  # noqa: ANN001
        """模型说"我不确定"，这一轮就该停下来等人。

        而且**不能落成工具调用** —— 带上 tool_call_id 的话，
        用户下一句回复是 user 消息、中间没有 tool 消息，下一次请求直接 400。
        """
        _, events = self._run_clarify_turn(session)

        assert events[-1] == {"type": "done", "status": "waiting_user"}

        asked = [
            event["message"]
            for event in events
            if event["type"] == "message"
            and event["message"]["finish_reason"] == "awaiting_user"
        ]
        assert len(asked) == 1
        # 这三条是"不落成工具调用"的落点
        assert asked[0]["tool_call_id"] is None
        assert asked[0]["tool_name"] is None
        assert asked[0]["finish_reason"] == "awaiting_user"

        # 问题、选项、默认方案都要写给用户 —— 让他能少打字
        assert "你要看哪个渠道的数据？" in asked[0]["content"]
        assert "1. 淘宝" in asked[0]["content"]
        assert "全渠道" in asked[0]["content"]

    def test_answer_replays_as_valid_history(self, session):  # noqa: ANN001
        """用户回答之后，历史仍然是合法的一问一答。

        这是上面那条"不落成工具调用"的**验收标准**：重放出来的上下文
        必须能通过 OpenAI 的配对校验。mock 不校验，所以这里手工查。
        """
        from fashionops.chat import store

        conversation_no, _ = self._run_clarify_turn(session)
        session.expire_all()
        conversation_id = conversation_id_of(session, conversation_no)

        store.append_message(conversation_id, role=MSG_ROLE_USER, content="抖音")

        session.expire_all()
        messages = store.to_llm_messages(
            store.history(session, conversation_id), system_prompt="sys"
        )
        assert_tool_pairs_valid(messages)

        roles = [m.role for m in messages]
        assert roles == ["system", "user", "assistant", "user"]
        assert messages[3].content == "抖音"

    @staticmethod
    def _run_clarify_turn(session):  # noqa: ANN001, ANN205
        """跑一个"模型不确定 → 提问"的回合，返回 ``(会话号, 事件列表)``。

        用 ``MockModelClient`` 的 script 强制模型调 ``ask_user`` ——
        零 API Key、零网络，但走的是**和真实模型完全同一条链路**
        （目录 → 循环 → tool_runner → 挂起 → 落库 → SSE）。
        """
        from unittest.mock import patch

        from fashionops.chat import agent, store
        from fashionops.llm import MockModelClient, MockReply, ToolCall
        from fashionops.security.service import Principal

        # user_id 给 None：测试库里没有 users 行，挂一个不存在的 id 会撞外键。
        # 这一列本来就是可空的（"会话当时是谁的"用 username 快照兜底）。
        created = store.create_conversation(username="zhang", user_id=None, title="t")
        conversation_no = created["conversation_no"]

        client = MockModelClient(
            script=[
                MockReply(
                    content="这个需求有两种理解。",
                    tool_calls=[
                        ToolCall(
                            id="call_ask",
                            name="ask_user",
                            arguments={
                                "question": "你要看哪个渠道的数据？",
                                "options": ["淘宝", "抖音"],
                                "assumption": "全渠道",
                            },
                        )
                    ],
                )
            ]
        )
        principal = Principal(user_id=1, username="zhang", is_superuser=True)

        session.expire_all()
        with patch.object(agent, "get_model_client", return_value=client):
            events = list(
                agent.stream_turn(
                    conversation_no=conversation_no,
                    principal=principal,
                    user_text="帮我出一份报表",
                )
            )
        return conversation_no, events


# ===========================================================================
# 6. 审批单的有效期
# ===========================================================================
class TestApprovalExpiry:
    """`APPROVAL_STATUS_EXPIRED` 从表建起来那天就在，却**一直没有代码写它** ——
    于是"用户等太久没点"这件事在数据上从未发生过，幽灵单永远挂着。

    这里锁住三件事：
    1. 过期的单子**不再出现在界面上**（读取侧：hide + 落库作废）
    2. 过期的单子**不能被执行**（执行口：这才是安全边界）
    3. `ttl=0` 能退回旧行为（给"人就坐在电脑前"的小团队留退路）
    """

    def _conversation(self, session) -> int:  # noqa: ANN001
        from fashionops.chat import store

        created = store.create_conversation(username="z", user_id=None)
        return conversation_id_of(session, created["conversation_no"])

    def _aged_approval(self, session, conversation_id: int, *, minutes_ago: int) -> dict:  # noqa: ANN001
        """造一张 `created_at` 在 N 分钟前的待确认单。

        `create_approval` 走的是列默认值（= 现在），没法伪造"三天前"，
        所以建完直接把 `created_at` 改掉 —— 这是唯一能测到过期路径的办法。
        """
        from datetime import timedelta

        from fashionops.chat import store
        from fashionops.db.base import utcnow
        from fashionops.db.models_chat import Approval

        approval = store.create_approval(
            conversation_id=conversation_id,
            message_id=None,
            tool_call_id="c1",
            tool_name="mail.send",
            arguments={"to_addrs": ["ops@example.com"]},
            preview="收件人：ops@example.com",
        )
        session.expire_all()
        row = session.get(Approval, approval["id"])
        assert row is not None
        row.created_at = utcnow() - timedelta(minutes=minutes_ago)
        session.commit()
        return approval

    def test_fresh_approval_is_visible(self, session):  # noqa: ANN001
        """刚提的单子当然要能看到 —— 否则这道闸口就没用了。"""
        from fashionops.chat import store

        conversation_id = self._conversation(session)
        self._aged_approval(session, conversation_id, minutes_ago=1)

        session.expire_all()
        assert len(store.pending_for_conversation(session, conversation_id)) == 1

    def test_stale_approval_is_hidden_and_marked_expired(self, session):  # noqa: ANN001
        """过期单既不再返回，也要**落库**成 expired（看板和复盘都靠它）。"""
        from fashionops.chat import store
        from fashionops.db.models_chat import APPROVAL_STATUS_EXPIRED, Approval

        conversation_id = self._conversation(session)
        approval = self._aged_approval(session, conversation_id, minutes_ago=999)

        session.expire_all()
        assert store.pending_for_conversation(session, conversation_id) == []

        # 不是"藏起来了"就完事 —— 状态本身也要落库，
        # 否则 `WHERE status='pending'` 那条运维查询照样数到它
        session.expire_all()
        row = session.get(Approval, approval["id"])
        assert row is not None
        assert row.status == APPROVAL_STATUS_EXPIRED

    def test_resolve_approval_refuses_stale_single(self, session):  # noqa: ANN001
        """**执行口必须拦住。**

        这一条是整组里最重要的：读取侧的过滤只是"让用户看不到"，
        它拦不住任何一个拿着审批单号直接 POST 的人 —— 那不构成安全边界。
        真正拦住的是 `resolve_approval` 里执行前那次判断。
        """
        from fashionops.chat import agent, store
        from fashionops.security.service import Principal

        created = store.create_conversation(username="z", user_id=None, title="t")
        conversation_no = created["conversation_no"]
        session.expire_all()
        conversation_id = conversation_id_of(session, conversation_no)

        approval = self._aged_approval(session, conversation_id, minutes_ago=999)
        principal = Principal(user_id=1, username="z", is_superuser=True)

        # resolve_approval 是生成器：惰性，必须消费才真的跑起来
        with pytest.raises(ValueError, match="作废"):
            list(
                agent.resolve_approval(
                    conversation_no=conversation_no,
                    principal=principal,
                    approval_no=approval["approval_no"],
                    approved=True,
                )
            )

    def test_ttl_zero_disables_expiry(self, session, monkeypatch):  # noqa: ANN001
        """`ttl=0` = 永不过期。

        留这个开关是因为有些团队确实希望单子一直挂着（人就坐在电脑前，
        提完立刻点）。但默认是**开**的：漏拦的代价是"三天前的邮件照发"，
        误拦的代价只是"再问一次"。代价不对等。
        """
        from fashionops.approval import lifecycle
        from fashionops.chat import store

        monkeypatch.setattr(lifecycle, "ttl_minutes", lambda: 0)

        conversation_id = self._conversation(session)
        self._aged_approval(session, conversation_id, minutes_ago=100_000)

        session.expire_all()
        assert len(store.pending_for_conversation(session, conversation_id)) == 1

    def test_pending_for_task_reports_expired_separately(self, session):  # noqa: ANN001
        """任务侧审批端要能区分"没在等确认"和"在等但过期了"。

        两者给用户的提示完全不同：一个是"去看任务状态"，
        另一个是"重新发起"。混成一句会让用户去检查任务号，排查方向跑偏。
        """
        from datetime import timedelta

        from fashionops.approval import records
        from fashionops.db import session_scope
        from fashionops.db.base import utcnow
        from fashionops.db.models_chat import APPROVAL_STATUS_EXPIRED, Approval

        task_no = "T2609190001"
        approval = records.open_for_todo(
            task_no=task_no,
            todo_seq=1,
            tool_call_id="c1",
            tool_name="mail.send",
            arguments={"to_addrs": ["ops@example.com"]},
        )
        with session_scope() as s:
            row = s.get(Approval, approval["id"])
            assert row is not None
            row.created_at = utcnow() - timedelta(minutes=999)

        # 界面上看不到（幽灵单不该出现）
        assert records.pending_for_task(task_no) is None
        # 但审批端看得到，而且知道它是"过期"而不是"没有"
        expired = records.pending_for_task(task_no, include_expired=True)
        assert expired is not None
        assert expired["status"] == APPROVAL_STATUS_EXPIRED

    def test_resume_task_asks_for_expired_approvals_too(self, monkeypatch):
        """`resume_task` **必须**能看到过期单，否则驳回会静默 noop。

        这是一条**回归测试**，锁的是一个真实的坑：给 `pending_for_task`
        加上"隐藏过期单"之后，这个调用点忘了跟着改，
        于是点「驳回」会走到"没有待处理的审批单"分支 ——
        没有报错、没有日志异常，任务永远卡在 `waiting_approval`，
        而 `approved_by` 已经被写上了。

        这种"两个模块各自都对、合起来错"的地方，测试必须直接断言
        **调用参数**，不能只测各自的单元行为。
        """
        from fashionops.agents import loop
        from fashionops.approval import records

        seen: list[dict] = []

        def fake_pending_for_task(task_no, **kwargs):  # noqa: ANN001, ANN202
            seen.append({"task_no": task_no, **kwargs})
            return None  # 走"无事可做"分支，避免真去建一个任务

        monkeypatch.setattr(records, "pending_for_task", fake_pending_for_task)
        result = loop.resume_task(
            "T2609190009", decision={"approved": False, "by": "z"}, recorder=object()
        )

        assert result["status"] == "noop"
        assert seen and seen[0].get("include_expired") is True

    def test_resume_task_refuses_expired_approval_when_approving(self, monkeypatch):
        """纵深防御：绕过 API 直接调 resume、想执行一张过期单 → 必须拒绝。

        这里抛的是异常而不是 noop —— `execution.resume` 会接住并
        `mark_failed`，任务能落到终态；noop 会让它永远挂着。

        顺带覆盖 `is_expired` 吃 ISO 字符串那条路径：
        `records.serialize` 给出来的 `created_at` 就是字符串。
        """
        from fashionops.agents import loop
        from fashionops.approval import records

        monkeypatch.setattr(
            records,
            "pending_for_task",
            lambda task_no, **kwargs: {  # noqa: ARG005
                "status": APPROVAL_STATUS_EXPIRED,
                "tool_name": "mail.send",
                "created_at": "2020-01-01T00:00:00",
            },
        )

        with pytest.raises(RuntimeError, match="作废"):
            loop.resume_task(
                "T2609190010", decision={"approved": True, "by": "z"}, recorder=object()
            )

    def test_both_sides_serialize_the_same_fields(self, session):  # noqa: ANN001
        """两侧的审批形状必须**逐字段一致** —— 前端一个组件渲染两边。

        这条断言直接来自 `records.serialize` 里那句"字段名与
        `chat/store.serialize_approval` 保持一致"。写下来之前它就漏了
        `created_at`，后果很隐蔽：**只有任务侧的卡片不显示"什么时候提的"**，
        而对话侧的显示 —— 这种一边有一边没有最容易被认为是前端写错了。
        """
        from fashionops.approval import records
        from fashionops.chat import store

        conversation_id = self._conversation(session)
        chat_side = store.create_approval(
            conversation_id=conversation_id,
            message_id=None,
            tool_call_id="c1",
            tool_name="mail.send",
            arguments={"to_addrs": ["a@b.com"]},
        )
        task_side = records.open_for_todo(
            task_no="T2609190002",
            todo_seq=1,
            tool_call_id="c2",
            tool_name="mail.send",
            arguments={"to_addrs": ["a@b.com"]},
        )
        assert set(chat_side) == set(task_side)


# ===========================================================================
# 7. 记忆压缩（短期记忆超出上下文窗口之后）
# ===========================================================================
def _seed_long_conversation(session, *, pairs: int = 30) -> tuple[str, int]:  # noqa: ANN001
    """造一场比上下文窗口长的对话，返回 ``(会话号, 会话 id)``。

    默认 30 组问答 = 60 条消息，而窗口是 40 条 —— 正好有 20 条掉出去。
    """
    from fashionops.chat import store

    created = store.create_conversation(username="z", user_id=None, title="t")
    conversation_no = created["conversation_no"]
    session.expire_all()
    conversation_id = conversation_id_of(session, conversation_no)
    for index in range(pairs):
        store.append_message(conversation_id, role=MSG_ROLE_USER, content=f"问题 {index}")
        store.append_message(
            conversation_id, role=MSG_ROLE_ASSISTANT, content=f"回答 {index}"
        )
    return conversation_no, conversation_id


class TestMemoryCompression:
    """超出上下文窗口的历史**不再被静默丢掉**，而是折叠成摘要留下来。

    改动前：`MAX_CONTEXT_MESSAGES` 一刀切，丢掉的部分没有任何痕迹，
    用户觉得"它忘了我们开头说的类目"，排查时连个异常都看不到。

    这里锁住的是"压缩确实发生了、记到了正确的边界上、失败不打断对话"。
    """

    def test_no_summary_when_conversation_fits_in_window(self, session):  # noqa: ANN001
        """没超出窗口就**一次模型调用都不该多**。"""
        from fashionops.chat import memory, store
        from fashionops.llm import MockModelClient

        created = store.create_conversation(username="z", user_id=None)
        session.expire_all()
        conversation_id = conversation_id_of(session, created["conversation_no"])
        store.append_message(conversation_id, role=MSG_ROLE_USER, content="查下库存")

        client = MockModelClient()
        assert memory.maybe_refresh(conversation_id, model_client=client) == ""
        assert client.calls == [], "没超出窗口却调了模型 —— 白白多花一次钱"

    def test_summary_is_generated_and_recorded(self, session):  # noqa: ANN001
        """超出窗口的历史要压成摘要，并记住**压到哪一条**。"""
        from fashionops.chat import memory
        from fashionops.llm import MockModelClient

        _, conversation_id = _seed_long_conversation(session)

        client = MockModelClient()
        summary = memory.maybe_refresh(conversation_id, model_client=client)
        assert summary, "超出窗口却没生成摘要"
        assert len(client.calls) == 1, "第一次压缩应该只调一次模型"

        session.expire_all()
        conversation = session.get(Conversation, conversation_id)
        assert conversation is not None
        assert conversation.memory_summary == summary
        # 60 条消息、窗口 40 条 → 掉出去的是 seq 1..20
        assert conversation.memory_summary_upto_seq == 20

    def test_second_call_reuses_the_summary(self, session):  # noqa: ANN001
        """已经覆盖到窗口边界时不再压缩 —— 否则每轮都要多一次模型调用。"""
        from fashionops.chat import memory
        from fashionops.llm import MockModelClient

        _, conversation_id = _seed_long_conversation(session)
        client = MockModelClient()
        first = memory.maybe_refresh(conversation_id, model_client=client)

        client.calls.clear()
        assert memory.maybe_refresh(conversation_id, model_client=client) == first
        assert client.calls == []

    def test_refresh_waits_for_the_threshold(self, session):  # noqa: ANN001
        """新掉出去的不足阈值时**先不刷** —— 这是省钱和新鲜度之间的取舍。

        一旦对话超过窗口，之后每轮都会有消息掉出去；每轮都刷 =
        每轮多一次模型调用。所以攒够 `chat_summary_refresh_every` 条再刷。
        """
        from fashionops.chat import memory, store
        from fashionops.llm import MockModelClient

        _, conversation_id = _seed_long_conversation(session)
        client = MockModelClient()
        first = memory.maybe_refresh(conversation_id, model_client=client)

        # 再加 3 条 → 总共掉出去 21..23 这 3 条，不足阈值（默认 10）
        for index in range(3):
            store.append_message(conversation_id, role=MSG_ROLE_USER, content=f"补充 {index}")

        client.calls.clear()
        session.expire_all()
        assert memory.maybe_refresh(conversation_id, model_client=client) == first
        assert client.calls == [], "不足阈值却重做了摘要"

    def test_refresh_happens_once_the_threshold_is_reached(self, session):  # noqa: ANN001
        from fashionops.chat import memory, store
        from fashionops.llm import MockModelClient

        _, conversation_id = _seed_long_conversation(session)
        client = MockModelClient()
        memory.maybe_refresh(conversation_id, model_client=client)

        for index in range(13):
            store.append_message(conversation_id, role=MSG_ROLE_USER, content=f"补充 {index}")

        client.calls.clear()
        memory.maybe_refresh(conversation_id, model_client=client)
        assert len(client.calls) == 1, "攒够阈值应该重做一次摘要"

        session.expire_all()
        conversation = session.get(Conversation, conversation_id)
        assert conversation is not None
        # 60 + 13 = 73 条，窗口 40 → 窗口从 seq 34 开始，摘要该覆盖到 33
        assert conversation.memory_summary_upto_seq == 33

    def test_upto_never_advances_past_what_was_compressed(self, session, monkeypatch):  # noqa: ANN001
        """一次塞不下时，``upto`` 必须停在**实际压缩到的那一条**上。

        设成窗口起点的话，那些没被压缩的消息就被标记成"已覆盖"了 ——
        它们会永久消失，而且不留任何痕迹。这是整个模块里最容易写错的一行。
        """
        from fashionops.chat import memory
        from fashionops.llm import MockModelClient

        _, conversation_id = _seed_long_conversation(session)
        # 把压缩器的输入预算压到很小，逼出"塞不下"的分支
        monkeypatch.setattr(memory, "_SUMMARY_INPUT_CHARS", 60)

        summary = memory.maybe_refresh(conversation_id, model_client=MockModelClient())
        assert summary

        session.expire_all()
        conversation = session.get(Conversation, conversation_id)
        assert conversation is not None
        upto = int(conversation.memory_summary_upto_seq or 0)
        assert 0 < upto < 20, f"upto={upto} 越过了实际压缩到的位置（窗口边界是 20）"

    def test_summarisation_failure_never_breaks_the_turn(self, session):  # noqa: ANN001
        """压缩是**锦上添花**，绝不能因为它把一轮对话搞挂。"""

        from fashionops.chat import memory

        class Boom:
            def complete(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202, ARG002
                raise RuntimeError("模型挂了")

        _, conversation_id = _seed_long_conversation(session)
        assert memory.maybe_refresh(conversation_id, model_client=Boom()) == ""

    def test_build_block_is_empty_without_summary(self):
        """没有摘要时不能往 system prompt 里塞一个空标题。"""
        from fashionops.chat import memory

        assert memory.build_block("") == ""
        assert memory.build_block("   ") == ""
        block = memory.build_block("用户只做抖音渠道")
        assert memory.SUMMARY_HEADING in block
        assert "用户只做抖音渠道" in block
        # 摘要可能和后面的话冲突，必须说清以谁为准
        assert "以后面说的为准" in block

    def test_summary_is_actually_injected_into_the_model_request(self, session):  # noqa: ANN001
        """**这一条是验收标准**：摘要必须真的出现在发给模型的 system 消息里。

        前面几条只证明"摘要被生成并存下来了"，而"存下来了但没喂给模型"
        完全可能 —— 那才是白干。所以直接查 MockModelClient 收到的请求。
        """
        from unittest.mock import patch

        from fashionops.chat import agent, memory
        from fashionops.llm import MockModelClient
        from fashionops.security.service import Principal

        conversation_no, conversation_id = _seed_long_conversation(session)
        client = MockModelClient()
        principal = Principal(user_id=1, username="z", is_superuser=True)

        session.expire_all()
        with patch.object(agent, "get_model_client", return_value=client):
            list(
                agent.stream_turn(
                    conversation_no=conversation_no, principal=principal, user_text="继续"
                )
            )

        session.expire_all()
        conversation = session.get(Conversation, conversation_id)
        assert conversation is not None and conversation.memory_summary

        system = client.calls[-1][0]
        assert system.role == "system"
        assert memory.SUMMARY_HEADING in system.content
        assert conversation.memory_summary in system.content


# ===========================================================================
# 8. 上下文预算的另外两处
# ===========================================================================
class TestContextBudget:
    """`to_llm_messages` 里两处"喂给模型的"和"库里存的"故意不一样的地方。"""

    def test_interrupted_answer_is_marked_for_the_model(self, session):  # noqa: ANN001
        """被打断的回答必须显式标注。

        库里留原文是事实记录，但原样喂回去模型会以为自己当时说完了 ——
        它可能据此往下推理，或者认为"这个问题已经答过"而不回答。
        """
        from fashionops.chat import store

        created = store.create_conversation(username="z", user_id=None)
        session.expire_all()
        conversation_id = conversation_id_of(session, created["conversation_no"])
        store.append_message(conversation_id, role=MSG_ROLE_USER, content="退货率多少")
        store.append_message(
            conversation_id,
            role=MSG_ROLE_ASSISTANT,
            content="退货率是 3.2%",
            interrupted=True,
            finish_reason="error",
        )

        messages = store.to_llm_messages(
            store.history(session, conversation_id), system_prompt="sys"
        )
        assistant = [m for m in messages if m.role == "assistant"][0]
        assert "退货率是 3.2%" in assistant.content
        assert "中断" in assistant.content, "被打断的回答没标注，模型会当成完整回答"

    def test_complete_answer_is_not_marked(self, session):  # noqa: ANN001
        """没被打断的不能被误标 —— 否则正常回答会被模型当成残的。"""
        from fashionops.chat import store

        created = store.create_conversation(username="z", user_id=None)
        session.expire_all()
        conversation_id = conversation_id_of(session, created["conversation_no"])
        store.append_message(conversation_id, role=MSG_ROLE_ASSISTANT, content="退货率是 3.2%")

        messages = store.to_llm_messages(
            store.history(session, conversation_id), system_prompt="sys"
        )
        assert messages[1].content == "退货率是 3.2%"

    def _seed_tool_result(self, session, *, filler: int, big: str = "数据" * 400):  # noqa: ANN001
        """造一条工具结果，后面跟 ``filler`` 条消息把它推远。"""
        from fashionops.chat import store

        created = store.create_conversation(username="z", user_id=None)
        session.expire_all()
        conversation_id = conversation_id_of(session, created["conversation_no"])
        store.append_message(conversation_id, role=MSG_ROLE_USER, content="查库存")
        store.append_message(
            conversation_id,
            role=MSG_ROLE_ASSISTANT,
            content="",
            tool_call_id="c1",
            tool_name="erp.query_inventory",
            tool_arguments={"sku_codes": ["A1"]},
            finish_reason="tool_calls",
        )
        store.append_message(
            conversation_id,
            role=MSG_ROLE_TOOL,
            content=big,
            tool_call_id="c1",
            tool_name="erp.query_inventory",
            tool_result={"ok": True},
            tool_ok=True,
        )
        for index in range(filler):
            store.append_message(conversation_id, role=MSG_ROLE_USER, content=f"补充 {index}")
        return conversation_id

    def test_old_tool_results_are_truncated(self, session):  # noqa: ANN001
        """离得远的工具结果要截断 —— 它每轮都会重发一次，而多数内容不再被需要。"""
        from fashionops.chat import store

        conversation_id = self._seed_tool_result(session, filler=20)
        messages = store.to_llm_messages(
            store.history(session, conversation_id),
            system_prompt="sys",
            old_tool_result_chars=50,
        )
        tool_message = [m for m in messages if m.role == "tool"][0]
        assert len(tool_message.content) < 200
        assert "已截断" in tool_message.content, "截断了却不说明，模型会把半截当完整数据"

    def test_recent_tool_results_are_kept_intact(self, session):  # noqa: ANN001
        """最近的工具结果必须保持完整 —— 模型真正在读的就是它。"""
        from fashionops.chat import store

        conversation_id = self._seed_tool_result(session, filler=2)
        big = "数据" * 400
        messages = store.to_llm_messages(
            store.history(session, conversation_id),
            system_prompt="sys",
            old_tool_result_chars=50,
        )
        tool_message = [m for m in messages if m.role == "tool"][0]
        assert tool_message.content == big, "最近的工具结果被截断了 —— 模型会看不到完整数据"

    def test_truncation_can_be_disabled(self, session):  # noqa: ANN001
        """`0` = 不截断。留这条是因为截断本身有信息损失，
        排查"模型说数据不全"这类问题时需要能一键关掉。"""
        from fashionops.chat import store

        conversation_id = self._seed_tool_result(session, filler=20)
        big = "数据" * 400
        messages = store.to_llm_messages(
            store.history(session, conversation_id),
            system_prompt="sys",
            old_tool_result_chars=0,
        )
        tool_message = [m for m in messages if m.role == "tool"][0]
        assert tool_message.content == big


# ===========================================================================
# 9. 跨会话长期记忆（见 chat/longterm.py）
# ===========================================================================
class TestLongTermMemory:
    """用户的长期偏好应该跨对话保留，而不是"新开对话就失忆"。

    这一层在 `docs/09` 里原本是 ❌（`messages` 字段有、没有记忆逻辑）。
    现在 Agent 在对话里用 `remember` 工具主动记下偏好，以后每轮注入 ——
    即使换了全新的会话。锁住的是"它真的**跨会话**生效"和"同一个 topic 是更新不是追加"。
    """

    @staticmethod
    def _make_user(session, username: str = "z") -> int:  # noqa: ANN001
        from fashionops.db.models_identity import USER_STATUS_ACTIVE, User

        user = User(username=username, full_name=username, status=USER_STATUS_ACTIVE)
        session.add(user)
        session.commit()
        return int(user.id)

    def test_remember_stores_a_preference(self, session):  # noqa: ANN001
        from fashionops.chat import longterm

        uid = self._make_user(session)
        result = longterm.remember_tool(
            user_id=uid,
            username="z",
            arguments={"topic": "渠道偏好", "content": "只看抖音渠道", "kind": "preference"},
        )
        assert result["ok"] is True
        assert result["data"]["action"] == "created"

        session.expire_all()
        items = longterm.list_for_user(uid)
        assert len(items) == 1
        assert items[0]["topic"] == "渠道偏好"
        assert items[0]["content"] == "只看抖音渠道"
        assert items[0]["kind"] == "preference"

    def test_same_topic_updates_instead_of_adding(self, session):  # noqa: ANN001
        """同一个 topic 再记一次是**更新**，不是又加一条。

        否则用户改主意时库里会同时有"只看抖音"和"淘宝也要看"两条，
        注入时两条都在，模型面对互相矛盾的记忆只会瞎猜。
        """
        from fashionops.chat import longterm

        uid = self._make_user(session)
        longterm.remember_tool(
            user_id=uid, username="z", arguments={"topic": "渠道偏好", "content": "只看抖音渠道"}
        )
        longterm.remember_tool(
            user_id=uid, username="z", arguments={"topic": "渠道偏好", "content": "抖音和淘宝都看"}
        )

        session.expire_all()
        items = longterm.list_for_user(uid)
        assert len(items) == 1, "同一个 topic 不应该出现两条"
        assert items[0]["content"] == "抖音和淘宝都看"
        assert items[0]["updated_at"] >= items[0]["created_at"]

    def test_forget_removes_it(self, session):  # noqa: ANN001
        from fashionops.chat import longterm

        uid = self._make_user(session)
        longterm.remember_tool(
            user_id=uid, username="z", arguments={"topic": "渠道偏好", "content": "只看抖音渠道"}
        )
        assert longterm.forget(user_id=uid, topic="渠道偏好") is True
        session.expire_all()
        assert longterm.list_for_user(uid) == []

    def test_build_block_marks_facts_as_stale(self):
        """`fact`（有时效的数字）必须标注记录时间，否则模型会把旧数字当今天的。"""
        from fashionops.chat import longterm

        block = longterm.build_block(
            [
                {"topic": "渠道偏好", "kind": "preference", "content": "只看抖音"},
                {
                    "topic": "退货率",
                    "kind": "fact",
                    "content": "9 月退货率 3.2%",
                    "updated_at": "2026-09-01T00:00:00",
                },
            ]
        )
        assert "渠道偏好" in block and "只看抖音" in block
        assert "9 月退货率 3.2%" in block
        assert "2026-09-01" in block and "可能已过期" in block

    def test_build_block_empty_without_memories(self):
        from fashionops.chat import longterm

        assert longterm.build_block([]) == ""

    def test_disabled_feature_rejects_remember(self, session, monkeypatch):  # noqa: ANN001
        """关掉开关后，`remember` 必须被拒绝 —— 而不是偷偷记下来。"""
        from fashionops.chat import longterm

        uid = self._make_user(session)
        monkeypatch.setattr(longterm, "enabled", lambda: False)
        result = longterm.remember_tool(
            user_id=uid, username="z", arguments={"topic": "渠道偏好", "content": "只看抖音"}
        )
        assert result["ok"] is False
        assert result["error"]["code"] == "MEMORY_DISABLED"

    def test_remember_rejects_empty_args(self, session):  # noqa: ANN001
        from fashionops.chat import longterm

        uid = self._make_user(session)
        result = longterm.remember_tool(
            user_id=uid, username="z", arguments={"topic": "", "content": "只看抖音"}
        )
        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_ARGUMENTS"

    def test_memory_is_injected_into_a_new_conversation(self, session):  # noqa: ANN001
        """**这条是跨会话层的验收标准**：在 A 会话里记住一件事，
        换个全新的 B 会话（同一用户），这条记忆必须出现在发给模型的 system 里。
        """
        from unittest.mock import patch

        from fashionops.chat import agent, longterm, store
        from fashionops.llm import MockModelClient, MockReply, ToolCall
        from fashionops.security.service import Principal

        uid = self._make_user(session)

        # ---- 会话 A：模型调用 remember ----
        conv_a = store.create_conversation(username="z", user_id=uid, title="A")
        no_a = conv_a["conversation_no"]
        client = MockModelClient(
            script=[
                MockReply(
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="remember",
                            arguments={"topic": "渠道偏好", "content": "只看抖音渠道"},
                        )
                    ]
                )
            ]
        )
        principal = Principal(user_id=uid, username="z", is_superuser=True)
        session.expire_all()
        with patch.object(agent, "get_model_client", return_value=client):
            list(
                agent.stream_turn(
                    conversation_no=no_a, principal=principal, user_text="记住：我只做抖音渠道"
                )
            )

        session.expire_all()
        assert longterm.list_for_user(uid), "A 会话里没记下记忆"

        # ---- 会话 B：全新会话，模型只回答不记 ----
        conv_b = store.create_conversation(username="z", user_id=uid, title="B")
        no_b = conv_b["conversation_no"]
        client2 = MockModelClient()  # 无脚本 → 默认回一句纯文本，不调工具
        session.expire_all()
        with patch.object(agent, "get_model_client", return_value=client2):
            list(
                agent.stream_turn(
                    conversation_no=no_b, principal=principal, user_text="今天退货率多少"
                )
            )

        system = client2.calls[-1][0]
        assert system.role == "system"
        assert "你记得的关于这位用户的事" in system.content
        assert "只看抖音渠道" in system.content


# ===========================================================================
# 10. 记忆接口的归属校验（见 api/routers/memory.py）
# ===========================================================================
class TestMemoryAPI:
    """记忆接口只操作**本人**名下，不能越权删别人的。"""

    @staticmethod
    def _make_user(session, username: str, *, superuser: bool = False) -> int:  # noqa: ANN001
        from fashionops.db.models_identity import USER_STATUS_ACTIVE, User

        user = User(
            username=username,
            full_name=username,
            status=USER_STATUS_ACTIVE,
            is_superuser=superuser,
        )
        session.add(user)
        session.commit()
        return int(user.id)

    def test_list_returns_only_mine(self, session):  # noqa: ANN001
        from fashionops.api.routers.memory import list_my_memories
        from fashionops.chat import longterm
        from fashionops.security.service import Principal

        uid_z = self._make_user(session, "z")
        uid_w = self._make_user(session, "w")
        longterm.remember_tool(
            user_id=uid_z, username="z", arguments={"topic": "渠道", "content": "抖音"}
        )
        longterm.remember_tool(
            user_id=uid_w, username="w", arguments={"topic": "渠道", "content": "淘宝"}
        )

        # 这里**直接调函数**（不走 FastAPI），所以查询参数要显式传 ——
        # 不传的话拿到的是 Query(...) 这个默认值对象而不是数字。
        items = list_my_memories(
            principal=Principal(user_id=uid_z, username="z"), session=session, limit=200
        )["items"]
        assert [i["content"] for i in items] == ["抖音"]

    def test_cannot_delete_someone_elses_memory(self, session):  # noqa: ANN001
        from fashionops.api.routers.memory import forget_memory
        from fashionops.chat import longterm
        from fashionops.security.service import Principal
        from fastapi import HTTPException

        uid_z = self._make_user(session, "z")
        uid_w = self._make_user(session, "w")
        longterm.remember_tool(
            user_id=uid_w, username="w", arguments={"topic": "渠道", "content": "淘宝"}
        )
        wid = longterm.list_for_user(uid_w)[0]["id"]

        # z 删 w 的 → 404（不能越权删别人的数据；memory id 可枚举，漏校验就是洞）
        try:
            forget_memory(
                memory_id=wid,
                principal=Principal(user_id=uid_z, username="z"),
                session=session,
            )
            raise AssertionError("越权删除竟然成功了")
        except HTTPException as e:
            assert e.status_code == 404

        # w 自己删 → 成功
        result = forget_memory(
            memory_id=wid,
            principal=Principal(user_id=uid_w, username="w"),
            session=session,
        )
        assert result["ok"] is True


# ===========================================================================
# 11. 会话归档（见 api/routers/chat.py 的 archive / unarchive）
# ===========================================================================
class TestConversationArchive:
    """`CONVERSATION_STATUS_ARCHIVED` 之前定义了却从没被写过 —— 会话只能增不能收。

    现在补上归档：从默认列表里收起但**不删数据**，随时能取消归档。
    """

    @staticmethod
    def _make_user(session, username: str = "z") -> int:  # noqa: ANN001
        from fashionops.db.models_identity import USER_STATUS_ACTIVE, User

        user = User(username=username, full_name=username, status=USER_STATUS_ACTIVE)
        session.add(user)
        session.commit()
        return int(user.id)

    @staticmethod
    def _conv(session, user_id: int, title: str = "c") -> str:  # noqa: ANN001
        return store.create_conversation(username="z", user_id=user_id, title=title)["conversation_no"]

    def test_store_excludes_archived_when_filtering(self, session):  # noqa: ANN001
        from fashionops.db.models_chat import (
            CONVERSATION_STATUS_ACTIVE,
            CONVERSATION_STATUS_ARCHIVED,
        )

        uid = self._make_user(session)
        no = self._conv(session, uid)
        cid = store.get_conversation(session, no).id
        # 默认（status=None）和按 active 过滤都能看到
        assert any(c.conversation_no == no for c in store.list_conversations(session, user_id=uid))
        assert any(
            c.conversation_no == no
            for c in store.list_conversations(session, user_id=uid, status=CONVERSATION_STATUS_ACTIVE)
        )
        # 归档后：按 active 看不到，按 archived 能看到
        store.set_conversation_status(cid, CONVERSATION_STATUS_ARCHIVED)
        assert not any(
            c.conversation_no == no
            for c in store.list_conversations(session, user_id=uid, status=CONVERSATION_STATUS_ACTIVE)
        )
        assert any(
            c.conversation_no == no
            for c in store.list_conversations(session, user_id=uid, status=CONVERSATION_STATUS_ARCHIVED)
        )
        # store 层不做"默认隐藏"——隐藏在 router 层；status=None 仍含全部
        assert any(c.conversation_no == no for c in store.list_conversations(session, user_id=uid))

    def test_router_list_defaults_to_active(self, session):  # noqa: ANN001
        from fashionops.api.routers.chat import list_conversations as list_endpoint
        from fashionops.db.models_chat import CONVERSATION_STATUS_ARCHIVED
        from fashionops.security.service import Principal

        uid = self._make_user(session)
        no = self._conv(session, uid)
        store.set_conversation_status(store.get_conversation(session, no).id, CONVERSATION_STATUS_ARCHIVED)
        principal = Principal(user_id=uid, username="z", is_superuser=True)

        # 同上：直接调函数，新加的 offset 要显式传
        result = list_endpoint(
            principal=principal, session=session, limit=50, offset=0, status=None
        )
        nos = [i["conversation_no"] for i in result["items"]]
        assert no not in nos

    def test_archive_endpoint_roundtrip(self, session):  # noqa: ANN001
        from fashionops.api.routers.chat import archive_conversation, unarchive_conversation
        from fashionops.db.models_chat import CONVERSATION_STATUS_ARCHIVED
        from fashionops.security.service import Principal

        uid = self._make_user(session)
        no = self._conv(session, uid)
        principal = Principal(user_id=uid, username="z", is_superuser=True)

        archived = archive_conversation(
            conversation_no=no, request=None, principal=principal, session=session
        )
        assert archived["ok"] is True
        assert archived["status"] == CONVERSATION_STATUS_ARCHIVED

        restored = unarchive_conversation(
            conversation_no=no, request=None, principal=principal, session=session
        )
        assert restored["status"] == "active"

    def test_cannot_archive_someone_elses_conversation(self, session):  # noqa: ANN001
        from fashionops.api.routers.chat import archive_conversation
        from fashionops.security.service import Principal
        from fastapi import HTTPException

        uid_z = self._make_user(session, "z")
        uid_w = self._make_user(session, "w")
        no = self._conv(session, uid_z)  # 属于用户 z
        # w 来归档 → 404（不能确认"这个编号存在"，防枚举）
        try:
            archive_conversation(
                conversation_no=no,
                request=None,
                principal=Principal(user_id=uid_w, username="w"),
                session=session,
            )
            raise AssertionError("归档了别人的会话竟然成功了")
        except HTTPException as e:
            assert e.status_code == 404


# ===========================================================================
# 11.5 会话历史分页（见 api/routers/chat.py 的 get_conversation + store.history）
# ===========================================================================
class TestConversationHistoryPaging:
    """会话详情只回最近 N 条 + `before_seq` 向上翻页。

    以前是**全量返回**：长会话每打开一次、每回合结束都把所有消息
    （含工具返回的大段 JSON）拉一遍。这里锁住两条不变量：

    1. `store.history` 的 `limit` 是"取最后 N 条"，`before_seq` 是"在这个序号之前"
    2. `get_conversation` 的 `has_more` 必须真实 —— 靠"多取一条"判断，不是靠猜
    """

    @staticmethod
    def _seed(session, n: int = 5):  # noqa: ANN001, ANN205
        from fashionops.chat import store

        created = store.create_conversation(username="z", user_id=None, title="c")
        cid = conversation_id_of(session, created["conversation_no"])
        for ch in ("a", "b", "c", "d", "e")[:n]:
            store.append_message(cid, role=MSG_ROLE_USER, content=ch)
        session.commit()
        return created["conversation_no"], cid

    def test_store_history_returns_last_n(self, session):
        _, cid = self._seed(session)
        session.expire_all()
        assert [m.content for m in store.history(session, cid, limit=2)] == ["d", "e"]

    def test_store_history_before_seq_pages_backward(self, session):
        _, cid = self._seed(session)
        session.expire_all()
        # before_seq=4 → 只取 seq<4 的消息，再取最后 2 条 → b, c
        rows = store.history(session, cid, limit=2, before_seq=4)
        assert [m.content for m in rows] == ["b", "c"]

    def test_endpoint_paginates_with_has_more(self, session):
        from fashionops.api.routers.chat import get_conversation
        from fashionops.security.service import Principal

        no, _ = self._seed(session)
        principal = Principal(user_id=None, username="z")

        # 直接调函数（不走 FastAPI），所以 Query() 默认值不会自动解析成实际值，
        # 每个查询参数都得显式传 —— 否则 before_seq 会带着一个 Query 对象进 SQL 报错。
        first = get_conversation(
            conversation_no=no,
            principal=principal,
            session=session,
            limit=2,
            before_seq=None,
        )
        assert [m["content"] for m in first["messages"]] == ["d", "e"]
        assert first["has_more"] is True

        # 用本页最旧一条的 seq 往上翻
        oldest_seq = first["messages"][0]["seq"]
        second = get_conversation(
            conversation_no=no,
            principal=principal,
            session=session,
            limit=2,
            before_seq=oldest_seq,
        )
        assert [m["content"] for m in second["messages"]] == ["b", "c"]
        assert second["has_more"] is True

        # 翻到底 → 只剩 a，has_more 必须变 False（不能一直"还有更多"）
        bottom = get_conversation(
            conversation_no=no,
            principal=principal,
            session=session,
            limit=2,
            before_seq=second["messages"][0]["seq"],
        )
        assert [m["content"] for m in bottom["messages"]] == ["a"]
        assert bottom["has_more"] is False


# ===========================================================================
# 12. 消息序号的并发安全（见 chat/store.py:_next_seq）
# ===========================================================================
class TestMessageSeq:
    """会话内序号用锁行 + `last_seq` 生成，不再靠 `MAX(seq)+1` 撞唯一约束。"""

    def test_seq_is_monotonic_and_last_seq_is_maintained(self, session):  # noqa: ANN001
        from fashionops.chat import store
        from fashionops.db.models_chat import Conversation

        # user_id=None：测试库没有 users 行且强外键，挂真实 id 会撞；这列本身就是可空的。
        no = store.create_conversation(username="z", user_id=None, title="seq")["conversation_no"]
        cid = store.get_conversation(session, no).id

        seqs = [store.append_message(cid, role=r, content=r)["seq"] for r in ("user", "assistant", "user")]
        assert seqs == [1, 2, 3]

        session.expire_all()
        assert session.get(Conversation, cid).last_seq == 3
