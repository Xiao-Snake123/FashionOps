"""流式输出测试。

## 为什么必须有这个文件

真模型（glm-4-flash）在"要不要调工具"这件事上**不是确定性的**：
同一个提示词，有时发起工具调用、有时直接反问用户。
所以分片拼装的正确性**不能只靠真模型验证** —— 那验证的是"这次运气好"。

这里用**构造的假分片**把 OpenAI 兼容接口的线格式固定下来：
分片按 ``index`` 交错到达、``arguments`` 是逐段拼的 JSON 字符串。
这是唯一能确定性地覆盖那段拼接逻辑的办法。

## 覆盖的重点

1. **按 index 分组**：不分组的话两个工具的参数会拼到一起，
   而报错现场是"JSON 解析失败"，很容易误判成"模型输出不稳定"
2. **usage 的降级**：厂商给了就用，没给就用估算 —— 而且**绝不传**
   ``stream_options.include_usage``（那个参数不是每家都支持，传了可能整条流 400）
3. **必须有 done 分片**：缺了要报错，不能静默返回空内容
4. **暂停不执行工具**：需要人工确认时，那个工具**一次都不能被调用**
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from fashionops.llm import (
    Message,
    MockModelClient,
    MockReply,
    ModelError,
    StreamChunkKind,
    ToolCall,
    ToolLoopPaused,
    run_tool_loop,
)
from fashionops.llm.openai_compat import OpenAICompatClient


# ===========================================================================
# 构造"像真的一样的"流式分片
# ===========================================================================
def _content(text: str, *, finish: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish,
                delta=SimpleNamespace(content=text, tool_calls=None),
            )
        ],
        usage=None,
    )


def _tool_fragment(
    index: int,
    *,
    call_id: str | None = None,
    name: str | None = None,
    args: str | None = None,
    finish: str | None = None,
) -> SimpleNamespace:
    """一个 tool_calls 分片。

    真实线格式就是长这样：第一片带 ``id`` 和 ``name``，
    后面几片只带 ``arguments`` 的一段。
    """
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish,
                delta=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            index=index,
                            id=call_id,
                            function=SimpleNamespace(name=name, arguments=args),
                        )
                    ],
                ),
            )
        ],
        usage=None,
    )


def _usage_only(prompt: int, completion: int) -> SimpleNamespace:
    total = prompt + completion
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total),
    )


class _FakeCompletions:
    def __init__(self, chunks: list[SimpleNamespace]) -> None:
        self._chunks = chunks
        self.payloads: list[dict] = []

    def create(self, **payload):  # noqa: ANN003, ANN201
        self.payloads.append(payload)
        return iter(self._chunks)


class _FakeOpenAI:
    """只实现 ``client.chat.completions.create`` —— 用到什么就假装什么。"""

    def __init__(self, chunks: list[SimpleNamespace]) -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletions(chunks))

    @property
    def payloads(self) -> list[dict]:
        return self.chat.completions.payloads


def _client(chunks: list[SimpleNamespace]) -> tuple[OpenAICompatClient, _FakeOpenAI]:
    client = OpenAICompatClient(api_key="test-key", model="test-model")
    fake = _FakeOpenAI(chunks)
    client._client = fake  # noqa: SLF001 - 测试里替掉底层 SDK 是刻意的
    return client, fake


def _collect(client: OpenAICompatClient, **kwargs):  # noqa: ANN202
    return list(client.stream([Message.user("hi")], **kwargs))


# ===========================================================================
# 分片拼装
# ===========================================================================
class TestToolCallAssembly:
    def test_fragments_across_chunks_become_one_call(self):
        """参数的 JSON 字符串被切成好几片，必须拼回一个完整的对象。"""
        chunks = [
            _tool_fragment(0, call_id="call_1", name="erp__query_inventory", args='{"sku'),
            _tool_fragment(0, args='_codes": ["SK'),
            _tool_fragment(0, args='U00042"]}', finish="tool_calls"),
        ]
        client, _ = _client(chunks)
        out = _collect(client)

        done = out[-1]
        assert done.kind is StreamChunkKind.DONE
        assert len(done.response.tool_calls) == 1
        call = done.response.tool_calls[0]
        assert call.id == "call_1"
        assert call.name == "erp__query_inventory"
        assert call.arguments == {"sku_codes": ["SKU00042"]}

    def test_two_interleaved_tool_calls_are_not_mixed_up(self):
        """一轮里同时调两个工具时，分片是**交错**到达的。

        不按 index 分组的话，两个工具的参数会被拼到一起 ——
        而报错现场是"JSON 解析失败"，很容易误判成"模型输出不稳定"。
        """
        chunks = [
            _tool_fragment(0, call_id="c0", name="erp__query_inventory", args='{"sku'),
            _tool_fragment(1, call_id="c1", name="kb__search", args='{"q'),
            _tool_fragment(0, args='_codes": ["A1"]}'),
            _tool_fragment(1, args='uery": "退货"}', finish="tool_calls"),
        ]
        client, _ = _client(chunks)
        done = _collect(client)[-1]

        calls = {c.name: c.arguments for c in done.response.tool_calls}
        assert calls == {
            "erp__query_inventory": {"sku_codes": ["A1"]},
            "kb__search": {"query": "退货"},
        }

    def test_invalid_arguments_json_keeps_raw_text(self):
        """参数不是合法 JSON 时不能丢 —— 原文要留着，否则没法排查是模型给错了什么。"""
        chunks = [
            _tool_fragment(0, call_id="c0", name="erp__query", args="{not json", finish="tool_calls"),
        ]
        client, _ = _client(chunks)
        done = _collect(client)[-1]

        call = done.response.tool_calls[0]
        assert call.arguments == {}
        assert call.raw_arguments == "{not json"

    def test_tool_call_chunk_is_emitted_before_done(self):
        """先发 tool_call 分片（前端可以显示"正在查询…"），再发 done。"""
        chunks = [
            _tool_fragment(0, call_id="c0", name="erp__x", args="{}", finish="tool_calls"),
        ]
        client, _ = _client(chunks)
        out = _collect(client)

        kinds = [c.kind for c in out]
        assert kinds == [StreamChunkKind.TOOL_CALL, StreamChunkKind.DONE]
        assert out[0].tool_call.name == "erp__x"


# ===========================================================================
# 内容与 usage
# ===========================================================================
class TestContentAndUsage:
    def test_content_chunks_concatenate_to_full_content(self):
        chunks = [_content("服装"), _content("电商"), _content("退货率高。", finish="stop")]
        client, _ = _client(chunks)
        out = _collect(client)

        text = "".join(c.text for c in out if c.kind is StreamChunkKind.CONTENT)
        assert text == "服装电商退货率高。"
        assert out[-1].response.content == text

    def test_provider_usage_is_preferred_when_present(self):
        chunks = [_content("好", finish="stop"), _usage_only(123, 45)]
        client, _ = _client(chunks)
        done = _collect(client)[-1]

        assert done.response.usage.prompt_tokens == 123
        assert done.response.usage.completion_tokens == 45
        assert done.response.usage.total_tokens == 168

    def test_usage_is_estimated_when_provider_omits_it(self):
        """厂商不给 usage 时要估一个量级正确的数字，而不是留 0。

        留 0 的话成本统计整天是 0，看起来"省钱"，实际是**统计失效**。
        """
        chunks = [_content("这是一个足够长的中文回答，用来让 token 估算不是零。", finish="stop")]
        client, _ = _client(chunks)
        done = _collect(client)[-1]

        assert done.response.usage.prompt_tokens > 0
        assert done.response.usage.completion_tokens > 0
        assert done.response.usage.total_tokens > 0

    def test_request_never_sends_stream_options(self):
        """**刻意不传** ``stream_options.include_usage``。

        它不是所有 OpenAI 兼容厂商都支持：在支持的厂商上是锦上添花，
        在不支持的厂商上会直接 400 掉整个请求。
        为了让成本统计更精确而赌上整条对话能不能用，这笔账不划算。

        这条断言就是防止将来有人"顺手优化"把它加上。
        """
        client, fake = _client([_content("x", finish="stop")])
        _collect(client)

        payload = fake.payloads[0]
        assert payload["stream"] is True
        assert "stream_options" not in payload

    def test_stream_is_requested_with_tools(self):
        client, fake = _client([_content("x", finish="stop")])
        tools = [{"type": "function", "function": {"name": "t", "description": "", "parameters": {}}}]
        _collect(client, tools=tools)

        payload = fake.payloads[0]
        assert payload["tools"] == tools
        assert payload["tool_choice"] == "auto"


# ===========================================================================
# 契约：必须有 done
# ===========================================================================
class _NoDoneClient:
    """违反契约的客户端：只吐 content，不吐 done。"""

    def complete(self, messages, **kwargs):  # noqa: ANN001, ANN003, ANN201
        raise AssertionError("不应该走整块路径")

    def stream(self, messages, **kwargs):  # noqa: ANN001, ANN003, ANN201
        from fashionops.llm.types import StreamChunk

        yield StreamChunk(kind=StreamChunkKind.CONTENT, text="半句话")


class TestDoneContract:
    def test_missing_done_raises_instead_of_returning_empty(self):
        """缺 done 分片必须报错。

        静默返回空内容的话，上层会把空字符串当成模型的回答写进历史 ——
        用户看到一条空消息、日志里一条报错都没有。这是最难查的一类问题。
        """
        with pytest.raises(ModelError, match="done"):
            run_tool_loop(
                _NoDoneClient(),
                [Message.user("hi")],
                tools=[],
                tool_runner=lambda n, a: {"ok": True},
                stream=True,
            )


# ===========================================================================
# mock 的两条路径必须一致
# ===========================================================================
class TestMockStreamParity:
    def test_mock_stream_content_equals_mock_complete_content(self):
        """mock 的流式只是把同一份内容切片，**不能是另一套生成逻辑**。

        否则测试里验证的是 A、真模型跑的是 B，mock 就从"提前发现问题"
        变成了"掩盖问题"。
        """
        messages = [Message.user("你好")]
        whole = MockModelClient(latency_ms=0).complete(messages)
        out = list(MockModelClient(latency_ms=0).stream(messages))

        text = "".join(c.text for c in out if c.kind is StreamChunkKind.CONTENT)
        assert text == whole.content
        assert out[-1].response.content == whole.content

    def test_mock_stream_splits_into_multiple_pieces(self):
        """必须真的切成多片，否则前端的打字机效果测不到。"""
        messages = [Message.user("你好")]
        out = list(MockModelClient(latency_ms=0).stream(messages))
        pieces = [c for c in out if c.kind is StreamChunkKind.CONTENT]
        assert len(pieces) > 1

    def test_mock_stream_emits_tool_calls(self):
        client = MockModelClient(
            latency_ms=0,
            script=[
                MockReply(tool_calls=[ToolCall(id="c1", name="erp__query_inventory")]),
                MockReply(content="库存 128 件"),
            ],
        )
        out = list(client.stream([Message.user("查库存")], tools=[{"type": "function"}]))
        tool_chunks = [c for c in out if c.kind is StreamChunkKind.TOOL_CALL]
        assert len(tool_chunks) == 1
        assert tool_chunks[0].tool_call.name == "erp__query_inventory"


# ===========================================================================
# 工具循环：流式 / 非流式一致；暂停不执行
# ===========================================================================
def _tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {"name": "erp__query_inventory", "description": "查库存", "parameters": {}},
        }
    ]


class TestStreamToolLoop:
    def test_stream_and_whole_have_identical_control_flow(self):
        logs = [
            MockReply(tool_calls=[ToolCall(id="c1", name="erp__query_inventory")]),
            MockReply(content="库存充足"),
        ]
        whole = run_tool_loop(
            MockModelClient(latency_ms=0, script=list(logs)),
            [Message.user("查库存")],
            tools=_tools(),
            tool_runner=lambda n, a: {"ok": True},
        )

        tokens: list[str] = []
        streamed = run_tool_loop(
            MockModelClient(latency_ms=0, script=list(logs)),
            [Message.user("查库存")],
            tools=_tools(),
            tool_runner=lambda n, a: {"ok": True},
            stream=True,
            on_token=tokens.append,
        )

        assert streamed.steps == whole.steps
        assert streamed.finished == whole.finished
        assert streamed.content == whole.content
        assert len(streamed.tool_calls) == len(whole.tool_calls)
        # 流式下 on_token 必须真的收到东西，否则前端会一片空白
        assert "".join(tokens) == whole.content

    def test_on_token_also_fires_in_whole_mode(self):
        """非流式也要回调一次 —— 否则"把 stream 开关关掉"会让前端不显示内容。"""
        tokens: list[str] = []
        run_tool_loop(
            MockModelClient(latency_ms=0, script=[MockReply(content="完整回答")]),
            [Message.user("hi")],
            tools=_tools(),
            tool_runner=lambda n, a: {"ok": True},
            stream=False,
            on_token=tokens.append,
        )
        assert tokens == ["完整回答"]


class TestToolLoopPause:
    def _pausing_runner(self, seen: list[str]):
        def runner(name, args):  # noqa: ANN001, ANN202
            seen.append(name)
            raise ToolLoopPaused("approval", {"preview": "要发邮件给 ops@example.com"})

        return runner

    def test_paused_returns_pending_call(self):
        result = run_tool_loop(
            MockModelClient(
                latency_ms=0,
                script=[MockReply(tool_calls=[ToolCall(id="c1", name="mail__send")])],
            ),
            [Message.user("发邮件")],
            tools=_tools(),
            tool_runner=self._pausing_runner([]),
        )

        assert result.paused is True
        assert result.paused_reason == "approval"
        assert result.pending_tool_call is not None
        assert result.pending_tool_call.name == "mail__send"
        assert result.paused_payload["preview"].startswith("要发邮件")

    def test_paused_tool_is_never_executed(self):
        """挂起的那次调用**一次都不能被执行**。

        记进 executed 就会让审计说"调用过这个工具"—— 而它没被调用。
        对一个有写能力的工具来说，这个区别就是事故与没事故的区别。
        """
        seen: list[str] = []
        result = run_tool_loop(
            MockModelClient(
                latency_ms=0,
                script=[MockReply(tool_calls=[ToolCall(id="c1", name="mail__send")])],
            ),
            [Message.user("发邮件")],
            tools=_tools(),
            tool_runner=self._pausing_runner(seen),
        )

        assert seen == ["mail__send"]  # runner 被调用了（要拿参数给用户看）
        assert result.tool_calls == []  # 但**没有**被记为已执行

    def test_paused_payload_is_not_fed_back_to_model(self):
        """挂起不是"工具失败"，不能把 "pending_approval" 喂回模型。

        喂回去的话模型会看到"失败"并想办法绕过去 —— 换参数重试、换个工具，
        或者跟用户解释这个操作失败了。而实际上什么都没失败，只是在等人确认。
        """
        client = MockModelClient(
            latency_ms=0,
            script=[MockReply(tool_calls=[ToolCall(id="c1", name="mail__send")])],
        )
        run_tool_loop(
            client,
            [Message.user("发邮件")],
            tools=_tools(),
            tool_runner=self._pausing_runner([]),
        )

        # 只发生了一轮：模型没有被第二次调用
        assert len(client.calls) == 1
        # 而且没有任何 tool 角色的消息被写进上下文
        assert all(m.role != "tool" for m in client.calls[0])

    def test_paused_result_is_persistable_via_messages(self):
        """挂起时 ``messages`` 里必须已经带上那条 assistant(tool_calls)。

        恢复时要靠它把"模型当时想调什么"还原出来（以及给 tool 结果消息
        一个能对上的 ``tool_call_id``）。没有它就只能靠猜。
        """
        result = run_tool_loop(
            MockModelClient(
                latency_ms=0,
                script=[MockReply(tool_calls=[ToolCall(id="c1", name="mail__send")])],
            ),
            [Message.user("发邮件")],
            tools=_tools(),
            tool_runner=self._pausing_runner([]),
        )

        assistant = [m for m in result.messages if m.role == "assistant"]
        assert assistant and assistant[-1].tool_calls
        assert assistant[-1].tool_calls[0].id == "c1"


# ===========================================================================
# 流式失败的语义
# ===========================================================================
class _FailingMidStream:
    """吐了两个字之后断流。"""

    def complete(self, messages, **kwargs):  # noqa: ANN001, ANN003, ANN201
        raise AssertionError("不应该走整块路径")

    def stream(self, messages, **kwargs):  # noqa: ANN001, ANN003, ANN201
        from fashionops.llm.types import StreamChunk

        yield StreamChunk(kind=StreamChunkKind.CONTENT, text="已经")
        yield StreamChunk(kind=StreamChunkKind.CONTENT, text="说了")
        raise ModelError("连接中断")


class TestMidStreamFailure:
    def test_exception_propagates_after_partial_content(self):
        """中途断流要把异常抛出去 —— 上层需要知道"这次是失败的"，
        才能把这条消息标成不完整，而不是当成完整回答继续往下接话。
        """
        seen: list[str] = []
        with pytest.raises(ModelError, match="中断"):
            run_tool_loop(
                _FailingMidStream(),
                [Message.user("hi")],
                tools=[],
                tool_runner=lambda n, a: {"ok": True},
                stream=True,
                on_token=seen.append,
            )
        # 已经收到的内容仍然回调过（前端能把它显示出来）
        assert "".join(seen) == "已经说了"

    def test_raw_arguments_round_trip(self):
        """``raw_arguments`` 只在解析失败时有值 —— 解析成功就是 None，不重复存。"""
        chunks = [
            _tool_fragment(0, call_id="c", name="t", args='{"a": 1}', finish="tool_calls"),
        ]
        client, _ = _client(chunks)
        call = _collect(client)[-1].response.tool_calls[0]
        assert call.arguments == {"a": 1}
        assert call.raw_arguments is None
        assert json.loads('{"a": 1}') == call.arguments
