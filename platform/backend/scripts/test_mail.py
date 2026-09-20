"""验证「真实 SMTP 投递」这条路能走通。

## 为什么需要这个脚本

四层闸门（总开关 → 白名单/黑名单 → 限流 → 体积）**从来没有任何一封邮件
通过过** —— 因为默认配置是 fail-closed（总开关关 + 演练模式开 + 白名单空），
所以历史上跑出来的全是 `blocked_disabled` / `dry_run`。

于是"真发"这条路是**没被验证过**的代码。这种代码最危险：
平时看着一切正常，第一次真用的时候才暴露问题（而那时是对客户发的）。

本脚本做的是最小化的真实投递验证：

1. 走**完整链路** —— 工具网关（限流/熔断/权限/审计）→ 四层闸门 → SMTP 投递，
   不是直接调 SMTP 客户端。绕过网关测等于没测。
2. **临场覆盖配置**（不改进程外的 .env）：
   ``MAIL_ENABLED=true``、``MAIL_DRY_RUN=false``、白名单只放目标地址。
   这样"能不能真发"这件事不会因为跑过一次测试就永久留在 `.env` 里。
3. 发完**回查数据库**，确认这封信在 ``email_messages`` 里是
   ``gate_decision=allowed`` + ``status=sent``，而不是只看"没报错"。

## 用法

    # 先不加 --send 跑一次：只做检查，不真发
    uv run python scripts\\test_mail.py --to 你的邮箱@qq.com

    # 确认无误后真发
    uv run python scripts\\test_mail.py --to 你的邮箱@qq.com --send

⚠️ 这是唯一会真的往外发信的入口。默认不带 ``--send`` 就是干跑。
"""

from __future__ import annotations

import io
import os
import sys
from argparse import ArgumentParser


# 必须在 import Settings 之前把配置塞进环境 —— get_settings() 是 lru_cache 的单例，
# 一旦构造过就不会再读环境变量了。
def _parse_args() -> ArgumentParser:
    parser = ArgumentParser(description="真实 SMTP 投递验证")
    parser.add_argument("--to", required=True, help="收件人（只会发到这个地址）")
    parser.add_argument("--send", action="store_true", help="真的发出去；不加则只做检查")
    return parser


args = _parse_args().parse_args()

TO = args.to.strip()
IF_REALLY = args.send

os.environ["FASHIONOPS_MAIL_ENABLED"] = "true"
os.environ["FASHIONOPS_MAIL_DRY_RUN"] = "false" if IF_REALLY else "true"
os.environ["FASHIONOPS_MAIL_SEND_ALLOWLIST"] = TO
os.environ["FASHIONOPS_MAIL_SEND_DENYLIST"] = ""
os.environ["PYTHONIOENCODING"] = "utf-8"

# 整个进程的 stdout 都指向这个文件，所以不该关（用 with 反而会提前关掉）
_LOG = open("data/logs/test_mail.txt", "wb")  # noqa: SIM115
sys.stdout = io.TextIOWrapper(_LOG, encoding="utf-8", write_through=True)

from sqlalchemy import desc, select  # noqa: E402

from fashionops.config import get_settings  # noqa: E402
from fashionops.config_doctor import check_config  # noqa: E402
from fashionops.db import session_scope  # noqa: E402
from fashionops.db.models import EmailMessage  # noqa: E402
from fashionops.gateway import (  # noqa: E402
    CallContext,
    IdempotencyStore,
    MemoryCircuitBreaker,
    MemoryRateLimiter,
    ToolGateway,
    ToolRegistry,
)
from fashionops.tools import AGENT_SCOPES, register_all_tools  # noqa: E402

settings = get_settings()

print("=" * 72)
print("  真实 SMTP 投递验证")
print("=" * 72)
print(f"  收件人      : {TO}")
print(f"  模式        : {'真发（--send）' if IF_REALLY else '干跑（不加 --send 不会发出去）'}")
print(f"  SMTP        : {settings.smtp_host}:{settings.smtp_port} ssl={settings.smtp_use_ssl}")
print(f"  SMTP 用户   : {settings.smtp_user or '（未配）'}")
print(f"  授权码      : {'已配置' if settings.smtp_password else '（未配）'}")
print(f"  发件人      : {settings.mail_from}")
print(f"  mail_can_really_send: {settings.mail_can_really_send}")
print("-" * 72)

# ---- 第 0 步：配置体检，先把"变量名写错"这类问题挡掉 ----
report = check_config(settings)
if not report.ok:
    print("\n  ⚠ 配置体检发现问题：")
    for issue in report.issues:
        print(f"      [{issue.key}] {issue.message}  {issue.suggestion}")
    print("\n  先把上面改掉再跑。")
    raise SystemExit(1)
print("  ✓ 配置体检通过")

if not settings.smtp_host:
    print("\n  ✗ SMTP_HOST 是空的，投递会退化成 dry_run。先配 FASHIONOPS_SMTP_HOST")
    raise SystemExit(1)

# ---- 第 1 步：走完整网关链路发一封信 ----
registry = ToolRegistry()
register_all_tools(registry)
gateway = ToolGateway(
    registry,
    rate_limiter=MemoryRateLimiter(),
    breaker=MemoryCircuitBreaker(),
    idempotency=IdempotencyStore(use_redis=False),
)

subject = "【FashionOps】SMTP 真实投递验证"
body = (
    "这是一封由 FashionOps 平台发出的投递验证邮件。\n\n"
    "收到它就说明：\n"
    "  1. SMTP 认证成功（授权码有效）\n"
    "  2. 四层闸门的第一层（总开关）已放行\n"
    "  3. 白名单命中，没有被拦截\n"
    "  4. 限流与体积检查通过\n"
    "  5. 邮件已落库到 email_messages 表\n\n"
    "如果你不希望收到这类邮件，把 .env 里的 FASHIONOPS_MAIL_SEND_ALLOWLIST 清空即可 —— "
    "空白名单等于禁发。\n"
)

print("\n[1] 调用 mail.send 工具（走网关：权限 → 限流 → 熔断 → 闸门 → 投递）")
result = gateway.call(
    "mail.send",
    {
        "to_addrs": [TO],
        "subject": subject,
        "body_text": body,
        "purpose": "SMTP 连通性验证（scripts/test_mail.py）",
    },
    ctx=CallContext(
        actor="test_mail_script",
        scopes=AGENT_SCOPES["notification"],
        trace_id="test-mail-connectivity",
    ),
)

print(f"    工具返回 ok = {result.ok}")
if not result.ok:
    print(f"    错误: {result.error}")
    print("\n  ✗ 发送失败")
    raise SystemExit(1)

data = result.data or {}
print(f"    message_no    = {data.get('message_no')}")
# 注意键名：工具返回给调用方（模型）的字段叫 ``decision``，
# 而数据库列叫 ``gate_decision`` —— 两处名字不一样，别搞混
print(f"    decision      = {data.get('decision')}")
print(f"    reason        = {data.get('reason')}")
print(f"    status        = {data.get('status')}")

# ---- 第 2 步：回查数据库，确认落库状态 ----
print("\n[2] 回查 email_messages 表")
message_no = data.get("message_no")
with session_scope() as session:
    row = session.scalar(
        select(EmailMessage).where(EmailMessage.message_no == message_no)
        if message_no
        else select(EmailMessage).order_by(desc(EmailMessage.id)).limit(1)
    )
    if row is None:
        print("    ✗ 库里查不到这封信 —— 说明投递没落库")
        raise SystemExit(1)
    print(f"    邮件号     : {row.message_no}")
    print(f"    收件人     : {row.to_addrs}")
    print(f"    发件人     : {row.from_addr}")
    print(f"    主题       : {row.subject}")
    print(f"    状态       : {row.status}")
    print(f"    闸门决策   : {row.gate_decision}")
    print(f"    闸门原因   : {row.gate_reason}")
    print(f"    发送时间   : {row.sent_at}")
    print(f"    错误信息   : {row.error_message or '（无）'}")

    decision = row.gate_decision
    status = row.status

print("\n" + "=" * 72)
if decision == "allowed" and status == "sent":
    print("  结论: ✓ 真实投递成功，且已正确落库")
    print(f"       去 {TO} 的收件箱（含垃圾箱）确认一下真的到了")
elif decision == "dry_run":
    print("  结论: 走了干跑（没有真发）。加 --send 才会真的发出去")
else:
    print(f"  结论: ✗ 未通过 —— 决策 {decision!r}，状态 {status!r}")
    print("       如果是 blocked_*，看上面的 gate_reason 找原因")
    raise SystemExit(1)
print("=" * 72)
