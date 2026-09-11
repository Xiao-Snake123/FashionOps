"""数据库命令行工具。

用法::

    uv run fashionops-db info      # 看当前连的是哪个库、有哪些表、多少行
    uv run fashionops-db init      # 建表（幂等，已存在的不动）
    uv run fashionops-db drop      # 删表（危险，需要 --yes）
    uv run fashionops-db reset     # 删表 + 重建（危险，需要 --yes）

为什么不用 Alembic：阶段 0 表结构还在剧烈变动，每次都写 migration 是纯浪费。
等表稳定下来（阶段 2 之后）再引入 Alembic 做版本管理。
"""

from __future__ import annotations

import sys
from argparse import ArgumentParser

from sqlalchemy import inspect, text

from fashionops.config import get_settings
from fashionops.db.base import Base, get_engine
from fashionops.db.models import ALL_MODELS

# Windows 控制台默认 GBK，打印中文和 emoji 会直接抛 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _mask_url(url: str) -> str:
    """打日志时把密码抹掉。"""
    if "@" not in url:
        return url
    head, tail = url.split("://", 1)
    return f"{head}://***@{tail.rsplit('@', 1)[-1]}"


def cmd_info(_args) -> int:
    settings = get_settings()
    engine = get_engine(settings)

    print("=" * 68)
    print("  服装电商多 Agent 运营中台 —— 数据库状态")
    print("=" * 68)
    print(f"  连接串    : {_mask_url(settings.sqlalchemy_url)}")
    print(f"  后端      : {'SQLite（降级模式）' if settings.is_sqlite else 'MySQL'}")
    print(f"  模型表数  : {len(ALL_MODELS)}")
    print("-" * 68)

    existing = set(inspect(engine).get_table_names())
    total_rows = 0
    for model in ALL_MODELS:
        name = model.__tablename__
        if name not in existing:
            print(f"  {'✗':<3} {name:<18} 未建表")
            continue
        with engine.connect() as conn:
            count = conn.execute(text(f"SELECT COUNT(*) FROM `{name}`")).scalar_one()
        total_rows += count
        print(f"  {'✓':<3} {name:<18} {count:>8} 行")
    print("-" * 68)
    print(f"  合计 {total_rows} 行")
    missing = len(ALL_MODELS) - len([m for m in ALL_MODELS if m.__tablename__ in existing])
    if missing:
        print(f"\n  还有 {missing} 张表没建，跑：uv run fashionops-db init")
    print()
    return 0


def cmd_init(_args) -> int:
    settings = get_settings()
    engine = get_engine(settings)

    print(f"目标库: {_mask_url(settings.sqlalchemy_url)}")
    if settings.is_sqlite:
        print(
            "提示: 当前落到 SQLite —— 说明 .env 里 FASHIONOPS_DB_HOST 还是空的。\n"
            "      想要 MySQL 请先跑 deploy\\scripts\\init-mysql.ps1"
        )

    Base.metadata.create_all(engine)

    existing = set(inspect(engine).get_table_names())
    created = [m.__tablename__ for m in ALL_MODELS if m.__tablename__ in existing]
    print(f"完成：{len(created)} 张表就绪")
    for i, name in enumerate(created, 1):
        print(f"  {i:>2}. {name}")
    return 0


def _drop_all(args) -> int:
    if not args.yes:
        print("这是破坏性操作，会删掉所有业务数据。确认请加 --yes")
        return 2
    engine = get_engine(get_settings())
    # 按依赖倒序删，避免外键挡住
    Base.metadata.drop_all(engine)
    print(f"已删除 {len(ALL_MODELS)} 张表")
    return 0


def cmd_drop(args) -> int:
    return _drop_all(args)


def cmd_reset(args) -> int:
    if _drop_all(args) != 0:
        return 2
    print()
    return cmd_init(args)


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="fashionops-db", description="数据库管理工具")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("info", help="查看连接信息与各表行数")
    sub.add_parser("init", help="建表（幂等）")

    for name, help_text in (("drop", "删除所有表"), ("reset", "删表后重建")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--yes", action="store_true", help="确认执行破坏性操作")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    handlers = {
        "info": cmd_info,
        "init": cmd_init,
        "drop": cmd_drop,
        "reset": cmd_reset,
    }
    if args.command is None:
        parser.print_help()
        return 0
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
