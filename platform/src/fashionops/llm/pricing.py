"""模型价格表与成本计算。

## 为什么成本要算到"每一次调用"

§11 的 W10 要交付「token 成本有对比数字」。要给出这个数字，前提是
**每一次模型调用都记了它花了多少钱**，而不是月底看账单估一个。
所以 ``ModelResponse`` 从第一天就带 ``cost`` 字段，并落到 ``agent_runs`` 表。

## 价格的坑

价格按 **每百万 token** 报价，但调用量往往只有几千 token，
用 float 直接算会累积浮点误差（对账时差几分钱就很尴尬）。
所以统一用 ``Decimal`` 全程精确计算，只在展示时转 float。
"""

from __future__ import annotations

from decimal import Decimal

#: 每百万 token 的价格（人民币元）。(输入价, 输出价)
#: 数据会变，更新时改这里一处即可；也可以将来改成从配置或数据库读。
PRICES: dict[str, tuple[Decimal, Decimal]] = {
    # ---- DeepSeek（开发期主力，成本最低）----
    "deepseek-chat": (Decimal("1.0"), Decimal("2.0")),
    "deepseek-reasoner": (Decimal("4.0"), Decimal("16.0")),
    # ---- 通义千问 ----
    "qwen-turbo": (Decimal("0.3"), Decimal("0.6")),
    "qwen-plus": (Decimal("0.8"), Decimal("2.0")),
    "qwen-max": (Decimal("2.4"), Decimal("9.6")),
    # ---- 智谱 ----
    "glm-4-flash": (Decimal("0.0"), Decimal("0.0")),
    "glm-4-plus": (Decimal("5.0"), Decimal("5.0")),
    # ---- OpenAI（仅作对比基准）----
    "gpt-4o-mini": (Decimal("1.1"), Decimal("4.3")),
    "gpt-4o": (Decimal("18.0"), Decimal("72.0")),
    # ---- 本地 mock：不花钱 ----
    "mock-model": (Decimal("0"), Decimal("0")),
}

#: 未知模型按这个价格估，宁可高估也不要低估 —— 成本告警要偏保守
FALLBACK_PRICE: tuple[Decimal, Decimal] = (Decimal("1.0"), Decimal("2.0"))

_PER_MILLION = Decimal(1_000_000)


def price_of(model: str) -> tuple[Decimal, Decimal]:
    """取模型的 (输入价, 输出价)，单位：元/百万 token。

    做前缀匹配：``deepseek-chat-0324`` 这类带版本后缀的名字也能对上
    ``deepseek-chat`` 的价格，不用每出一个版本就改一次表。
    """
    if model in PRICES:
        return PRICES[model]
    for known, price in PRICES.items():
        if model.startswith(known):
            return price
    return FALLBACK_PRICE


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> Decimal:
    """算出一次调用的成本（人民币元，保留 6 位小数）。

    保留 6 位而不是 2 位：单次调用可能只花 0.000123 元，
    四舍五入到分就全变成 0 了，成本统计直接失效。
    存库用 ``Numeric(12, 6)``，和这里对齐。
    """
    inp, out = price_of(model)
    cost = (
        Decimal(prompt_tokens) * inp / _PER_MILLION
        + Decimal(completion_tokens) * out / _PER_MILLION
    )
    return cost.quantize(Decimal("0.000001"))
