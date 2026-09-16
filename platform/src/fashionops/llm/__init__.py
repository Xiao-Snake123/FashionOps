"""模型层：统一的客户端、结构化输出、成本归因、模型路由。

用法::

    from fashionops.llm import Message, get_model_client, structured_call

    client = get_model_client()          # provider=mock 时不需要 API Key
    result = structured_call(
        client,
        [Message.system("你是选品专家"), Message.user("帮我选 3 款秋季女装")],
        SelectionResult,
    )
    if result.ok:
        print(result.value.model_dump())
    else:
        print("修了", result.attempted, "轮还是没通过：", result.errors)
"""

from __future__ import annotations

from fashionops.llm.client import (
    DEFAULT_MAX_REPAIR,
    ModelClient,
    ModelRouter,
    StructuredResult,
    ToolLoopPaused,
    ToolLoopResult,
    extract_json,
    get_model_client,
    run_tool_loop,
    schema_instruction,
    structured_call,
)
from fashionops.llm.mock import MockModelClient, MockReply, build_instance, fake_instance
from fashionops.llm.pricing import PRICES, approx_tokens_text, estimate_cost, price_of
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

__all__ = [
    "DEFAULT_MAX_REPAIR",
    "PRICES",
    "FinishReason",
    "Message",
    "MockModelClient",
    "MockReply",
    "ModelClient",
    "ModelError",
    "ModelResponse",
    "ModelRouter",
    "StreamChunk",
    "StreamChunkKind",
    "StructuredResult",
    "ToolCall",
    "ToolLoopPaused",
    "ToolLoopResult",
    "Usage",
    "approx_tokens_text",
    "build_instance",
    "estimate_cost",
    "extract_json",
    "fake_instance",
    "get_model_client",
    "price_of",
    "run_tool_loop",
    "schema_instruction",
    "structured_call",
]
