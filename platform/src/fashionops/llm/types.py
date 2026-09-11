"""模型层的公共类型。

## 为什么不用 LangChain 的 Message 类

LangGraph/LangChain 自带 ``HumanMessage`` / ``AIMessage`` 等类型，直接用很方便。
但这里刻意只依赖 ``langchain-core`` 里最稳定的部分，自己定义一层薄的类型：

* **可测**：不用起一个假的 chat model 就能测模型层逻辑
* **可换**：将来换成 Anthropic 原生 SDK、或者本地 vLLM，业务代码不动
* **可记**：token 数、成本、延迟这些"要拿去算指标"的字段，
  放在自己的类型里比从 LangChain 的 ``response_metadata`` 里挖更可靠 ——
  那个字典的结构在不同 provider 下是不一样的
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]


class Message(BaseModel):
    """一条对话消息。

    ``tool_call_id`` 只在 ``role="tool"`` 时有值 —— 用来把工具执行结果
    对应回模型发出的那个 tool_call。
    """

    role: Role
    content: str = ""
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] | None = None

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str, tool_calls: list[ToolCall] | None = None) -> Message:
        return cls(role="assistant", content=content, tool_calls=tool_calls)

    @classmethod
    def tool(cls, tool_call_id: str, content: str, name: str | None = None) -> Message:
        return cls(role="tool", content=content, tool_call_id=tool_call_id, name=name)


class ToolCall(BaseModel):
    """模型请求调用某个工具。"""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    #: 模型给的参数不是合法 JSON 时，原文放这里，方便排查是哪一步出的问题
    raw_arguments: str | None = None


class FinishReason(StrEnum):
    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"


class Usage(BaseModel):
    """token 用量。成本归因、模型路由效果对比都靠它。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


class ModelResponse(BaseModel):
    """一次模型调用的完整结果。"""

    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    cost: Decimal = Decimal("0")
    latency_ms: int = 0
    model: str = ""
    provider: str = ""
    finish_reason: FinishReason = FinishReason.STOP
    #: 结构化输出修复了几轮（0 表示一次就对）
    repair_rounds: int = 0
    #: 原始响应，出问题时用来排查
    raw: dict[str, Any] | None = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class ModelError(Exception):
    """模型调用失败。

    ``retryable`` 用来区分"再试一次可能好"和"再试也没用"：
    * 限流 / 超时 / 5xx → 可以重试
    * 参数错误 / 内容被安全策略拦截 → 重试没意义
    """

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status
