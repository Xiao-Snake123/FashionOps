"""模型连通性自检 —— 填完 API Key 后先跑这个，别等到 Agent 跑起来才发现问题。

检查项::

    1. 配置是否自洽（provider 和 model 对得上吗）
    2. 网络能不能通（顺带告诉你有没有被系统代理拦）
    3. 普通对话调用
    4. 结构化输出（Agent 每个节点都靠它）
    5. 工具调用 / Function Calling
    6. 成本估算（按一个典型任务量算一次要花多少钱）

运行::

    uv run python scripts/check_model.py

不会打印 API Key（只显示前 4 位 + 长度）。
"""

from __future__ import annotations

import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from pydantic import BaseModel, Field  # noqa: E402

from fashionops.config import get_settings  # noqa: E402
from fashionops.llm import (  # noqa: E402
    Message,
    ModelError,
    estimate_cost,
    get_model_client,
    price_of,
    run_tool_loop,
    structured_call,
)
from fashionops.net import proxy_report  # noqa: E402

#: provider -> (推荐的 base_url, 常用模型名)
PROVIDERS: dict[str, tuple[str, list[str]]] = {
    "deepseek": ("https://api.deepseek.com/v1", ["deepseek-chat", "deepseek-reasoner"]),
    "qwen": (
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ["qwen-plus", "qwen-turbo", "qwen-max"],
    ),
    "zhipu": ("https://open.bigmodel.cn/api/paas/v4", ["glm-4-plus", "glm-4-flash"]),
    "openai": ("（SDK 默认）", ["gpt-4o-mini", "gpt-4o"]),
    "mock": ("不适用", ["mock-model"]),
}

OK = "  [OK]  "
FAIL = "  [FAIL]"
WARN = "  [WARN]"


class SelectionResult(BaseModel):
    """结构化输出的测试 schema，故意带约束，能顺带验证 mock 生成器。"""

    summary: str = Field(description="一句话结论")
    count: int = Field(default=3, ge=1, le=10, description="选出的款数")
    top_sku: str = Field(default="SKU00001", description="排第一的 SKU 编码")


def mask(secret: str | None) -> str:
    if not secret:
        return "（未配置）"
    if len(secret) <= 8:
        return f"{'*' * len(secret)}（长度 {len(secret)}）"
    return f"{secret[:4]}{'*' * 8}…（长度 {len(secret)}）"


def header(text: str) -> None:
    print()
    print("=" * 72)
    print(f"  {text}")
    print("=" * 72)


def main() -> int:
    settings = get_settings()

    # ------------------------------------------------------------------
    header("1. 配置")
    # ------------------------------------------------------------------
    print(f"  provider    : {settings.model_provider}")
    print(f"  model       : {settings.model_name}")
    print(f"  fast model  : {settings.model_name_fast}")
    print(f"  api key     : {mask(settings.model_api_key)}")
    print(f"  base url    : {settings.model_base_url or '（用 provider 默认值）'}")
    print(f"  temperature : {settings.model_temperature}")

    problems: list[str] = []
    recommended = PROVIDERS.get(settings.model_provider)
    if recommended:
        base_url, models = recommended
        print(f"  该 provider 的常用模型: {', '.join(models)}")
        if settings.model_provider != "mock" and settings.model_name not in models:
            # provider 和 model 对不上是最常见的配置错误：报错信息会很含糊，
            # 但根因其实一眼就能看出来，所以在这里提前拦住
            problems.append(
                f"model_name={settings.model_name!r} 不像 {settings.model_provider} 的模型，"
                f"常见的是 {', '.join(models)}"
            )
            print(f"{WARN} 模型名和 provider 可能不匹配：{problems[-1]}")
        if not settings.model_base_url and base_url.startswith("http"):
            print(f"  （会自动使用 {base_url}）")

    if settings.model_provider == "mock":
        print(f"{WARN} 当前是 mock 模式：不会真的调用外部模型，返回的是结构化假数据。")
        print("        要接真实模型，请在 platform\\.env 里改这两行：")
        print("          FASHIONOPS_MODEL_PROVIDER=deepseek")
        print("          FASHIONOPS_MODEL_API_KEY=你的key")
        print()
        print("        注意：改完 .env 后当前进程不会自动重载，需要重跑本脚本。")

    if settings.model_provider != "mock" and not settings.model_api_key:
        problems.append("provider 不是 mock，但 api_key 是空的 —— 会自动降级成 mock")

    # ------------------------------------------------------------------
    header("2. 网络与代理")
    # ------------------------------------------------------------------
    report = proxy_report()
    upstream = report["upstream"]
    if upstream:
        print(f"{WARN} 检测到系统代理：{upstream}")
        print("        外部模型 API 会走这个代理（如果代理挂了，调用就会超时）")
        print("        如果调用失败，先确认代理是否正常工作、节点是否可用。")
    else:
        print(f"{OK} 没有检测到系统代理，外网直连")
    print(f"  本地绕过（NO_PROXY）: {report['no_proxy']}")
    print("        本地服务（MySQL / Redis / Qdrant）已自动绕过代理。")
    print("        这一步很关键 —— 不绕过的话连 127.0.0.1 都会被丢给代理，")
    print("        表现成「本地服务返回 502」，而服务日志里什么都看不到。")

    # ------------------------------------------------------------------
    header("3~5. 真实调用测试")
    # ------------------------------------------------------------------
    try:
        client = get_model_client(settings)
    except Exception as exc:  # noqa: BLE001
        print(f"{FAIL} 客户端构造失败：{type(exc).__name__}: {exc}")
        return 1

    print(f"  客户端类型：{type(client).__name__}")
    failures = 0

    # ---- 3. 普通对话 ----
    print()
    print("  [3] 普通对话")
    try:
        started = time.perf_counter()
        response = client.complete(
            [Message.user("用一句话说明服装电商的退货率为什么通常高于其他品类。")]
        )
        elapsed = int((time.perf_counter() - started) * 1000)
        print(f"{OK} {elapsed} ms | {response.usage.total_tokens} tokens | ¥{response.cost}")
        print(f"        模型回答：{response.content.strip()[:120]}")
    except ModelError as exc:
        failures += 1
        print(f"{FAIL} {exc}")
        if exc.status in (401, 403):
            print("        这个错基本可以确定是 API Key 不对：过期、复制少了字符、")
            print("        或者 key 和 provider 不是一家的。")
        return 1
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"{FAIL} {type(exc).__name__}: {exc}")
        print("        如果提示连接超时/拒绝，而上面又检测到了系统代理，")
        print("        多半是代理的问题 —— 试试关掉代理或换个节点。")
        return 1

    # ---- 4. 结构化输出 ----
    print()
    print("  [4] 结构化输出（Agent 每个节点都依赖它）")
    try:
        result = structured_call(
            client,
            [Message.user("随便给一个选品结论，选出 3 款，第一名是 SKU00042。")],
            SelectionResult,
        )
        if result.ok:
            value: SelectionResult = result.value  # type: ignore[assignment]
            print(f"{OK} 一次通过 | 尝试 {result.attempted} 次 | 修复 {result.repair_rounds} 轮")
            print(f"        解析结果：{value.model_dump()}")
        else:
            failures += 1
            print(f"{FAIL} 修了 {result.attempted} 轮仍未通过 schema 校验")
            print(f"        错误：{' | '.join(result.errors[-3:])}")
            print("        这个能力不行的话，Agent 节点会大面积失败 —— 优先排查。")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"{FAIL} {type(exc).__name__}: {exc}")

    # ---- 5. 工具调用 ----
    print()
    print("  [5] 工具调用 / Function Calling")
    tools = [
        {
            "type": "function",
            "function": {
                "name": "erp__query_inventory",
                "description": "按 SKU 编码查询当前可用库存",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sku_codes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "SKU 编码列表",
                        }
                    },
                    "required": ["sku_codes"],
                },
            },
        }
    ]
    executed: list[str] = []

    def runner(name: str, args: dict) -> dict:
        executed.append(name)
        return {"ok": True, "data": {"SKU00042": 128}}

    try:
        loop = run_tool_loop(
            client,
            [Message.user("帮我查一下 SKU00042 还有多少库存。")],
            tools=tools,
            tool_runner=runner,
            max_steps=4,
        )
        if executed:
            print(f"{OK} 模型发起了工具调用：{executed}")
            print(f"        轮数 {loop.steps} | 累计 {loop.total_usage.total_tokens} tokens "
                  f"| ¥{loop.total_cost}")
            print(f"        最终回答：{loop.content.strip()[:120]}")
        else:
            print(f"{WARN} 模型没有发起工具调用，直接回答了：{loop.content.strip()[:120]}")
            print("        部分模型/参数下这是正常现象，功能本身可用。")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"{FAIL} {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    header("6. 成本估算")
    # ------------------------------------------------------------------
    inp, out = price_of(settings.model_name)
    print(f"  {settings.model_name} 价格：输入 ¥{inp}/百万 token，输出 ¥{out}/百万 token")
    print()
    print("  一次典型任务的量级估算（选品 + 数据分析 + 邮件通知 ≈ 15 次调用）：")
    for label, prompt_tokens, completion_tokens, calls in [
        ("轻量（只做选品）", 3000, 600, 4),
        ("标准（3 Agent 链路）", 6000, 1200, 15),
        ("完整（9 Agent 全链路）", 12000, 2500, 60),
    ]:
        per_call = estimate_cost(settings.model_name, prompt_tokens, completion_tokens)
        print(
            f"    {label:<22} 单次 ¥{per_call:.6f}"
            f"  ×  {calls:>2} 次  =  ¥{per_call * calls:.4f}"
        )
    print()
    print("  这是按「每次都把全量上下文发过去」估的上界；")
    print("  实际会因为上下文裁剪和模型路由（简单任务走 fast 模型）明显低于这个数。")

    # ------------------------------------------------------------------
    header("结论")
    # ------------------------------------------------------------------
    if failures:
        print(f"{FAIL} 有 {failures} 项没通过，先解决它们再跑 Agent。")
        return 1

    if settings.model_provider == "mock":
        print(f"{WARN} 全部通过，但当前是 mock 模式 —— 上面测的是假模型。")
        print("        填好 .env 里的 provider 和 api key 后重跑本脚本，")
        print("        看到「客户端类型：OpenAICompatClient」才算真的接上了。")
    else:
        print(f"{OK} 全部通过，可以接真实模型跑 Agent 了。")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
