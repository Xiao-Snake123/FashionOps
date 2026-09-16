"""OpenAI 兼容协议的模型客户端。

## 一个客户端接四家

DeepSeek、通义千问、智谱 GLM、以及 OpenAI 本身都提供 OpenAI 兼容的
``/chat/completions`` 接口。所以不需要为每家写一个适配器 ——
只要换 ``base_url`` 和 ``api_key``：

| 提供方 | base_url |
| --- | --- |
| DeepSeek | ``https://api.deepseek.com/v1`` |
| 通义千问 | ``https://dashscope.aliyuncs.com/compatible-mode/v1`` |
| 智谱 GLM | ``https://open.bigmodel.cn/api/paas/v4`` |
| OpenAI | 留空（SDK 默认） |

这也是为什么选 ``langchain-openai`` 作为 LangChain 的接入层 —— 它同样吃这套协议。

## 几个真实踩过的点

1. **``max_retries`` 自己控**：SDK 自带重试会和我们的重试叠加，
   一次调用实际发好几次请求，成本和延迟都翻倍且难排查。这里关掉 SDK 重试，
   统一由上层决定。
2. **``response_format`` 的支持度不一致**：``json_object`` 各家都支持，
   但严格的 json_schema 模式（``strict: true``）并不是。所以这里只用
   ``json_object``，把 schema 写进 system prompt 里 —— 通用性优先。
3. **模型可能把 JSON 包在 markdown 代码块里**：即使要求了 json_object，
   有些模型仍会输出 ```json ... ```。解析时要兜住。
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel

from fashionops.llm.pricing import approx_tokens_text, estimate_cost
from fashionops.llm.types import (
    FinishReason,
    Message,
    ModelError,
    ModelResponse,
    StreamChunk,
    StreamChunkKind,
    ToolCall,
    Usage,
)

#: 上游的 ``finish_reason`` → 我们自己的枚举。
#: 抽成模块级常量是因为 ``complete`` 和 ``stream`` 两条路径都要用 ——
#: 分开写迟早会出现"整块调用认它、流式不认它"这种飘移。
_FINISH_MAP: dict[str, FinishReason] = {
    "stop": FinishReason.STOP,
    "length": FinishReason.LENGTH,
    "tool_calls": FinishReason.TOOL_CALLS,
    "content_filter": FinishReason.CONTENT_FILTER,
}


class OpenAICompatClient:
    provider = "openai_compat"

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        base_url: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise ValueError("缺少 model_api_key。要零成本开发请把 provider 设为 mock。")

        from openai import OpenAI

        self.model = model
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url or None,
            timeout=timeout,
            # 关掉 SDK 自带重试：它和上层的重试策略会叠加，
            # 一次调用实际发好几次请求，成本和延迟都翻倍，而且日志里看不出来
            max_retries=0,
        )

    def complete(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: type[BaseModel] | None = None,
        temperature: float | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> ModelResponse:
        used_model = model or self.model
        payload: dict[str, Any] = {
            "model": used_model,
            "messages": [_to_openai_message(m) for m in messages],
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if response_schema is not None:
            # 只用最通用的 json_object 模式，schema 靠 prompt 约束
            payload["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        try:
            raw = self._client.chat.completions.create(**payload, **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise _classify(exc) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        return self._parse(raw, used_model, latency_ms)

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
        """流式调用。逐片吐 ``content``，工具调用拼完整后再吐。

        ## 为什么不传 ``stream_options.include_usage``

        那个参数能在最后一个 chunk 里带回准确的 ``usage``，看起来正是我们要的。
        但**它不是所有 OpenAI 兼容厂商都支持**：在支持的厂商上它是锦上添花，
        在不支持的厂商上它会直接把整个请求 400 掉 ——
        也就是说，为了让成本统计"精确到 token"，赌上了整条对话链路能不能用。

        所以这里的选择是：``usage`` 有就读（不少厂商默认会给），
        没有就用 :func:`approx_tokens_text` 估一个量级正确的数字。
        **成本归因的精度不值得拿可用性去换。**

        ## 工具调用的分片拼接

        流式下 ``tool_calls`` 是按 ``index`` 分组、交叉到达的：

            {"index":0,"id":"call_1","function":{"name":"erp__query","arguments":"{\"sk"}}
            {"index":1,"id":"call_2","function":{"name":"kb__search","arguments":"{\"q"}}
            {"index":0,"function":{"arguments":"u_codes\":[\"A"}}

        **必须按 index 分组累积**。不分组的话，一轮里同时发起两个工具调用时，
        两个工具的参数会被拼到一起 —— 而报错现场是"JSON 解析失败"，
        排查方向很容易跑到"模型输出不稳定"上去。
        """
        used_model = model or self.model
        payload: dict[str, Any] = {
            "model": used_model,
            "messages": [_to_openai_message(m) for m in messages],
            "stream": True,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if response_schema is not None:
            payload["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        try:
            stream = self._client.chat.completions.create(**payload, **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise _classify(exc) from exc

        content_parts: list[str] = []
        #: index -> 该工具调用的分片累积
        pending: dict[int, dict[str, str]] = {}
        finish_raw = ""
        usage = Usage()

        try:
            for chunk in stream:
                raw_usage = getattr(chunk, "usage", None)
                if raw_usage is not None:
                    usage = Usage(
                        prompt_tokens=getattr(raw_usage, "prompt_tokens", 0) or 0,
                        completion_tokens=getattr(raw_usage, "completion_tokens", 0) or 0,
                        total_tokens=getattr(raw_usage, "total_tokens", 0) or 0,
                    )

                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                choice = choices[0]
                if getattr(choice, "finish_reason", None):
                    finish_raw = str(choice.finish_reason)

                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue

                text = getattr(delta, "content", None)
                if text:
                    content_parts.append(text)
                    yield StreamChunk(kind=StreamChunkKind.CONTENT, text=text)

                for call in getattr(delta, "tool_calls", None) or []:
                    index = getattr(call, "index", 0)
                    slot = pending.setdefault(
                        index if index is not None else 0, {"id": "", "name": "", "args": ""}
                    )
                    if getattr(call, "id", None):
                        slot["id"] = call.id
                    function = getattr(call, "function", None)
                    if function is not None:
                        if getattr(function, "name", None):
                            slot["name"] = function.name
                        if getattr(function, "arguments", None):
                            slot["args"] += function.arguments
        except Exception as exc:  # noqa: BLE001
            # 中途断流也要翻译成 ModelError：上层可能已经收到半段内容，
            # 它需要知道"这次是失败的"，才能把消息标成不完整
            raise _classify(exc) from exc
        finally:
            # 提前退出（前端断开、上层抛异常）时把 HTTP 连接关掉，
            # 否则连接会一直挂到超时，把连接池占满
            with contextlib.suppress(Exception):
                stream.close()

        tool_calls: list[ToolCall] = []
        for index in sorted(pending):
            slot = pending[index]
            raw_args = slot["args"] or "{}"
            try:
                arguments = json.loads(raw_args)
            except json.JSONDecodeError:
                arguments = {}
            tool_calls.append(
                ToolCall(
                    id=slot["id"] or f"call_{index}",
                    name=slot["name"],
                    arguments=arguments,
                    raw_arguments=raw_args if not arguments else None,
                )
            )
            yield StreamChunk(kind=StreamChunkKind.TOOL_CALL, tool_call=tool_calls[-1])

        content = "".join(content_parts)
        if usage.total_tokens == 0:
            # 厂商没给 usage：prompt 拿整段消息估，completion 拿输出文本估。
            # 比真实值有偏差，但成本归因只需要量级对 —— 而且比"一个人读不到
            # 成本数字、干脆不做成本统计"要好得多。
            usage = Usage(
                prompt_tokens=approx_tokens_text(" ".join(m.content for m in messages)),
                completion_tokens=approx_tokens_text(content),
            )
            usage.total_tokens = usage.prompt_tokens + usage.completion_tokens

        response = ModelResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            cost=estimate_cost(used_model, usage.prompt_tokens, usage.completion_tokens),
            latency_ms=int((time.perf_counter() - started) * 1000),
            model=used_model,
            provider=self.provider,
            finish_reason=_FINISH_MAP.get(finish_raw, FinishReason.STOP),
        )
        yield StreamChunk(kind=StreamChunkKind.DONE, response=response)

    # ------------------------------------------------------------------
    @staticmethod
    def _parse(raw: Any, model: str, latency_ms: int) -> ModelResponse:
        choice = raw.choices[0] if raw.choices else None
        if choice is None:
            # 极少数情况下上游返回空 choices（内容被安全策略拦截等）
            return ModelResponse(
                content="",
                model=model,
                provider="openai_compat",
                finish_reason=FinishReason.ERROR,
                latency_ms=latency_ms,
            )

        message = choice.message
        tool_calls: list[ToolCall] = []
        for call in getattr(message, "tool_calls", None) or []:
            raw_args = call.function.arguments or "{}"
            try:
                arguments = json.loads(raw_args)
            except json.JSONDecodeError:
                # 参数不是合法 JSON 时不要直接抛 —— 把原文带上，
                # 让上层能把"模型给错了参数"当作一次可修复的失败来处理
                arguments = {}
            tool_calls.append(
                ToolCall(
                    id=call.id,
                    name=call.function.name,
                    arguments=arguments,
                    raw_arguments=raw_args if not arguments else None,
                )
            )

        usage_raw = getattr(raw, "usage", None)
        usage = Usage(
            prompt_tokens=getattr(usage_raw, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage_raw, "completion_tokens", 0) or 0,
            total_tokens=getattr(usage_raw, "total_tokens", 0) or 0,
        )
        if usage.total_tokens == 0:
            usage.total_tokens = usage.prompt_tokens + usage.completion_tokens

        finish = _FINISH_MAP.get(
            str(getattr(choice, "finish_reason", "") or ""), FinishReason.STOP
        )

        return ModelResponse(
            content=message.content or "",
            tool_calls=tool_calls,
            usage=usage,
            cost=estimate_cost(model, usage.prompt_tokens, usage.completion_tokens),
            latency_ms=latency_ms,
            model=model,
            provider="openai_compat",
            finish_reason=finish,
        )


def _to_openai_message(message: Message) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.name:
        payload["name"] = message.name
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in message.tool_calls
        ]
    return payload


def _classify(exc: Exception) -> ModelError:
    """把 SDK 的异常翻译成"能不能重试"。

    区分这个很重要：把 429（限流）当成不可重试，会白白失败掉任务；
    把 401（Key 错了）当成可重试，会一直重试到把配额耗尽。
    """
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    text = f"{type(exc).__name__}: {exc}"

    if status in (408, 409, 429) or (status is not None and status >= 500):
        return ModelError(f"模型服务暂时不可用：{text}", retryable=True, status=status)

    if status in (401, 403):
        return ModelError(
            f"模型认证失败，请检查 FASHIONOPS_MODEL_API_KEY：{text}",
            retryable=False,
            status=status,
        )

    if status == 400:
        # 参数问题重试也没用，但很可能是 prompt 太长这类可以改的地方
        return ModelError(f"模型请求被拒绝（可能是上下文超长或参数不合法）：{text}",
                          retryable=False, status=status)

    # 网络类异常（连接超时、DNS 失败）值得重试
    if any(word in text.lower() for word in ("timeout", "connection", "temporarily")):
        return ModelError(f"模型网络异常：{text}", retryable=True)

    return ModelError(text, retryable=False, status=status)
