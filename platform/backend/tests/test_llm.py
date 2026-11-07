"""模型层测试。

重点验证三件事：

1. **mock 生成的假数据真的能过 schema 校验** —— 这是"零 Key 跑通全链路"的前提。
   如果 mock 造的数据过不了校验，整个开发环境就是假的可用。
2. **结构化输出的修复循环真的在修** —— 要能观察到"回喂错误 → 模型改对"这个过程，
   而不是碰巧第一次就成功。
3. **成本计算精确到 6 位小数** —— 单次调用可能只花 0.000123 元，
   四舍五入到分就全变 0 了。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

import pytest
from pydantic import BaseModel, Field

from fashionops.llm import (
    Message,
    MockModelClient,
    MockReply,
    ModelError,
    ModelRouter,
    ToolCall,
    build_instance,
    estimate_cost,
    extract_json,
    get_model_client,
    price_of,
    run_tool_loop,
    structured_call,
)

# ===========================================================================
# 测试用 schema
# ===========================================================================


class Risk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PricePoint(BaseModel):
    day: date
    price: Decimal


class Candidate(BaseModel):
    """嵌套 + 多种类型，用来压 mock 的生成器。"""

    sku_code: str
    title: str
    score: float
    monthly_sales: int
    in_stock: bool
    risk: Risk
    action: Literal["promote", "hold", "drop"]
    tags: list[str] = Field(default_factory=list)
    history: list[PricePoint] = Field(default_factory=list)
    extra: dict[str, int] = Field(default_factory=dict)
    optional_note: str | None = None
    created_at: datetime | None = None


class SelectionResult(BaseModel):
    summary: str
    candidates: list[Candidate]
    total_cost: Decimal


class TreeNode(BaseModel):
    """自引用模型。

    必须定义在**模块级**：类体里的 ``child: TreeNode | None`` 是前向引用，
    Pydantic 重建模型时需要能在模块命名空间里找到 ``TreeNode``。
    定义在函数内部的话 ``model_rebuild()`` 会报 PydanticUndefinedAnnotation。
    """

    name: str
    child: TreeNode | None = None


# ===========================================================================
# 价格与成本
# ===========================================================================
class TestPricing:
    def test_known_model_price(self):
        assert price_of("deepseek-chat") == (Decimal("1.0"), Decimal("2.0"))

    def test_versioned_name_falls_back_to_base_price(self):
        """``deepseek-chat-0324`` 这类带版本后缀的名字要能对上主版本价格，
        否则每出一个新版本就得改一次价格表。"""
        assert price_of("deepseek-chat-0324") == price_of("deepseek-chat")

    def test_unknown_model_gets_conservative_fallback(self):
        assert price_of("some-unknown-model") == (Decimal("1.0"), Decimal("2.0"))

    def test_mock_model_is_free(self):
        assert estimate_cost("mock-model", 100_000, 50_000) == Decimal("0")

    def test_cost_keeps_six_decimals(self):
        """单次调用可能只花 0.000123 元，精度不够就全变 0 了。"""
        cost = estimate_cost("deepseek-chat", 123, 45)
        assert cost > 0
        assert cost == Decimal("0.000213")

    def test_cost_scales_linearly(self):
        one = estimate_cost("deepseek-chat", 1000, 1000)
        ten = estimate_cost("deepseek-chat", 10_000, 10_000)
        assert ten == one * 10


# ===========================================================================
# JSON 提取
# ===========================================================================
class TestExtractJson:
    def test_plain_json(self):
        assert extract_json('{"a": 1}') == '{"a": 1}'

    def test_markdown_fence(self):
        assert extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_bare_fence(self):
        assert extract_json('```\n{"a": 1}\n```') == '{"a": 1}'

    def test_wrapped_in_prose(self):
        """模型很爱先来一句"好的，结果如下："。"""
        assert extract_json('好的，结果如下：\n{"a": 1}\n希望有帮助！') == '{"a": 1}'

    def test_multiline_json(self):
        text = '```json\n{\n  "a": 1,\n  "b": [1, 2]\n}\n```'
        assert json.loads(extract_json(text)) == {"a": 1, "b": [1, 2]}

    def test_empty_input(self):
        assert extract_json("") == ""


# ===========================================================================
# Mock 生成器
# ===========================================================================
class TestMockGeneration:
    def test_generated_instance_passes_validation(self):
        """核心断言：造出来的数据必须能真的通过 schema 校验。"""
        instance = build_instance(SelectionResult)
        assert isinstance(instance, SelectionResult)
        assert len(instance.candidates) >= 2

    def test_nested_model_is_populated(self):
        instance = build_instance(SelectionResult)
        candidate = instance.candidates[0]
        assert candidate.history, "嵌套列表没生成数据"
        assert isinstance(candidate.history[0], PricePoint)
        assert isinstance(candidate.history[0].price, Decimal)

    def test_enum_and_literal_only_use_valid_members(self):
        instance = build_instance(SelectionResult)
        for candidate in instance.candidates:
            assert candidate.risk in set(Risk)
            assert candidate.action in ("promote", "hold", "drop")

    def test_field_name_drives_content(self):
        """字段名叫 sku_code 就该给 SKU 编码，而不是随便一段文字。"""
        instance = build_instance(SelectionResult)
        assert instance.candidates[0].sku_code.startswith("SKU")

    def test_optional_fields_get_real_values_not_none(self):
        """故意不生成 None：下游代码需要真实数据才能跑到真正的分支。

        这个用例同时是一个回归：``datetime | None`` 在 Python 3.10+ 下
        产生的是 ``types.UnionType`` 而不是 ``typing.Union``，
        只判断 ``is Union`` 会让整个 Optional 分支失效，
        表现为"某个字段莫名其妙变成了一段文字"。
        """
        instance = build_instance(SelectionResult)
        assert instance.candidates[0].optional_note is not None
        assert isinstance(instance.candidates[0].created_at, datetime)

    def test_deterministic_for_same_seed(self):
        a = build_instance(SelectionResult, session_seed="s1")
        b = build_instance(SelectionResult, session_seed="s1")
        assert a.model_dump() == b.model_dump()

    def test_different_seed_gives_different_data(self):
        a = build_instance(SelectionResult, session_seed="s1")
        b = build_instance(SelectionResult, session_seed="s2")
        assert a.model_dump() != b.model_dump()

    def test_self_referential_model_does_not_recurse_forever(self):
        # 只要能在合理时间内返回就算通过（depth 上限挡住了无限递归）
        instance = build_instance(TreeNode)
        assert isinstance(instance, TreeNode)
        assert instance.child is not None


class TestMockClient:
    def test_structured_response_is_valid_json(self):
        client = MockModelClient()
        response = client.complete([Message.user("选品")], response_schema=SelectionResult)
        parsed = SelectionResult.model_validate_json(extract_json(response.content))
        assert parsed.candidates

    def test_plain_response_when_no_schema(self):
        client = MockModelClient()
        response = client.complete([Message.user("你好")])
        assert response.content
        assert not response.has_tool_calls

    def test_script_is_consumed_in_order(self):
        client = MockModelClient(script=[MockReply(content="第一步"), MockReply(content="第二步")])
        assert client.complete([Message.user("x")]).content == "第一步"
        assert client.complete([Message.user("x")]).content == "第二步"
        # 脚本用完之后回退到默认行为，不报错
        assert client.complete([Message.user("x")]).content

    def test_emits_tool_call_when_tools_provided(self):
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "erp__query_inventory",
                    "description": "查库存",
                    "parameters": {
                        "type": "object",
                        "properties": {"sku_codes": {"type": "array"}},
                        "required": ["sku_codes"],
                    },
                },
            }
        ]
        client = MockModelClient()
        response = client.complete([Message.user("查一下库存")], tools=tools)
        assert response.has_tool_calls
        assert response.tool_calls[0].name == "erp__query_inventory"
        assert "sku_codes" in response.tool_calls[0].arguments

    def test_stops_emitting_tool_calls_after_tool_result(self):
        """对话里已经有工具结果了，就不该再发 tool_call —— 否则循环不收敛。"""
        tools = [{"type": "function", "function": {"name": "kb__search", "parameters": {}}}]
        client = MockModelClient()
        messages = [
            Message.user("查知识库"),
            Message.assistant("", tool_calls=[ToolCall(id="1", name="kb__search")]),
            Message.tool(tool_call_id="1", content="{}"),
        ]
        assert not client.complete(messages, tools=tools).has_tool_calls

    def test_records_calls_for_assertions(self):
        client = MockModelClient()
        client.complete([Message.user("a")])
        client.complete([Message.user("b")])
        assert len(client.calls) == 2

    def test_usage_and_cost_are_populated(self):
        client = MockModelClient()
        response = client.complete([Message.user("这是一段中文提示词")])
        assert response.usage.total_tokens > 0
        assert response.cost == Decimal("0")  # mock 不花钱
        assert response.latency_ms >= 0


# ===========================================================================
# 结构化输出与修复循环
# ===========================================================================
class TestStructuredCall:
    def test_happy_path(self):
        client = MockModelClient()
        result = structured_call(client, [Message.user("选品")], SelectionResult)
        assert result.ok
        assert result.repair_rounds == 0
        assert result.attempted == 1

    def test_repairs_after_schema_violation(self):
        """第一轮故意给不合规的数据，看它能不能靠回喂错误修回来。"""
        good = build_instance(SelectionResult).model_dump_json()
        client = MockModelClient(
            script=[
                MockReply(content='{"summary": "只有摘要，缺了两个必填字段"}'),
                MockReply(content=good),
            ]
        )
        result = structured_call(client, [Message.user("选品")], SelectionResult)

        assert result.ok, "修复循环应该把输出救回来"
        assert result.repair_rounds == 1
        assert result.attempted == 2

    def test_repair_prompt_contains_specific_errors(self):
        """回喂的内容必须**具体**。只说"你错了重来"，模型大概率再错一次。"""
        good = build_instance(SelectionResult).model_dump_json()
        client = MockModelClient(script=[MockReply(content='{"summary": "x"}'), MockReply(content=good)])
        structured_call(client, [Message.user("选品")], SelectionResult)

        repair_message = client.calls[1][-1]
        assert repair_message.role == "user"
        assert "candidates" in repair_message.content or "字段" in repair_message.content

    def test_gives_up_after_max_repair(self):
        """修复轮数必须有上限，否则遇到模型死活改不对会一直烧 token。"""
        client = MockModelClient(script=[MockReply(content='{"summary": "x"}') for _ in range(10)])
        result = structured_call(client, [Message.user("选品")], SelectionResult, max_repair=2)
        assert not result.ok
        assert result.value is None
        assert result.attempted == 3  # 首轮 + 2 次修复
        assert result.errors

    def test_require_raises_on_failure(self):
        client = MockModelClient(script=[MockReply(content="not json") for _ in range(5)])
        result = structured_call(client, [Message.user("x")], SelectionResult, max_repair=1)
        with pytest.raises(ModelError, match="结构化输出失败"):
            result.require()

    def test_parses_markdown_wrapped_output(self):
        good = build_instance(SelectionResult).model_dump_json()
        client = MockModelClient(script=[MockReply(content=f"```json\n{good}\n```")])
        result = structured_call(client, [Message.user("x")], SelectionResult)
        assert result.ok

    def test_schema_instruction_is_injected_into_system(self):
        client = MockModelClient()
        structured_call(client, [Message.user("x")], SelectionResult)
        system = client.calls[0][0]
        assert system.role == "system"
        assert "JSON Schema" in system.content


# ===========================================================================
# 工具调用循环
# ===========================================================================
class TestToolLoop:
    def _tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "erp__query_inventory",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            }
        ]

    def test_completes_when_model_stops_calling_tools(self):
        client = MockModelClient(
            script=[
                MockReply(tool_calls=[ToolCall(id="c1", name="erp__query_inventory")]),
                MockReply(content="库存充足，建议上架"),
            ]
        )
        seen: list[tuple[str, dict]] = []

        def runner(name, args):  # noqa: ANN001, ANN202
            seen.append((name, args))
            return {"ok": True, "data": {"available": 120}}

        result = run_tool_loop(
            client,
            [Message.user("查库存")],
            tools=self._tools(),
            tool_runner=runner,
        )

        assert result.finished
        assert result.steps == 2
        assert result.content == "库存充足，建议上架"
        assert seen == [("erp__query_inventory", {})]
        assert len(result.tool_calls) == 1

    def test_tool_result_is_fed_back_to_model(self):
        client = MockModelClient(
            script=[
                MockReply(tool_calls=[ToolCall(id="c1", name="erp__query_inventory")]),
                MockReply(content="完成"),
            ]
        )
        run_tool_loop(
            client,
            [Message.user("查库存")],
            tools=self._tools(),
            tool_runner=lambda n, a: {"ok": True, "data": {"available": 7}},
        )
        # 第二轮请求里应该能看到工具返回的结果
        second_call = client.calls[1]
        tool_messages = [m for m in second_call if m.role == "tool"]
        assert tool_messages
        assert "available" in tool_messages[0].content

    def test_tool_exception_becomes_error_payload_not_crash(self):
        """工具抛异常要变成给模型看的错误信息，而不是把整个任务打挂。"""
        client = MockModelClient(
            script=[
                MockReply(tool_calls=[ToolCall(id="c1", name="erp__query_inventory")]),
                MockReply(content="工具挂了，我改用其他方案"),
            ]
        )

        def boom(name, args):  # noqa: ANN001, ANN202
            raise RuntimeError("下游 500")

        result = run_tool_loop(client, [Message.user("查库存")], tools=self._tools(), tool_runner=boom)
        assert result.finished
        tool_messages = [m for m in client.calls[1] if m.role == "tool"]
        assert "TOOL_EXECUTION_FAILED" in tool_messages[0].content

    def test_max_steps_caps_infinite_loop(self):
        """模型要是一直在调工具，必须有硬上限兜住。"""
        client = MockModelClient(
            script=[MockReply(tool_calls=[ToolCall(id=f"c{i}", name="erp__query_inventory")]) for i in range(20)]
        )
        result = run_tool_loop(
            client,
            [Message.user("查库存")],
            tools=self._tools(),
            tool_runner=lambda n, a: {"ok": True},
            max_steps=3,
        )
        assert not result.finished
        assert result.steps == 3
        assert len(result.tool_calls) == 3

    def test_aggregates_usage_and_cost(self):
        client = MockModelClient(
            script=[
                MockReply(tool_calls=[ToolCall(id="c1", name="erp__query_inventory")]),
                MockReply(content="完成"),
            ]
        )
        result = run_tool_loop(
            client,
            [Message.user("查库存")],
            tools=self._tools(),
            tool_runner=lambda n, a: {"ok": True},
        )
        assert result.total_usage.total_tokens > 0
        assert len(result.responses) == 2


# ===========================================================================
# 模型路由
# ===========================================================================
class TestModelRouter:
    def test_complex_task_goes_to_heavy_model(self):
        router = ModelRouter("heavy", "light")
        assert router.choose("请分析最近退货率上升的原因并做归因") == "heavy"

    def test_simple_task_goes_to_light_model(self):
        router = ModelRouter("heavy", "light")
        assert router.choose("把这句话翻译一下") == "light"

    def test_ambiguous_defaults_to_heavy(self):
        """拿不准就用主力模型 —— 省钱的收益远小于答错的代价。"""
        router = ModelRouter("heavy", "light")
        assert router.choose("帮我看看这个") == "heavy"

    def test_very_long_prompt_forces_heavy(self):
        router = ModelRouter("heavy", "light")
        assert router.choose("x" * 3000) == "heavy"

    def test_force_heavy_overrides(self):
        router = ModelRouter("heavy", "light")
        assert router.choose("翻译", force_heavy=True) == "heavy"

    def test_stats_track_routing(self):
        router = ModelRouter("heavy", "light")
        router.choose("翻译")
        router.choose("分析归因")
        stats = router.to_dict()
        assert stats["routed_light"] == 1
        assert stats["routed_heavy"] == 1
        assert stats["light_ratio"] == 0.5


# ===========================================================================
# 工厂
# ===========================================================================
class TestFactory:
    def test_mock_provider_needs_no_key(self):
        from fashionops.config import Settings

        client = get_model_client(Settings(model_provider="mock"))
        assert isinstance(client, MockModelClient)

    def test_missing_key_falls_back_to_mock(self):
        """配了真实 provider 但忘填 Key 时自动降级，而不是启动就崩。"""
        from fashionops.config import Settings

        client = get_model_client(Settings(model_provider="deepseek", model_api_key=None))
        assert isinstance(client, MockModelClient)
