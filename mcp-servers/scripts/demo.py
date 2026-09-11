"""端到端演示：通过 MCP 协议真实调用 office-docs 的全部工具。

用法::

    uv run python scripts/demo.py

脚本会：
1. 列出服务器注册的所有工具
2. 依次调用各工具生成 Word / Excel / PPT / Markdown
3. 把生成的文件读回来，验证往返一致性
4. 演示路径沙箱拦截与错误提示
5. 打印审计日志的最后几行

这是一份可直接放进 README 或面试演示的脚本。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# 让脚本在未安装包时也能直接运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Windows 控制台默认 GBK，输出中文/符号会炸。统一强制 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

DEMO_ROOT = Path(__file__).resolve().parents[1] / "workspace"

os.environ.setdefault("OFFICE_MCP_WORKSPACE_ROOT", str(DEMO_ROOT))
os.environ.setdefault("OFFICE_MCP_AUDIT_LOG", str(DEMO_ROOT / "audit.jsonl"))

from office_mcp.docs.server import mcp  # noqa: E402

SEP = "─" * 72


def title(text: str) -> None:
    print(f"\n{SEP}\n  {text}\n{SEP}")


async def call(tool: str, **kwargs: object) -> dict:
    """调用工具并解析返回的 JSON。"""
    result = await mcp.call_tool(tool, kwargs)
    payload = json.loads(result.content[0].text)
    mark = "✓" if payload.get("ok") else "✗"
    summary = {k: v for k, v in payload.items() if k not in {"markdown", "result"}}
    print(f"  {mark} {tool}({_compact(kwargs)})")
    print(f"      → {_compact(summary)}")
    if not payload.get("ok"):
        print(f"      hint: {payload.get('hint')}")
    return payload


def _compact(data: object, limit: int = 150) -> str:
    text = json.dumps(data, ensure_ascii=False, default=str)
    return text if len(text) <= limit else f"{text[:limit]}…"


async def main() -> int:
    failures: list[str] = []

    # ---------------------------------------------------------------- #
    title("1. 列出服务器注册的工具")
    tools = await mcp.list_tools()
    print(f"  共 {len(tools)} 个工具：")
    for tool in tools:
        first_line = (tool.description or "").splitlines()[0][:52]
        print(f"    • {tool.name:24s} {first_line}")

    # ---------------------------------------------------------------- #
    title("2. 生成 Word 周报")
    docx = await call(
        "create_word_document",
        filename="周报.docx",
        title="2026 年第 37 周工作周报",
        blocks=[
            {"kind": "heading", "text": "一、本周完成", "level": 1},
            {
                "kind": "bullets",
                "items": [
                    "完成办公 Agent 的 MCP 工具层设计",
                    "接入文档处理能力（Word/Excel/PPT）",
                    "补充路径沙箱与审计日志",
                ],
            },
            {"kind": "heading", "text": "二、关键指标", "level": 1},
            {
                "kind": "table",
                "rows": [
                    ["指标", "上周", "本周", "变化"],
                    ["任务成功率", "62%", "84%", "+22pt"],
                    ["平均步数", "11.3", "7.8", "-3.5"],
                    ["单任务成本", "¥1.42", "¥0.84", "-41%"],
                ],
            },
            {"kind": "heading", "text": "三、下周计划", "level": 1},
            {
                "kind": "numbered",
                "items": [
                    "接入邮件 MCP 服务器",
                    "补齐日历读写能力",
                    "搭建 τ³-bench 评测流水线",
                ],
            },
        ],
    )
    if not docx.get("ok"):
        failures.append("create_word_document")

    # ---------------------------------------------------------------- #
    title("3. 生成 Excel 数据分析表")
    xlsx = await call(
        "create_excel_workbook",
        filename="指标看板.xlsx",
        sheets=[
            {
                "name": "核心指标",
                "rows": [
                    ["日期", "任务数", "成功率", "平均步数", "成本(元)"],
                    ["2026-09-01", 120, 0.62, 11.3, 170.4],
                    ["2026-09-05", 145, 0.71, 9.6, 152.1],
                    ["2026-09-10", 168, 0.84, 7.8, 141.1],
                ],
            },
            {
                "name": "失败归因",
                "rows": [
                    ["归因类型", "占比"],
                    ["工具参数错误", 0.42],
                    ["用错工具", 0.23],
                    ["非预期动作", 0.18],
                    ["目标未完成", 0.17],
                ],
            },
        ],
    )
    if not xlsx.get("ok"):
        failures.append("create_excel_workbook")

    # ---------------------------------------------------------------- #
    title("4. 生成 PPT 汇报")
    pptx = await call(
        "create_presentation",
        filename="项目汇报.pptx",
        title="办公 Agent 项目汇报",
        slides=[
            {
                "title": "项目背景",
                "bullets": [
                    "企业内部办公场景碎片化",
                    "通用大模型无法直接操作系统",
                    "需要一个能真正执行任务的 Agent",
                ],
                "notes": "强调从对话到执行的转变",
            },
            {
                "title": "技术方案",
                "bullets": [
                    "DeerFlow（LangGraph）作为 Agent 运行时",
                    "MCP 协议对接企业系统",
                    "OpenTelemetry 全链路可观测",
                    "τ³-bench 量化评测",
                ],
            },
            {
                "title": "效果",
                "bullets": [
                    "任务成功率 62% → 84%",
                    "平均步数 11.3 → 7.8",
                    "单任务成本下降 41%",
                ],
            },
        ],
    )
    if not pptx.get("ok"):
        failures.append("create_presentation")

    # ---------------------------------------------------------------- #
    title("5. 写 Markdown 并回读验证")
    md = await call(
        "write_text_file",
        filename="实验记录.md",
        content=(
            "# 实验记录\n\n"
            "## 对照组\n- 未加路径沙箱与参数校验\n\n"
            "## 实验组\n- 加入 MCP 工具 schema 约束\n- 加入错误 hint 回传\n\n"
            "> 结论：参数错误类失败下降 42%。\n"
        ),
    )
    if not md.get("ok"):
        failures.append("write_text_file")

    title("6. 列出工作区文件")
    listing = await call("list_files", pattern="*", recursive=True)
    if not listing.get("ok"):
        failures.append("list_files")

    title("7. 回读生成的文档（验证往返一致）")
    for name in ("outputs/周报.docx", "outputs/指标看板.xlsx", "outputs/项目汇报.pptx"):
        payload = await call("read_document", path=name)
        if payload.get("ok"):
            preview = payload["markdown"].splitlines()[:3]
            for line in preview:
                print(f"      │ {line[:70]}")
        else:
            failures.append(f"read_document:{name}")

    # ---------------------------------------------------------------- #
    title("8. 安全验证：路径沙箱拦截越权访问")
    blocked = await call("read_document", path="../../../Windows/win.ini")
    if blocked.get("ok"):
        failures.append("path_sandbox")
        print("      ⚠️ 越权访问未被拦截！")
    else:
        print(f"      ✓ 越权访问被正确拦截（code={blocked.get('code')}）")

    title("9. 错误提示验证：读取不存在的文件")
    missing = await call("read_document", path="outputs/不存在的文件.docx")
    if missing.get("ok"):
        failures.append("missing_file_check")

    # ---------------------------------------------------------------- #
    title("10. 审计日志")
    audit_path = Path(os.environ["OFFICE_MCP_AUDIT_LOG"])
    if audit_path.exists():
        lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
        print(f"  共 {len(lines)} 条记录，最近 3 条：")
        for line in lines[-3:]:
            record = json.loads(line)
            print(
                f"    • {record['event']:10s} {record.get('tool', '-'):22s} "
                f"{record.get('status', '-'):6s} {record.get('duration_ms', '-')}ms"
            )
    else:
        print("  (未生成审计日志)")
        failures.append("audit_log")

    # ---------------------------------------------------------------- #
    print(f"\n{SEP}")
    if failures:
        print(f"  ✗ 有 {len(failures)} 项失败：{', '.join(failures)}")
        return 1
    print("  ✓ 全部通过")
    print(f"  产物目录：{DEMO_ROOT / 'outputs'}")
    print(SEP)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
