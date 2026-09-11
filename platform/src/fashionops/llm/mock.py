"""Mock 模型客户端：零 API Key 跑通全链路。

## 为什么这个不是"玩具"

Agent 项目在开发期最容易卡住的两件事：模型 API 要花钱、要网络、还有限流。
一旦开发环境依赖真实模型，"跑一遍看看"这个动作就变得有成本，
于是没人愿意频繁验证，问题被推迟到很晚才暴露。

Mock 客户端解决的就是这个：**不填任何 Key 也能把 9 个 Agent 的完整链路跑通**。

## 关键能力：按 schema 造出"看起来对"的数据

如果 mock 只会返回 ``{"ok": true}`` 或者一段瞎编的文本，那下游的
解析、校验、展示代码全都测不到。所以这里的核心是一个
**从 Pydantic 模型反向生成合规实例**的生成器：

* 它读字段名和类型，给出形状正确的值（``title`` 给标题、``reason`` 给一句理由、
  ``sku_code`` 给 SKU 编码）
* 枚举只取合法成员，``Literal`` 只取声明的字面量
* 嵌套模型递归生成，列表给 2~3 项
* 同一份输入永远给同一份输出（按内容哈希播种），保证可复现

这样 Agent 的 schema 校验、字段访问、前端渲染全都能真的跑起来，
换到真实模型时不会"一接上就崩"。

## 怎么模拟多步流程

用 ``script`` 指定一串预定回复，按顺序消费。这让我们能在
**完全不联网**的情况下测多轮工具调用 / 反思迭代这类控制流。
script 用完之后回退到 schema 生成。
"""

from __future__ import annotations

import hashlib
import json
import random
import time
import types
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, Field

from fashionops.llm.pricing import estimate_cost
from fashionops.llm.types import (
    FinishReason,
    Message,
    ModelResponse,
    ToolCall,
    Usage,
)

#: 联合类型的两种形态。
#:
#: Python 3.10 引入 PEP 604 后，``X | None`` 得到的是 ``types.UnionType``，
#: 而 ``Optional[X]`` / ``Union[X, None]`` 得到的是 ``typing.Union`` ——
#: **两者不是同一个对象**，只判断 ``is Union`` 会让 ``datetime | None``
#: 这种写法整个掉进兜底分支。这个坑很隐蔽：报错现场在很远的地方，
#: 表现为"某个字段莫名其妙变成了一段文字"。
UNION_TYPES = (Union, types.UnionType)

# ---------------------------------------------------------------------------
# 按字段名猜内容
# ---------------------------------------------------------------------------
#: (字段名里的关键词, 生成函数)。顺序有意义：先匹配到的先用。
_STRING_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("reason", "rationale", "why", "explain", "justify"),
     "该款近 30 天动销率 68%，高于类目均值 23 个百分点，且竞品价格带存在空档"),
    (("summary", "conclusion", "comment", "insight"),
     "综合评分领先，建议优先排产；注意库存深度不足以支撑首波投放"),
    (("title", "name", "subject"),
     "秋季法式醋酸连衣裙收腰显瘦"),
    (("sku",), "SKU00001"),
    (("spu",), "SPU20260001"),
    (("code", "no", "number"),
     "T260911001"),
    (("url", "link", "href"), "https://mock-ecom.example.com/item/1000001"),
    (("email", "mail"), "ops@example.com"),
    (("color",), "米白"),
    (("size",), "M"),
    (("category", "category_name"), "女装"),
    (("season",), "秋季"),
    (("city", "province"), "浙江"),
    (("phone", "mobile"), "13800000000"),
    (("stage", "step", "phase"), "analyze"),
    (("status",), "succeeded"),
    (("priority",), "normal"),
]


def _guess_string(field_name: str, description: str | None) -> str:
    lowered = field_name.lower()
    for keywords, sample in _STRING_HINTS:
        if any(k in lowered for k in keywords):
            return sample
    # 字段本身带了 description 就用它 —— 那是作者写给人看的解释，当样本很合适
    if description and len(description) <= 60:
        return description
    return f"{field_name} 示例内容"


# ---------------------------------------------------------------------------
# schema → 假实例
# ---------------------------------------------------------------------------
#: 模型嵌套的最大层数。
#:
#: 超过这个层数就停止向里递归，返回 ``None`` —— 让上层用 ``Optional`` 的
#: ``None`` 兜住（自引用模型形如 ``child: Node | None``，恰好可以）。
MAX_NESTING_DEPTH = 6


def fake_value(  # noqa: C901, PLR0911, PLR0912
    annotation: Any,
    field_name: str,
    description: str | None,
    rng: random.Random,
    *,
    depth: int = 0,
) -> Any:
    """按类型注解造一个值。

    ``depth`` 只计**模型嵌套层数**，不计函数调用层数。

    这里踩过两个坑：

    1. 最初每递归一次就 +1，于是 ``SelectionResult → Candidate → PricePoint``
       这种两层嵌套加上容器各占一层，深度直接到 5 被截断成 ``None``，
       整份数据通不过校验。要防的是自引用无限递归，而容器不是模型嵌套。
    2. 截断必须在**嵌套模型**那一个分支上做，不能放在函数入口 ——
       放入口的话 ``name: str`` 这种必需字段也会被填成 ``None``，
       而它并不接受 None。
    """
    origin = get_origin(annotation)

    # ---- Optional[X] / X | None：**生成 X 而不是 None** ----
    # 故意不返回 None：下游代码需要拿到真实数据才能跑到真正的分支，
    # 全是 None 的话很多逻辑压根执行不到。
    if origin in UNION_TYPES:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if args:
            return fake_value(args[0], field_name, description, rng, depth=depth)
        return None

    if origin is Literal:
        return get_args(annotation)[0]

    # ---- 容器：深度不变，因为容器不是模型嵌套 ----
    if origin in (list, set, frozenset, tuple):
        args = get_args(annotation)
        item_type = args[0] if args else str
        count = rng.randint(2, 3)
        return [
            fake_value(item_type, f"{field_name}_{i}", None, rng, depth=depth)
            for i in range(count)
        ]

    if origin is dict:
        args = get_args(annotation)
        value_type = args[1] if len(args) > 1 else str
        return {
            f"key{i}": fake_value(value_type, f"{field_name}_{i}", None, rng, depth=depth)
            for i in range(2)
        }

    # ---- 标量 ----
    if annotation is str or annotation is Any or annotation is None:
        return _guess_string(field_name, description)

    if annotation is bool:
        return True

    if annotation is int:
        return rng.randint(1, 100)

    if annotation is float:
        return round(rng.uniform(1.0, 100.0), 2)

    if annotation is Decimal:
        return Decimal(str(round(rng.uniform(10.0, 9999.0), 2)))

    if annotation is datetime:
        return datetime(2026, 9, 11, 21, 0, 0) - timedelta(hours=rng.randint(0, 72))

    if annotation is date:
        return date(2026, 9, 11) - timedelta(days=rng.randint(0, 30))

    # ---- 枚举 ----
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return list(annotation)[0]

    # ---- 嵌套模型 ----
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        # 截断只在这里做：再往里就是自引用模型的无限递归了。
        # 返回 None 让上层的 Optional 兜住；如果字段是必需且非 Optional 的
        # 自引用（``child: Node``），那 schema 本身就是无法满足的，
        # 让校验报错比默默返回残缺数据好。
        if depth >= MAX_NESTING_DEPTH:
            return None
        return fake_instance(annotation, rng, depth=depth + 1)

    # 兜底：交给字符串生成，至少不会让整个流程崩
    return _guess_string(field_name, description)


def fake_instance(schema: type[BaseModel], rng: random.Random, *, depth: int = 0) -> dict[str, Any]:
    """按模型定义造一个合法实例（返回 dict，可直接 ``schema(**d)``）。"""
    result: dict[str, Any] = {}
    for name, field_info in schema.model_fields.items():
        result[name] = fake_value(
            field_info.annotation,
            name,
            field_info.description,
            rng,
            depth=depth,
        )
    return result


def build_instance(schema: type[BaseModel], *, session_seed: str = "") -> BaseModel:
    """造一个**确实能通过校验**的实例。

    播种方式：用 schema 名 + 本次会话标识做哈希。
    同一份输入 → 同一份输出（可复现）；
    换个会话 → 数据有变化（不会每次都看到一模一样的数字）。
    """
    digest = hashlib.sha256(f"{schema.__name__}::{session_seed}".encode()).hexdigest()
    rng = random.Random(int(digest[:16], 16))
    return schema(**fake_instance(schema, rng))


# ---------------------------------------------------------------------------
# 脚本化回复
# ---------------------------------------------------------------------------
class MockReply(BaseModel):
    """一条预设回复。"""

    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)


class MockModelClient:
    """假模型客户端。

    ``script`` 为空时，纯靠 schema 生成；给一串 ``MockReply``
    就能模拟多轮工具调用这样复杂的控制流。
    """

    provider = "mock"

    def __init__(
        self,
        model: str = "mock-model",
        *,
        script: list[MockReply] | None = None,
        latency_ms: int = 12,
        session_seed: str = "default",
    ) -> None:
        self.model = model
        self.script: list[MockReply] = list(script or [])
        self.latency_ms = latency_ms
        self.session_seed = session_seed
        self.calls: list[list[Message]] = []  # 记录收到的请求，测试断言用

    # ---- 主接口 ----
    def complete(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: type[BaseModel] | None = None,
        temperature: float | None = None,  # noqa: ARG002 - 保持签名一致
        model: str | None = None,
        **_: Any,
    ) -> ModelResponse:
        started = time.perf_counter()
        self.calls.append(messages)
        used_model = model or self.model

        # 1) 有脚本就按脚本走
        reply = self.script.pop(0) if self.script else None

        if reply is not None:
            content = reply.content
            tool_calls = reply.tool_calls
        # 2) 给了 schema → 造一个合规实例
        elif response_schema is not None:
            instance = build_instance(response_schema, session_seed=self.session_seed)
            content = instance.model_dump_json(indent=None)
            tool_calls = []
        # 3) 给了工具目录且对话还没出现过工具结果 → 模拟"模型决定先查数据"
        elif tools and not any(m.role == "tool" for m in messages):
            tool_calls = [self._pick_tool(tools, messages)]
            content = ""
        else:
            content = "（mock 模型）已完成本轮分析。"
            tool_calls = []

        prompt_tokens = _approx_tokens(messages)
        completion_tokens = _approx_tokens_text(content) + 20 * len(tool_calls)
        usage = Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )

        # 真的睡一下。虽然是假的，但让调用方感受到"模型是有延迟的"，
        # 才能暴露出并发、超时、进度反馈这些只在真实延迟下才出现的问题。
        elapsed_target = self.latency_ms / 1000
        actual = time.perf_counter() - started
        if actual < elapsed_target:
            time.sleep(elapsed_target - actual)

        return ModelResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            cost=estimate_cost(used_model, prompt_tokens, completion_tokens),
            latency_ms=int((time.perf_counter() - started) * 1000),
            model=used_model,
            provider=self.provider,
            finish_reason=FinishReason.TOOL_CALLS if tool_calls else FinishReason.STOP,
        )

    def _pick_tool(self, tools: list[dict[str, Any]], messages: list[Message]) -> ToolCall:
        """挑一个工具来"调用"。

        策略：优先选**不需要参数**或参数全都有默认值的工具 ——
        这样生成的 tool_call 一定能通过网关的参数校验，
        不会因为 mock 自己造了非法参数而让整条链路红掉。
        """
        prompt = " ".join(m.content for m in messages if m.content).lower()

        best: dict[str, Any] | None = None
        # 初值必须取负无穷而不是 -1：得分可能正好是 -1
        # （名称没命中关键词得 0 分，再减去 1 个必填参数），
        # 写 -1 的话 `-1 > -1` 为假，一个工具都选不出来。
        best_score = float("-inf")
        for schema in tools:
            func = schema.get("function", schema)
            name = str(func.get("name", ""))
            required = func.get("parameters", {}).get("required", []) or []
            # 名称里出现关键词的优先
            score = sum(1 for token in name.replace("__", ".").split(".") if token in prompt)
            score -= len(required)  # 必填参数越少越好
            if score > best_score:
                best, best_score = schema, score

        if best is None:  # tools 为空列表时的兵底
            return ToolCall(id="call_empty", name="", arguments={})
        func = best.get("function", best)
        required = func.get("parameters", {}).get("required", []) or []
        properties = func.get("parameters", {}).get("properties", {}) or {}

        args = {
            key: _default_for(properties.get(key, {}))
            for key in required
        }
        return ToolCall(
            id=f"call_{random.Random(hash(func.get('name', ''))).randrange(10**8):08d}",
            name=str(func.get("name", "")),
            arguments=args,
        )


def _default_for(json_schema: dict[str, Any]) -> Any:
    """按 JSON Schema 片段给一个能被 Pydantic 接受的参数值。"""
    if "enum" in json_schema:
        return json_schema["enum"][0]
    if "default" in json_schema:
        return json_schema["default"]

    match json_schema.get("type"):
        case "string":
            return "mock"
        case "integer":
            return 1
        case "number":
            return 1.0
        case "boolean":
            return False
        case "array":
            return []
        case "object":
            return {}
        case _:
            # anyOf / $ref 之类：给个字符串通常都能过
            return "mock"


def _approx_tokens(messages: list[Message]) -> int:
    return sum(_approx_tokens_text(m.content) for m in messages) + 4 * len(messages)


def _approx_tokens_text(text: str) -> int:
    """粗略估 token 数。

    真实分词要装 tiktoken 并按模型选编码，这里只要一个量级正确的数字 ——
    mock 场景下成本怎么算都是 0，没必要在这上面花时间。
    中文大致 1 字 ≈ 1 token，英文 4 字符 ≈ 1 token。
    """
    if not text:
        return 0
    chinese = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - chinese
    return chinese + max(1, other // 4)


def dumps_for_prompt(obj: Any) -> str:
    """把对象序列化成喂给模型的紧凑 JSON。"""
    return json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":"))
