"""DeerFlow 事件翻译层测试（``orchestration/translator.py``）。

## 这个文件在守什么

这一层把 DeerFlow 的 SSE 文本行转成 ``(事件名, data)``。它有一个**很难自查的
失效模式**：一条 SSE 事件是跨三行的 ——

    event: messages
    data: [...]
    <空行>              ← 空行才是"这条事件结束"的标志

如果有人把解析写成"每读一行就新建一个解析器"，事件名和 data 会被拆到两次调用里：

* ``event: messages`` 这一行会在读到行尾时单独吐出来（名字对、data 为空）
* 紧接着的 ``data: [...]`` 变成一条默认名为 ``message`` 的新事件

后果是上层永远匹配不到 ``messages`` / ``values`` / ``end``，**一个 token 都推不出去，
而且不抛任何异常** —— 表现只是"连上了，但什么都没发生"。

这种 bug 靠读代码很难发现，所以这里用真实形状的报文把它钉死。
"""

from __future__ import annotations

from fashionops.orchestration.translator import (
    _SSEDecoder,
    iter_sse,
    message_role,
    parse_messages,
    parse_values,
    safe_json,
    split_sse,
)


# ---------------------------------------------------------------------------
# 1. ★ 事件名不能丢
# ---------------------------------------------------------------------------
class TestEventNameSurvives:
    def test_realistic_stream_keeps_event_names(self):
        """真实形状：三种事件 + 心跳，名字和数据都要对。"""
        lines = [
            ": ping",  # 心跳，不是数据
            "event: metadata",
            'data: {"run_id": "r1"}',
            "",
            "event: messages",
            'data: [{"type": "AIMessageChunk", "content": "你好"}, {"langgraph_node": "agent"}]',
            "",
            "event: end",
            'data: {"status": "ok"}',
            "",
        ]

        events = list(split_sse(lines))

        assert events == [
            ("metadata", '{"run_id": "r1"}'),
            ("messages", '[{"type": "AIMessageChunk", "content": "你好"}, {"langgraph_node": "agent"}]'),
            ("end", '{"status": "ok"}'),
        ]

    def test_per_line_parsing_loses_the_event_name(self):
        """**反面教材**：逐行调用 ``split_sse([line])`` 会丢掉事件名。

        这是真踩过的坑 —— orchestrator 原来写的是::

            for ev_name, ev_data in split_sse([line]):

        于是 ``event: messages`` 单独成了一条（data 为空），真正的 payload
        又变成名叫 ``message`` 的另一条。上层判断 ``if ev_name == "messages"``
        永远不成立，一个 token 都推不出去，还不报错。

        谁要是把 orchestrator 改回逐行解析，这条测试会红。
        """
        lines = ["event: messages", "data: [1, 2]", ""]

        per_line = [ev for line in lines for ev in split_sse([line])]
        assert per_line == [
            ("messages", ""),  # 名字对了，但 data 丢了
            ("message", "[1, 2]"),  # 数据对了，但名字丢了
        ]

        # 正确做法：整个行流交给同一个解析器
        assert list(split_sse(lines)) == [("messages", "[1, 2]")]

    def test_decoder_is_reusable_incrementally(self):
        """状态机可以一行一行喂 —— 这正是异步版能复用它实现的原因。"""
        decoder = _SSEDecoder()
        assert decoder.feed("event: values") is None
        assert decoder.feed('data: {"messages": []}') is None
        assert decoder.feed("") == ("values", '{"messages": []}')
        # 吐完之后状态清空，可以接着解析下一条
        assert decoder.feed("") is None
        assert decoder.feed("event: end") is None
        assert decoder.feed("") == ("end", "")


# ---------------------------------------------------------------------------
# 2. SSE 规范细节
# ---------------------------------------------------------------------------
class TestSseSpecDetails:
    def test_blank_line_is_the_boundary(self):
        """没有空行就不算结束 —— 不能在第一行 data 就急着吐。"""
        decoder = _SSEDecoder()
        decoder.feed("event: messages")
        assert decoder.feed("data: part-1") is None, "data 还没读完就吐了"
        assert decoder.feed("data: part-2") is None
        assert decoder.feed("") == ("messages", "part-1\npart-2")

    def test_multiline_data_is_joined_with_newline(self):
        """多行 data 按 SSE 规范用 \\n 拼回来（DeerFlow 的大 JSON 会这么发）。"""
        events = list(split_sse(["event: values", "data: {", 'data:   "a": 1', "data: }", ""]))
        assert events == [("values", '{\n"a": 1\n}')]

    def test_heartbeat_comments_are_dropped(self):
        """``:`` 开头的是心跳/保活，不能当数据。"""
        assert list(split_sse([": ping", ": keep-alive", ""])) == []

    def test_event_without_name_defaults_to_message(self):
        """SSE 规范：没有 event 字段时事件名是 ``message``。"""
        assert list(split_sse(["data: hello", ""])) == [("message", "hello")]

    def test_trailing_event_without_blank_line_is_flushed(self):
        """对端没补最后的空行时，收尾也要把事件吐出来，不能吞掉。"""
        assert list(split_sse(["event: end", "data: {}"])) == [("end", "{}")]

    def test_empty_input_yields_nothing(self):
        assert list(split_sse([])) == []

    async def test_iter_sse_matches_the_sync_version(self):
        """异步版和同步版必须给出**完全一样**的结果（共用同一个状态机）。"""
        lines = [
            ": ping",
            "event: messages",
            'data: [{"content": "hi"}]',
            "",
            "event: end",
            "data: {}",
            "",
        ]

        async def source():
            for line in lines:
                yield line

        assert [ev async for ev in iter_sse(source())] == list(split_sse(lines))


# ---------------------------------------------------------------------------
# 3. 载荷解析
# ---------------------------------------------------------------------------
class TestPayloadParsing:
    def test_parse_messages_unwraps_message_and_metadata(self):
        """``messages`` 事件是 ``[message, metadata]``，只取 message。"""
        msg = parse_messages('[{"content": "hi", "type": "ai"}, {"langgraph_node": "agent"}]')
        assert msg == {"content": "hi", "type": "ai"}

    def test_parse_messages_tolerates_garbage(self):
        assert parse_messages("不是 JSON") is None
        assert parse_messages("[]") is None
        assert parse_messages("[1, 2]") is None

    def test_parse_values_returns_snapshot_messages(self):
        msgs = parse_values('{"messages": [{"role": "user"}, {"role": "ai"}], "other": 1}')
        assert msgs == [{"role": "user"}, {"role": "ai"}]

    def test_parse_values_tolerates_garbage(self):
        assert parse_values("不是 JSON") == []
        assert parse_values('{"no_messages": 1}') == []

    def test_message_role_normalizes_langchain_types(self):
        """LangChain 用 ``type: human/ai/tool``，平台统一成 ``role: user/...``。"""
        assert message_role({"role": "assistant"}) == "assistant"
        assert message_role({"type": "human"}) == "user"
        assert message_role({"type": "ai"}) == "assistant"
        assert message_role({"type": "tool"}) == "tool"
        # 认不出来就原样返回，不要瞎猜成 user
        assert message_role({"type": "unknown_kind"}) == "unknown_kind"

    def test_safe_json_falls_back_instead_of_raising(self):
        assert safe_json('{"a": 1}', {}) == {"a": 1}
        assert safe_json("坏了", {}) == {}
        assert safe_json(None, {"d": True}) == {"d": True}
        # 合法 JSON 但不是对象：工具结果契约是 dict，标量不能当结果
        assert safe_json("[1, 2]", {}) == {}
