"""配置体检命令行。

用法::

    uv run fashionops-config check      # 检查 .env 里的问题（拼错的键、可疑的值）
    uv run fashionops-config show       # 打印生效中的配置（敏感值脱敏）

``check`` 有错时返回非 0，可以放进 CI / 部署前的健康检查：

    ./fashionops-config check || exit 1
"""

from __future__ import annotations

import sys
from argparse import ArgumentParser

from fashionops.config import get_settings
from fashionops.config_doctor import check_config

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

#: 这些字段的值不能直接打印，只显示长度或掩码
_SECRET_HINTS = ("password", "key", "secret", "token")


def _mask(name: str, value: object) -> str:
    """脱敏。宁可显示得少一点，也不要把密钥打进日志或截图里。"""
    text = str(value)
    if not any(h in name.lower() for h in _SECRET_HINTS):
        return text
    if not text:
        return "（未设置）"
    # 保留前 2 位 + 长度，足够判断"是不是配了、配的是不是同一个"
    return f"{text[:2]}***（长度 {len(text)}）"


def cmd_check(_args) -> int:
    settings = get_settings()
    report = check_config(settings)

    print("=" * 72)
    print("  配置体检")
    print("=" * 72)
    if report.env_files:
        for path in report.env_files:
            print(f"  配置文件 : {path}")
    else:
        print("  配置文件 : （一个都没找到）")
    print(f"  已知配置项: {report.known_count} 个")
    print("-" * 72)

    if report.ok:
        print("\n  ✓ 没发现问题\n")
        return 0

    for issue in report.issues:
        mark = "✗" if issue.level == "error" else "!"
        color = "" if issue.level == "error" else ""
        print(f"\n  {color}{mark} [{issue.key}]{color}")
        print(f"      {issue.message}")
        if issue.suggestion:
            print(f"      → {issue.suggestion}")

    print()
    print("-" * 72)
    print(f"  错误 {len(report.errors)} 条，警告 {len(report.warnings)} 条")
    if report.errors:
        print("\n  ⚠ 上面标 ✗ 的配置项**完全没生效**，应用正在用默认值运行。")
    print()
    return 1 if report.errors else 0


def cmd_show(_args) -> int:
    settings = get_settings()

    print("=" * 72)
    print("  生效中的配置")
    print("=" * 72)
    for name in sorted(type(settings).model_fields):
        value = getattr(settings, name)
        print(f"  {name:<32} {_mask(name, value)}")

    print("-" * 72)
    print("  派生值（不是配置项，是算出来的）")
    print("=" * 72)
    print(f"  {'sqlalchemy_url':<32} {settings.sqlalchemy_url.split('@')[-1]}")
    print(f"  {'is_sqlite':<32} {settings.is_sqlite}")
    print(f"  {'model_configured':<32} {settings.model_configured}")
    print(f"  {'mail_can_really_send':<32} {settings.mail_can_really_send}")
    print(f"  {'storage_backend':<32} {settings.storage_backend}")
    print()

    mail = settings.mail_status()
    print("  邮件闸门状态（决定邮件能不能真发出去）")
    print("-" * 72)
    for key, value in mail.items():
        print(f"  {key:<20} {value}")
    print()
    if not settings.mail_can_really_send:
        print("  → 当前**发不出去**，所有邮件会走 dry_run 或被闸门拦下。")
        print("    要真发必须同时满足：MAIL_ENABLED=true、MAIL_DRY_RUN=false、")
        print("    SMTP_HOST 非空、MAIL_SEND_ALLOWLIST 非空、SMTP_USER/PASSWORD 正确。")
        print()
    return 0


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="fashionops-config", description="配置体检与查看")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("check", help="检查 .env 里拼错的键与可疑的值（有错返回非 0）")
    sub.add_parser("show", help="打印生效中的配置（敏感值脱敏）")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "show":
        return cmd_show(args)
    if args.command == "check":
        return cmd_check(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
