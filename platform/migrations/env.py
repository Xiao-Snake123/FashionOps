"""Alembic 迁移环境。

三件事和默认模板不一样，都是踩过才知道的：

1. **连接串从项目的 ``Settings`` 取**，不从 ``alembic.ini`` 读。
   否则会出现"迁移连的是 A 库、应用连的是 B 库"，而两边都"成功"了。

2. **显式 import ``fashionops.db.models``**。
   ``--autogenerate`` 靠 ``Base.metadata`` 里有哪些表来决定生成什么，
   而 metadata 是**导入时**才填充的。不导入模型模块，autogenerate 会认为
   "库里有一堆表、metadata 里一个都没有"，然后生成一堆删表语句。

3. **``render_as_batch=True``**。
   SQLite 不支持大部分 ``ALTER TABLE``，batch 模式会自动退化成
   "建新表 → 拷数据 → 换名"，让同一份迁移在 SQLite 和 MySQL 上都能跑
   （单测跑 SQLite，这条必需）。

另外用 ``compare_type=True`` / ``compare_server_default=True``：
默认配置**不检测列类型和默认值的变化**，改了 ``String(32)`` → ``String(64)``
不会生成任何迁移，静默漏掉 —— 这是 autogenerate 最容易骗人的地方。
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

# 让 alembic 直接 import src/ 下的包，不依赖 "pip install -e" 或 PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fashionops.config import get_settings  # noqa: E402
from fashionops.db import models  # noqa: E402,F401  —— 见模块 docstring 第 2 条
from fashionops.db.base import Base, build_engine  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _include_object(obj, name, type_, reflected, compare_to):  # noqa: ANN001, ANN201
    """把非本项目管的表排除在 autogenerate 之外。

    目前没有这类表，但这个钩子要留着：以后接进去任何外部表
    （或者 alembic 自己的 ``alembic_version`` 之外的工具表），
    autogenerate 会尝试把它们删掉。
    """
    del obj, name, type_, reflected, compare_to
    return True


def run_migrations_offline() -> None:
    """``--sql`` 模式：只生成 SQL 文件，不连库。

    用的时候注意：batch 模式在 offline 下需要显式指定 ``dialect``，
    这里是按当前配置推断的，所以离线生成 SQL 前要保证 .env 指向目标方言。
    """
    context.configure(
        url=get_settings().sqlalchemy_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """正常模式：连库执行。用项目自己的 ``build_engine``，池参数与运行时一致。"""
    connectable = build_engine(get_settings())

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
            compare_server_default=True,
            include_object=_include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
