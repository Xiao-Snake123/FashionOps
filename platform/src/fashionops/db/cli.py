"""数据库命令行工具（基于 Alembic 迁移）。

用法::

    uv run fashionops-db info        # 看连的是哪个库、迁移版本、各表行数
    uv run fashionops-db init        # 升到最新版本（**幂等**，可反复跑）
    uv run fashionops-db upgrade     # 同 init
    uv run fashionops-db downgrade -1  # 回退一步（危险）
    uv run fashionops-db current     # 当前版本
    uv run fashionops-db history     # 迁移历史
    uv run fashionops-db revision -m "加 xx 表"   # 生成新迁移（自动对比模型）
    uv run fashionops-db drop --yes  # 删库（危险）
    uv run fashionops-db reset --yes # 删库 + 升到最新（危险）

## 为什么改成了 Alembic

阶段 0 的表结构还在剧烈变动，那时用 ``Base.metadata.create_all`` 建表最省事，
所以 CLI 里有一句"等表稳定下来再引入 Alembic"。**现在是那个时候了** ——
上线之后不可能每次改一列就 drop 重建，必须能对已有数据做增量变更。

## 关键设计：能"接管"已存在的库

真正的项目里，库往往**先于**迁移体系存在（我们自己的开发库就是这样：
15 张表是 ``create_all`` 建的，没有 ``alembic_version`` 表）。

这种情况下直接跑 ``upgrade head`` 会因为"表已存在"而失败。正确做法是
**先 stamp（只写版本号，不执行 DDL）再 upgrade**。``init`` 自动判断并走这条路，
所以无论对空库还是对已存在的库，``init`` 都是安全且幂等的。

判断依据是"库里有没有业务表"而不是"有没有 alembic_version"：
后者只说明"迁移体系没跑过"，前者才说明"schema 已经有了"。
"""

from __future__ import annotations

import sys
from argparse import ArgumentParser, Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from fashionops.config import get_settings
from fashionops.db.base import Base, get_engine
from fashionops.db.models import ALL_MODELS

# Windows 控制台默认 GBK，打印中文和 emoji 会直接抛 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

#: 迁移脚本目录。用包路径反推项目根，不依赖当前工作目录 ——
#: 从任意目录执行 `fashionops-db` 都能找到迁移。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"

ALEMBIC_VERSION_TABLE = "alembic_version"


def _mask_url(url: str) -> str:
    """打日志时把密码抹掉。

    这个函数存在的意义：CLI 输出经常被贴到 issue / 聊天记录里。
    """
    if "@" not in url:
        return url
    head, tail = url.split("://", 1)
    return f"{head}://***@{tail.rsplit('@', 1)[-1]}"


def _alembic_config() -> Config:
    """程序化构造 Alembic 配置。

    **不读 ``alembic.ini``**。两个原因：

    1. Alembic 用 ``encoding="locale"`` 读 ini，Windows 中文环境是 GBK，
       ini 里只要有非 ASCII 字符就会 ``UnicodeDecodeError``。程序化构造完全绕开。
    2. 连接串统一由 ``env.py`` 从 ``Settings`` 取，这里不需要重复配。

    副作用是**命令行敲 ``alembic`` 也照常能用**（走 ini），两条路都能走。
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


def _table_names() -> list[str]:
    return sorted(inspect(get_engine(get_settings())).get_table_names())


def _app_tables() -> list[str]:
    """业务表名（排除 alembic 自己的版本表）。"""
    return [t for t in _table_names() if t != ALEMBIC_VERSION_TABLE]


def _migration_state() -> tuple[bool, str]:
    """返回 (是否已有 schema, 当前迁移版本描述)。"""
    has_schema = bool(_app_tables())

    engine = get_engine(get_settings())
    try:
        with engine.connect() as conn:
            row = conn.execute(text(f"SELECT version_num FROM `{ALEMBIC_VERSION_TABLE}`")).first()
        version = row[0] if row else "（未标记）"
    except Exception:  # noqa: BLE001 —— 表不存在就是没跑过迁移
        version = "（未标记）"
    return has_schema, version


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------
def cmd_info(_args: Namespace) -> int:
    settings = get_settings()
    engine = get_engine(settings)

    print("=" * 68)
    print("  服装电商多 Agent 运营中台 —— 数据库状态")
    print("=" * 68)
    print(f"  连接串    : {_mask_url(settings.sqlalchemy_url)}")
    print(f"  后端      : {'SQLite' if settings.is_sqlite else 'MySQL'}")
    print(f"  模型表数  : {len(ALL_MODELS)}")

    has_schema, version = _migration_state()
    print(f"  迁移版本  : {version}")
    print("-" * 68)

    existing = set(_table_names())
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

    if not has_schema:
        print("\n  库是空的，跑：uv run fashionops-db init")
    elif version == "（未标记）":
        print(
            "\n  库里已有表，但没有迁移版本号 —— 说明这些表是早期 create_all 建的。\n"
            "  跑 uv run fashionops-db init 会自动接管（先 stamp 再 upgrade），不会丢数据。"
        )
    print()
    return 0


def cmd_init(_args: Namespace) -> int:
    settings = get_settings()
    print(f"目标库: {_mask_url(settings.sqlalchemy_url)}")
    if settings.is_sqlite:
        print(
            "提示: 当前落到 SQLite —— 说明 .env 里 FASHIONOPS_DB_HOST 还是空的。\n"
            "      想要 MySQL 请先跑 deploy\\scripts\\init-mysql.ps1"
        )

    has_schema, version = _migration_state()

    if has_schema and version == "（未标记）":
        # 接管已存在的库：只写版本号，不跑 DDL。
        # 绝不能在这里 upgrade —— 那些表已经在了，CREATE TABLE 会直接报错。
        print(
            f"\n检测到库里已有 {len(_app_tables())} 张表（早期 create_all 建的），"
            "先把迁移版本标到当前 head。"
        )
        command.stamp(_alembic_config(), "head")
        print("已标记。后续变更走迁移，不再动这些表。")
        return cmd_info(_args)

    command.upgrade(_alembic_config(), "head")

    existing = set(_table_names())
    ready = [m.__tablename__ for m in ALL_MODELS if m.__tablename__ in existing]
    print(f"\n完成：{len(ready)} 张表就绪")
    for i, name in enumerate(sorted(ready), 1):
        print(f"  {i:>2}. {name}")
    return 0


def cmd_upgrade(args: Namespace) -> int:
    command.upgrade(_alembic_config(), args.revision)
    return 0


def cmd_downgrade(args: Namespace) -> int:
    if not args.yes:
        print("回退会丢数据。确认请加 --yes")
        return 2
    command.downgrade(_alembic_config(), args.revision)
    return 0


def cmd_current(_args: Namespace) -> int:
    command.current(_alembic_config(), verbose=True)
    return 0


def cmd_history(_args: Namespace) -> int:
    command.history(_alembic_config(), verbose=True)
    return 0


def cmd_revision(args: Namespace) -> int:
    command.revision(_alembic_config(), message=args.message, autogenerate=not args.no_autogenerate)
    return 0


def _drop_all(yes: bool) -> int:
    if not yes:
        print("这是破坏性操作，会删掉所有业务数据。确认请加 --yes")
        return 2
    engine = get_engine(get_settings())
    # 按依赖倒序删，避免外键挡住
    Base.metadata.drop_all(engine)

    # alembic_version 不在 metadata 里，得单独删 ——
    # 留着它会让下次 init 以为"迁移已经跑过了"，然后跳过建表，得到一堆"未建表"。
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS `{ALEMBIC_VERSION_TABLE}`"))

    print(f"已删除 {len(ALL_MODELS)} 张表 + 迁移版本表")
    return 0


def cmd_drop(args: Namespace) -> int:
    return _drop_all(args.yes)


def cmd_reset(args: Namespace) -> int:
    if _drop_all(args.yes) != 0:
        return 2
    print()
    return cmd_init(args)


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="fashionops-db", description="数据库管理与迁移工具")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("info", help="查看连接信息、迁移版本与各表行数")
    sub.add_parser("init", help="升到最新迁移版本（幂等；会自动接管已存在的库）")
    sub.add_parser("current", help="显示当前迁移版本")
    sub.add_parser("history", help="显示迁移历史")

    p_up = sub.add_parser("upgrade", help="升到指定版本（默认 head）")
    p_up.add_argument("revision", nargs="?", default="head")

    p_down = sub.add_parser("downgrade", help="回退到指定版本，如 -1 / base")
    p_down.add_argument("revision")
    p_down.add_argument("--yes", action="store_true")

    p_rev = sub.add_parser("revision", help="生成新迁移")
    p_rev.add_argument("-m", "--message", required=True)
    p_rev.add_argument(
        "--no-autogenerate", action="store_true", help="生成空模板，手写升级逻辑"
    )

    for name, help_text in (("drop", "删除所有表"), ("reset", "删表后按迁移重建")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--yes", action="store_true", help="确认执行破坏性操作")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    handlers = {
        "info": cmd_info,
        "init": cmd_init,
        "upgrade": cmd_upgrade,
        "downgrade": cmd_downgrade,
        "current": cmd_current,
        "history": cmd_history,
        "revision": cmd_revision,
        "drop": cmd_drop,
        "reset": cmd_reset,
    }
    if args.command is None:
        parser.print_help()
        return 0
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
