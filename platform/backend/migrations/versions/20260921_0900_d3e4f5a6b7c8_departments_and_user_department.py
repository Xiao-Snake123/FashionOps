"""新增部门表，并把用户挂到部门上

分级的账号管理需要"部门"这个维度：部门主管只能管本部门的账号。
没有这一列，"本部门"就无从判定 —— 而靠用户名前缀之类的外部约定去猜，
一旦有人改名就悄悄失效，且失效时不会有任何报错。

## 两个刻意的选择

1. **``users.department_id`` 可空**。系统初始管理员、对接用的服务账号
   本来就不属于任何部门。不许为空会逼出一个叫"其它"的假部门，
   然后所有人都被塞进去 —— 那个字段从此不再表达任何信息。

2. **``ON DELETE SET NULL``**，不是 CASCADE。删部门时不能连带删人：
   人的任务、审批、审计、发件记录全挂在 user_id 上，
   连带删会把这些历史一起毁掉，而审计是不可变记录。
   删部门的结果应该是"这些人暂时没有部门"，然后由管理员重新分配。

种子部门是给界面下拉框用的初始值，可以改名和停用，不是硬编码约束。

Revision ID: d3e4f5a6b7c8
Revises: c1d2e3f4a5b6
Create Date: 2026-09-21 09:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op


def _now() -> datetime:
    """naive UTC —— 和 `base.utcnow()` 保持同样的约定（库里存不带时区的时间）。

    刻意**不 import 应用代码**：迁移脚本要在任何版本的应用代码上都跑得起来，
    引用 `fashionops.db.base.utcnow` 会让"以后这个函数改了签名"变成
    "历史迁移跑不动了"，而历史迁移是不能改的。
    """
    return datetime.now(UTC).replace(tzinfo=None)


revision: str = "d3e4f5a6b7c8"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: 初始部门。只在新库初始化/首次升级时插入一次，之后以数据库为准 ——
#: 迁移里**不做 upsert**：那样每次升级都会把运维改过的部门名覆盖回去。
_SEED_DEPARTMENTS = (
    ("ops", "运营部", "商品运营、活动与日常经营分析"),
    ("service", "客服部", "售前售后工单与知识库维护"),
    ("marketing", "市场部", "广告投放与增长"),
    ("product", "商品部", "选品、供应链与库存"),
    ("tech", "技术部", "系统维护与数据对接"),
)


def upgrade() -> None:
    op.create_table(
        "departments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        # description **不能带 server_default**：MySQL 拒绝给 TEXT 列设默认值
        # （错误 1101）。默认值由 ORM 侧给（`default=""`），
        # 建表 DDL 不需要它 —— 迁移脚本自己插入种子行时也会显式传值。
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_departments_code", "departments", ["code"], unique=True)

    # 用 batch 模式：SQLite 不支持 ALTER TABLE ADD CONSTRAINT，
    # 批量模式会重建表来达到同样效果，MySQL 上则退化成普通 ALTER。
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("department_id", sa.Integer(), nullable=True))
        batch.create_index("ix_users_department_id", ["department_id"])
        batch.create_foreign_key(
            "fk_users_department_id",
            "departments",
            ["department_id"],
            ["id"],
            ondelete="SET NULL",
        )

    now = _now()
    op.bulk_insert(
        sa.table(
            "departments",
            sa.column("code", sa.String),
            sa.column("name", sa.String),
            sa.column("description", sa.Text),
            sa.column("is_active", sa.Boolean),
            sa.column("created_at", sa.DateTime),
            sa.column("updated_at", sa.DateTime),
        ),
        [
            {
                "code": code,
                "name": name,
                "description": desc,
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            }
            for code, name, desc in _SEED_DEPARTMENTS
        ],
    )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("fk_users_department_id", type_="foreignkey")
        batch.drop_index("ix_users_department_id")
        batch.drop_column("department_id")

    op.drop_index("ix_departments_code", table_name="departments")
    op.drop_table("departments")
