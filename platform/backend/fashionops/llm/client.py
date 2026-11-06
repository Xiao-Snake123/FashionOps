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
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from fashionops.config import Settings, get_settings
from fashionops.llm.mock import MockModelClient
from fashionops.llm.types import (
    Message,
    ModelError,
    ModelResponse,
    StreamChunk,
    StreamChunkKind,
    ToolCall,
    Usage,
)

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

    def stream(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: type[BaseModel] | None = None,
        temperature: float | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> Iterator[StreamChunk]:
        """流式版本。

        **契约的关键一条**：最后**必须**产出一个 ``kind=done`` 的分片，
        里面带着拼装完整的 :class:`ModelResponse`。

        这条约定让上层只需要关心"响应是怎么拿到的"，
        后面的控制流（有没有工具调用、怎么记账、怎么写库）
        **和 ``complete()`` 完全一样** —— 不需要为流式再写一套分支。
        """
        ...


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
        # 超时和重试都走配置：这一步曾被硬编码成 60 秒且不重试，
        # 结果一次网络抖动就能把整个任务判失败（真实事故见 openai_compat 的注释）
        timeout=settings.model_timeout,
        retries=settings.model_max_retries,
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
    #: 循环因为**需要人工介入**而停下（见 :class:`ToolLoopPaused`）。
    #: 和 ``finished=False``（步数用尽）是两件事：那个是"没跑完"，
    #: 这个是"按设计停下来了，等人"。
    paused: bool = False
    paused_reason: str = ""
    paused_payload: dict[str, Any] = field(default_factory=dict)
    #: 挂起时**还没执行**的那个工具调用。人工确认后要从它继续，
    #: 所以必须带出来 —— 不带的话恢复时只能靠猜是哪一次调用。
    pending_tool_call: ToolCall | None = None

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


class ToolLoopPaused(Exception):
    """工具执行需要人工介入 —— 循环在这里停下，等确认后再继续。

    ## 为什么用异常，而不是让 tool_runner 返回一个特殊值

    让 ``tool_runner`` 返回 ``{"ok": False, "pending_approval": ...}`` 也能走通，
    但那个结构会被**回喂给模型**：模型看到"工具失败"就会去想办法绕过去 ——
    换个参数重试、换个工具、或者跟用户解释"这个操作失败了"。
    而实际上什么都没失败，只是在等人点确认。

    异常是控制流，不会混进数据流。``raise`` 之后就不必再担心
    它被当成内容传给模型，也不用在 prompt 里写一段"看到 pending_approval
    请不要重试"来打补丁。
    """

    def __init__(self, reason: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.payload = payload or {}


def run_tool_loop(
    client: ModelClient,
    messages: list[Message],
    *,
    tools: list[dict[str, Any]],
    tool_runner: Any,
    max_steps: int = 8,
    model: str | None = None,
    temperature: float | None = None,
    stream: bool = False,
    on_token: Callable[[str], None] | None = None,
    on_tool_call: Callable[[ToolCall], None] | None = None,
    on_tool_result: Callable[[ToolCall, dict[str, Any]], None] | None = None,
    #: 只在**第一次**请求上生效的 tool_choice。
    #: 用来"逼"模型必须调某个工具（见 `agents/loop.py` 的必需工具检查）。
    #: 默认 None → 客户端按 ``auto`` 处理。
    first_tool_choice: Any = None,
) -> ToolLoopResult:
    """跑「模型要求调工具 → 执行 → 把结果回喂」这个循环。

    ``tool_runner`` 接收 ``(name, arguments)`` 返回一个 **dict**，
    由调用方决定返回什么给模型看 —— 通常是 ``ToolResult.to_dict()``，
    这样模型能看到 ``ok/error.hint``，从而自己决定是重试还是换方案。

    ``tool_runner`` 也可以 ``raise ToolLoopPaused`` 来要求人工确认：
    循环会带着 ``pending_tool_call`` 返回，由上层决定怎么问人。
    **注意这种情况下结果不会被回喂给模型** —— 理由见 :class:`ToolLoopPaused`。

    ``max_steps`` 是硬性上限。**工具循环必须有上限**：
    模型有可能在两个工具之间来回跳，或者反复重试同一个失败的工具，
    没有上限就是拿 token 打水漂。

    ## 流式只改"响应怎么拿到"，不改控制流

    ``stream=True`` 时改用 ``client.stream()``，并通过 ``on_token``
    逐片回调文本。**除此之外循环逻辑一字未改** ——
    因为 ``stream()`` 的契约保证最后会给出和 ``complete()`` 同一种
    ``ModelResponse``。

    这一点是刻意设计的：如果流式和非流式各写一套循环，两者的行为迟早会漂移
    （比如"流式路径忘了记账"或者"非流式路径没触发 on_tool_result"），
    而这种差异只在某一条路径上出现，测试很容易漏。
    """
    working = list(messages)
    responses: list[ModelResponse] = []
    executed: list[ToolCall] = []
    steps = 0

    while steps < max_steps:
        steps += 1
        response = _obtain_response(
            client,
            working,
            tools=tools,
            model=model,
            temperature=temperature,
            stream=stream,
            on_token=on_token,
            on_tool_call=on_tool_call,
            # 只在第一轮强制：逼出那次工具调用之后就交还给模型自己决定，
            # 否则它会一直被逼着调同一个工具，停不下来
            tool_choice=first_tool_choice if steps == 1 else None,
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
            try:
                payload = tool_runner(call.name, call.arguments)
            except ToolLoopPaused as paused:
                return ToolLoopResult(
                    content=response.content,
                    messages=working,
                    steps=steps,
                    tool_calls=executed,
                    responses=responses,
                    finished=False,
                    paused=True,
                    paused_reason=paused.reason,
                    paused_payload=paused.payload,
                    pending_tool_call=call,
                )
            except Exception as exc:  # noqa: BLE001
                # 工具执行抛异常也要喂回给模型，让它知道发生了什么并改策略。
                # 直接往上抛会把"一次工具失败"升级成"整个任务失败"。
                logger.warning("工具 %s 执行抛异常：%s", call.name, exc)
                payload = {"ok": False, "error": {"code": "TOOL_EXECUTION_FAILED",
                                                  "message": str(exc)}}

            # 只有**真的执行了**才记进 executed。
            # 挂起的那次没执行，记进去会让审计说"调用过这个工具"—— 而它没被调用。
            executed.append(call)
            if on_tool_result is not None:
                on_tool_result(call, payload)
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


def _obtain_response(
    client: ModelClient,
    messages: list[Message],
    *,
    tools: list[dict[str, Any]],
    model: str | None,
    temperature: float | None,
    stream: bool,
    on_token: Callable[[str], None] | None,
    on_tool_call: Callable[[ToolCall], None] | None,
    tool_choice: Any = None,
) -> ModelResponse:
    """拿一次响应 —— 整块或流式，对调用方是同一件事。"""
    if not stream:
        response = client.complete(
            messages, tools=tools, model=model, temperature=temperature, tool_choice=tool_choice
        )
        # 非流式也回调一次：让调用方不必为两种模式各写一套"把文本显示出去"的逻辑。
        # 少了这一句就会出现"切换 stream 开关后前端不显示内容"这种莫名其妙的 bug
        if on_token is not None and response.content:
            on_token(response.content)
        return response

    response: ModelResponse | None = None
    for chunk in client.stream(
        messages, tools=tools, model=model, temperature=temperature, tool_choice=tool_choice
    ):
        if chunk.kind is StreamChunkKind.CONTENT:
            if on_token is not None and chunk.text:
                on_token(chunk.text)
        elif chunk.kind is StreamChunkKind.TOOL_CALL and chunk.tool_call is not None:
            if on_tool_call is not None:
                on_tool_call(chunk.tool_call)
        elif chunk.kind is StreamChunkKind.DONE:
            response = chunk.response

    if response is None:
        # 客户端违反了"最后必须有 done 分片"的契约。
        # **不静默返回空内容** —— 那会让上层把空字符串当成模型的回答写进历史，
        # 用户看到一条空消息、日志里一条报错都没有，是最难查的一类问题。
        raise ModelError("流式响应缺少结束分片（done），无法确定本次结果")
    return response


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
