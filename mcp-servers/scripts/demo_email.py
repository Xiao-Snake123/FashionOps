"""邮件 MCP 服务器端到端演示。

**不需要真实邮箱**：整个演示跑在 ``dry_run`` 模式下，
重点展示安全闸门在各种攻击/误操作场景下如何正确地拦住 Agent。

用法::

    uv run python scripts/demo_email.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

DEMO_ROOT = Path(__file__).resolve().parents[1] / "workspace"
DEMO_ROOT.mkdir(parents=True, exist_ok=True)

# 演示用配置：dry_run + 白名单 + 小配额，便于把各条策略都触发一遍
os.environ["OFFICE_MCP_WORKSPACE_ROOT"] = str(DEMO_ROOT)
os.environ["OFFICE_MCP_AUDIT_LOG"] = str(DEMO_ROOT / "audit-email.jsonl")
os.environ["OFFICE_MCP_DRY_RUN"] = "1"
os.environ["OFFICE_MCP_MAIL_ADDRESS"] = "agent@corp.example.com"
os.environ["OFFICE_MCP_MAIL_SEND_ALLOWLIST"] = '["@corp.example.com"]'
os.environ["OFFICE_MCP_MAIL_RATE_LIMIT_PER_MINUTE"] = "2"

from office_mcp.common.config import Settings  # noqa: E402
from office_mcp.email.guard import SendGuard  # noqa: E402
from office_mcp.email.server import mcp  # noqa: E402

SEP = "─" * 74


def section(text: str) -> None:
    print(f"\n{SEP}\n  {text}\n{SEP}")


async def call(tool: str, **kwargs: object) -> dict:
    result = await mcp.call_tool(tool, kwargs)
    payload = json.loads(result.content[0].text)
    ok = payload.get("ok")
    print(f"  {'✓' if ok else '✗'} {tool}  →  {_brief(payload)}")
    if not ok:
        print(f"      code: {payload.get('code')}")
        print(f"      hint: {payload.get('hint')}")
    return payload


def _brief(payload: dict, limit: int = 130) -> str:
    keep = {k: v for k, v in payload.items() if k not in {"ok", "hint"}}
    text = json.dumps(keep, ensure_ascii=False, default=str)
    return text if len(text) <= limit else f"{text[:limit]}…"


async def main() -> int:
    problems: list[str] = []

    # ---------------------------------------------------------------- #
    section("1. 注册的工具")
    for tool in await mcp.list_tools():
        print(f"    • {tool.name:22s} {(tool.description or '').splitlines()[0][:46]}")

    # ---------------------------------------------------------------- #
    section("2. 能力与策略状态（不含任何凭证）")
    status = await call("get_mail_status")
    print(f"      policy.allowlist = {status.get('policy', {}).get('allowlist')}")
    print(f"      send_enabled     = {status.get('send_enabled')}")
    if "password" in json.dumps(status):
        problems.append("凭证泄露")

    # ---------------------------------------------------------------- #
    section("3. 正常发送（dry_run，不产生真实副作用）")
    sent = await call(
        "send_email",
        to=["alice@corp.example.com"],
        subject="本周周报",
        body_text="你好，附件是本周周报，请查收。",
    )
    if not sent.get("ok") or not sent.get("dry_run"):
        problems.append("dry_run 发送")

    # ---------------------------------------------------------------- #
    section("4. 闸门拦截：收件人在白名单之外")
    await call(
        "send_email",
        to=["stranger@outside.com"],
        subject="尝试外发",
        body_text="这封应该被拦住。",
    )

    # ---------------------------------------------------------------- #
    section("5. 闸门拦截：把目标塞进 cc 绕过检查")
    await call(
        "send_email",
        to=["ok@corp.example.com"],
        cc=["evil@outside.com"],
        subject="cc 绕过尝试",
        body_text="cc 也必须被检查。",
    )

    # ---------------------------------------------------------------- #
    section("6. 闸门拦截：敏感文件当附件外发")
    (DEMO_ROOT / "id_rsa").write_text("-----BEGIN PRIVATE KEY-----", encoding="utf-8")
    await call(
        "send_email",
        to=["alice@corp.example.com"],
        subject="带附件的尝试",
        body_text="见附件。",
        attachments=["id_rsa"],
    )

    # ---------------------------------------------------------------- #
    section("7. 闸门拦截：附件路径越界")
    await call(
        "send_email",
        to=["alice@corp.example.com"],
        subject="路径越界尝试",
        body_text="见附件。",
        attachments=["../../../Windows/win.ini"],
    )

    # ---------------------------------------------------------------- #
    section("8. 闸门拦截：发送频率超限")
    for _ in range(2):
        await call(
            "send_email",
            to=["alice@corp.example.com"],
            subject="配额消耗",
            body_text="消耗配额。",
        )
    await call(
        "send_email",
        to=["alice@corp.example.com"],
        subject="第四封",
        body_text="这封应该被限流拦住。",
    )

    # ---------------------------------------------------------------- #
    section("9. 被拒时的替代路径：存草稿")
    draft = await call(
        "save_email_draft",
        to=["stranger@outside.com"],
        subject="无法直接发送的邮件",
        body_text="收件人不在白名单，先存草稿交人工确认。",
    )
    if draft.get("ok"):
        path = DEMO_ROOT / draft["path"]
        size = path.stat().st_size if path.exists() else 0
        print(f"      草稿文件存在: {path.exists()}  ({size} 字节)")

    # ---------------------------------------------------------------- #
    section("10. fail-closed 验证：白名单为空时谁都发不出去")
    bare = SendGuard(Settings(workspace_root=DEMO_ROOT, mail_send_allowlist=()))
    try:
        bare.check(to=["anyone@anywhere.com"], cc=[], bcc=[], attachments=[])
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", "?")
        print(f"  ✓ 被拒绝  code={code}")
        print(f"      {getattr(exc, 'message', exc)}")
        print(f"      hint: {getattr(exc, 'hint', '')}")
    else:
        problems.append("fail-closed")
        print("  ✗ 未配置白名单时竟然允许发送！")

    # ---------------------------------------------------------------- #
    section("11. 审计日志（最近的拒绝记录）")
    audit = Path(os.environ["OFFICE_MCP_AUDIT_LOG"])
    if audit.exists():
        records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        denied = [r for r in records if r.get("status") == "error"]
        print(f"  总记录 {len(records)} 条，其中被拒 {len(denied)} 条，最近 5 条：")
        for record in denied[-5:]:
            print(
                f"    • {record['tool']:16s} {record.get('error_code', '-'):26s} "
                f"{record.get('duration_ms', '-')}ms"
            )
    else:
        problems.append("审计日志")
        print("  (未生成审计日志)")

    print(f"\n{SEP}")
    if problems:
        print(f"  ✗ 有 {len(problems)} 项异常：{', '.join(problems)}")
        return 1
    print("  ✓ 全部通过")
    print(SEP)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
