"""task_todos + 审批表支持任务

把任务的计划从 ``tasks.dag``（DAG）换成 ``task_todos``（有序列表），
并让 ``chat_approvals`` 同时能承载任务的闸口。

Revision ID: b7c1e9a2f4d8
Revises: d5eb1be3d355
Create Date: 2026-09-16 14:40:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "b7c1e9a2f4d8"
down_revision: str | None = "d5eb1be3d355"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- 1. 任务待办 ----
    op.create_table(
        "task_todos",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, comment="任务内第几步，从 1 开始"),
        sa.Column("goal", sa.String(length=500), nullable=False),
        sa.Column(
            "kind",
            sa.String(length=32),
            nullable=False,
            server_default="generic",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("tool_names", sa.JSON(), nullable=True),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("artifact_nos", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("approval_no", sa.String(length=64), nullable=True),
        sa.Column("llm_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_cost", sa.Numeric(precision=12, scale=6), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "seq", name="uq_task_todo_seq"),
    )
    op.create_index("ix_task_todos_task_id", "task_todos", ["task_id"])
    op.create_index("ix_task_todos_status", "task_todos", ["status"])
    op.create_index("ix_task_todos_approval_no", "task_todos", ["approval_no"])
    op.create_index("ix_task_todos_created_at", "task_todos", ["created_at"])

    # ---- 2. 审批表支持任务侧 ----
    #
    # 只加列 + 放宽一列的可空性，**不动表名**。
    # 表名 ``chat_approvals`` 已经名不副实（它现在也管任务），但重命名的代价是：
    # `conversation_id` 上的外键占着同名索引，MySQL 直接 DROP 会报 errno 1553，
    # 得先造一个新索引顶上去；而 SQLite 的 batch 模式重建整表后索引名不受控，
    # 最后"迁移过的库"和"新建的库"索引名会分叉，`alembic check` 一直报。
    # 纯装饰性的改动不值得换来这个风险，原因记在 models_chat.py 里。
    with op.batch_alter_table("chat_approvals", schema=None) as batch_op:
        batch_op.add_column(sa.Column("task_no", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column("todo_seq", sa.Integer(), nullable=True, comment="任务侧：第几个 todo")
        )
        batch_op.create_index("ix_chat_approval_task", ["task_no", "status"], unique=False)
        batch_op.create_index("ix_chat_approvals_task_no", ["task_no"], unique=False)
        # 对话侧填 conversation_id，任务侧填 task_no —— 两边都不用的只能是脏数据。
        # 原来 NOT NULL，任务侧根本写不进来。
        batch_op.alter_column(
            "conversation_id", existing_type=sa.Integer(), nullable=True
        )


def downgrade() -> None:
    # 改回 NOT NULL 之前必须先处理掉任务侧的行，否则 MySQL 会写进一批 0：
    # ``ALTER ... NOT NULL`` 在非严格模式下把 NULL 悄悄变成默认值，
    # 于是任务的审批全挂到了 conversation_id=0 上 —— 看起来成功、数据全错。
    # 这里直接删掉任务侧的审批（它们是给任务用的，降级后没有消费者）。
    op.execute("DELETE FROM chat_approvals WHERE conversation_id IS NULL")

    with op.batch_alter_table("chat_approvals", schema=None) as batch_op:
        batch_op.alter_column(
            "conversation_id", existing_type=sa.Integer(), nullable=False
        )
        batch_op.drop_index("ix_chat_approvals_task_no")
        batch_op.drop_index("ix_chat_approval_task")
        batch_op.drop_column("todo_seq")
        batch_op.drop_column("task_no")

    # 不逐条 drop_index —— MySQL 的外键会复用同名索引，单独 DROP 会报 1553，
    # 而 DROP TABLE 本来就会带走索引和外键。同一个坑在 G3 里记过。
    op.drop_table("task_todos")
