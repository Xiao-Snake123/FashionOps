"""模型客户端的统一入口：工厂、结构化输出、工具调用循环。

## 结构化输出与"修复循环"

Agent 的每个节点几乎都要模型吐出一个符合 schema 的对象（选品打分、
DAG 拆解、意图分类……）。但模型**一定会**有概率给出不合规的输出：
多一层嵌套、少一个字段、数字写成字符串、或者干脆包在 markdown 代码块里。

面对这种情况有三条路：

1. **直接抛异常** —— 一个格式抖动就让整个任务失败，不可接受
2. **让模型重来整轮** —— 浪费 token，而且可能再错
3. **把校验错误回喂给模型让它修** —— 这才是有用的做法

这里实现的是第 3 种。关键在于**回喂的内容**：不能只说"你错了，重来"，
要把 Pydantic 的具体报错（哪个字段、什么类型不对）连同上次的输出一起给它。
模型看到 ``field required: score`` 这种明确信息，修对的概率很高。

实测下来修复循环能把结构化输出的失败率压得很低，
而且**最大修复轮数必须设上限**（默认 2）—— 否则遇到模型死活改不对的情况
会一直烧 token。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from fashionops.config import Settings, get_settings
from fashionops.llm.mock import MockModelClient
from fashionops.llm.types import Message, ModelError, ModelResponse, ToolCall, Usage

logger = logging.getLogger("fashionops.llm")

T = TypeVar("T", bound=BaseModel)

#: 默认最大修复轮数。再往上收益很小，但 token 会一直涨。
DEFAULT_MAX_REPAIR = 2

#: 模型即使被要求输出纯 JSON，也常把结果包在代码块里 —— 解析前先剥掉
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class ModelClient(Protocol):
    """模型客户端协议。mock 和真实实现都满足它。"""

    def complete(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: type[BaseModel] | None = None,
        temperature: float | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> ModelResponse: ...


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
def get_model_client(settings: Settings | None = None) -> ModelClient:
    """按配置返回模型客户端。

    ``provider=mock`` 时不需要 API Key —— 这是刻意的默认值，
    保证 clone 下来就能跑通全链路，而不是先卡在"要填 Key"。
    """
    settings = settings or get_settings()
    provider = (settings.model_provider or "mock").lower()

    if provider == "mock" or not settings.model_api_key:
        if provider != "mock":
            logger.warning(
                "model_provider=%s 但没配 model_api_key，自动降级为 mock。"
                "要接真实模型请在 .env 里填 FASHIONOPS_MODEL_API_KEY。",
                provider,
            )
        return MockModelClient(model=settings.model_name or "mock-model")

    from fashionops.llm.openai_compat import OpenAICompatClient

    base_urls = {
        "deepseek": "https://api.deepseek.com/v1",
        "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "zhipu": "https://open.bigmodel.cn/api/paas/v4",
        "openai": None,
    }
    return OpenAICompatClient(
        api_key=settings.model_api_key,
        model=settings.model_name,
        base_url=settings.model_base_url or base_urls.get(provider),
    )


# ---------------------------------------------------------------------------
# JSON 解析
# ---------------------------------------------------------------------------
def extract_json(text: str) -> str:
    """从模型输出里捞出 JSON 主体。

    处理三种常见脏数据：
    1. 整段包在 ```json ... ``` 里
    2. 前后有解释性废话（"好的，这是结果：{...}"）
    3. 正常的纯 JSON
    """
    stripped = text.strip()
    if not stripped:
        return stripped

    match = _FENCE_RE.search(stripped)
    if match:
        return match.group(1).strip()

    # 找第一个 { 到最后一个 } —— 比按行找更稳，能处理多行 JSON
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        return stripped[start : end + 1]

    start = stripped.find("[")
    end = stripped.rfind("]")
    if start != -1 and end > start:
        return stripped[start : end + 1]

    return stripped


def schema_instruction(schema: type[BaseModel]) -> str:
    """生成给模型看的 schema 说明。"""
    return (
        "你只能输出一个 JSON 对象，不要任何解释文字，不要 markdown 代码块。\n"
        "JSON 必须满足下面这个 JSON Schema：\n"
        f"{json.dumps(schema.model_json_schema(), ensure_ascii=False)}"
    )


# ---------------------------------------------------------------------------
# 结构化调用
# ---------------------------------------------------------------------------
@dataclass
class StructuredResult:
    """结构化调用的结果。

    ``value`` 为 ``None`` 表示修复轮数用尽仍未通过校验 ——
    这时调用方必须显式处理失败，而不是拿到一个半成品继续往下跑。
    """

    value: BaseModel | None
    response: ModelResponse
    repair_rounds: int = 0
    attempted: int = 1
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.value is not None

    def require(self) -> BaseModel:
        """强制要求成功，失败则抛异常。给"拿不到结果就没法继续"的场景用。"""
        if self.value is None:
            raise ModelError(
                f"结构化输出失败，已尝试 {self.attempted} 次。最后错误："
                + " | ".join(self.errors[-2:])
            )
        return self.value


def structured_call(
    client: ModelClient,
    messages: list[Message],
    schema: type[T],
    *,
    max_repair: int = DEFAULT_MAX_REPAIR,
    model: str | None = None,
    temperature: float | None = None,
) -> StructuredResult:
    """调用模型并拿到一个通过校验的 ``schema`` 实例。

    流程::

        1. 在 system 里注入 schema 说明
        2. 调用模型
        3. 解析 + Pydantic 校验
        4. 失败 → 把**具体校验错误 + 上次输出**回喂给它，重试
        5. 达到 max_repair 仍失败 → 返回 value=None（不抛异常）
    """
    working = list(messages)
    # 只在最前面插一次 schema 说明，后续修复轮不重复注入
    if not working or working[0].role != "system":
        working.insert(0, Message.system(schema_instruction(schema)))
    else:
        working[0] = Message.system(working[0].content + "\n\n" + schema_instruction(schema))

    errors: list[str] = []
    last_response: ModelResponse | None = None
    repair = 0
    attempted = 0

    while attempted <= max_repair:
        attempted += 1
        response = client.complete(
            working,
            response_schema=schema,
            model=model,
            temperature=temperature,
        )
        last_response = response

        try:
            payload = extract_json(response.content)
            value = schema.model_validate_json(payload)
        except ValidationError as exc:
            detail = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}"
                for e in exc.errors()[:6]
            )
            errors.append(f"校验失败：{detail}")
        except (json.JSONDecodeError, ValueError) as exc:
            errors.append(f"JSON 解析失败：{exc}")
        else:
            return StructuredResult(
                value=value, response=response, repair_rounds=repair, attempted=attempted
            )

        if attempted > max_repair:
            break

        repair += 1
        logger.info("结构化输出第 %d 轮修复：%s", repair, errors[-1])

        # 关键：回喂必须**具体**。只说"你错了重来"，模型大概率再错一次。
        working.append(Message.assistant(response.content))
        working.append(
            Message.user(
                "你上一次的输出不符合 schema。\n"
                f"错误：{errors[-1]}\n"
                "请只输出修正后的 JSON 对象，不要解释，不要 markdown 代码块。"
            )
        )

    assert last_response is not None
    return StructuredResult(
        value=None,
        response=last_response,
        repair_rounds=repair,
        attempted=attempted,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# 工具调用循环
# ---------------------------------------------------------------------------
@dataclass
class ToolLoopResult:
    content: str
    messages: list[Message]
    steps: int
    tool_calls: list[ToolCall]
    responses: list[ModelResponse]
    finished: bool

    @property
    def total_usage(self) -> Usage:
        total = Usage()
        for r in self.responses:
            total = total + r.usage
        return total

    @property
    def total_cost(self) -> Decimal:
        return sum((r.cost for r in self.responses), Decimal("0"))

    @property
    def total_latency_ms(self) -> int:
        return sum(r.latency_ms for r in self.responses)


def run_tool_loop(
    client: ModelClient,
    messages: list[Message],
    *,
    tools: list[dict[str, Any]],
    tool_runner: Any,
    max_steps: int = 8,
    model: str | None = None,
    temperature: float | None = None,
) -> ToolLoopResult:
    """跑「模型要求调工具 → 执行 → 把结果回喂」这个循环。

    ``tool_runner`` 接收 ``(name, arguments)`` 返回一个 **dict**，
    由调用方决定返回什么给模型看 —— 通常是 ``ToolResult.to_dict()``，
    这样模型能看到 ``ok/error.hint``，从而自己决定是重试还是换方案。

    ``max_steps`` 是硬性上限。**工具循环必须有上限**：
    模型有可能在两个工具之间来回跳，或者反复重试同一个失败的工具，
    没有上限就是拿 token 打水漂。
    """
    working = list(messages)
    responses: list[ModelResponse] = []
    executed: list[ToolCall] = []
    steps = 0

    while steps < max_steps:
        steps += 1
        response = client.complete(
            working, tools=tools, model=model, temperature=temperature
        )
        responses.append(response)

        if not response.has_tool_calls:
            return ToolLoopResult(
                content=response.content,
                messages=working,
                steps=steps,
                tool_calls=executed,
                responses=responses,
                finished=True,
            )

        working.append(Message.assistant(response.content, tool_calls=response.tool_calls))
        for call in response.tool_calls:
            executed.append(call)
            try:
                payload = tool_runner(call.name, call.arguments)
            except Exception as exc:  # noqa: BLE001
                # 工具执行抛异常也要喂回给模型，让它知道发生了什么并改策略。
                # 直接往上抛会把"一次工具失败"升级成"整个任务失败"。
                logger.warning("工具 %s 执行抛异常：%s", call.name, exc)
                payload = {"ok": False, "error": {"code": "TOOL_EXECUTION_FAILED",
                                                  "message": str(exc)}}

            working.append(
                Message.tool(
                    tool_call_id=call.id,
                    name=call.name,
                    content=json.dumps(payload, ensure_ascii=False, default=str),
                )
            )

    # 步数用尽：把当前状态返回，由调用方决定是转人工还是降级
    return ToolLoopResult(
        content=responses[-1].content if responses else "",
        messages=working,
        steps=steps,
        tool_calls=executed,
        responses=responses,
        finished=False,
    )


# ---------------------------------------------------------------------------
# 模型路由
# ---------------------------------------------------------------------------
class ModelRouter:
    """按任务复杂度选模型。

    这是 §3 里 P2 的「模型路由」，之所以便宜地先做掉：
    它能直接产出"成本下降 X%"这个数字，而实现成本极低。
    路由策略是规则式的（关键词 + 长度）而不是再上一个分类模型 ——
    为了省钱而花钱，逻辑上就不成立。
    """

    #: 命中这些词的任务足够简单，用轻量模型就够
    SIMPLE_HINTS = ("分类", "判断", "是否", "抽取", "格式化", "翻译", "摘要")
    #: 命中这些词说明需要推理，必须上主力模型
    COMPLEX_HINTS = ("推理", "分析", "规划", "拆解", "复盘", "归因", "对比", "为什么")

    def __init__(self, heavy_model: str, light_model: str) -> None:
        self.heavy_model = heavy_model
        self.light_model = light_model
        self.routed_light = 0
        self.routed_heavy = 0

    def choose(self, prompt: str, *, force_heavy: bool = False) -> str:
        if force_heavy:
            self.routed_heavy += 1
            return self.heavy_model

        text = prompt.lower()
        if any(word in text for word in self.COMPLEX_HINTS) or len(prompt) > 2000:
            self.routed_heavy += 1
            return self.heavy_model

        if any(word in text for word in self.SIMPLE_HINTS):
            self.routed_light += 1
            return self.light_model

        # 拿不准就用主力模型：省钱的收益远小于答错的代价
        self.routed_heavy += 1
        return self.heavy_model

    def to_dict(self) -> dict[str, Any]:
        total = self.routed_light + self.routed_heavy
        return {
            "heavy_model": self.heavy_model,
            "light_model": self.light_model,
            "routed_light": self.routed_light,
            "routed_heavy": self.routed_heavy,
            "light_ratio": round(self.routed_light / total, 4) if total else None,
        }
