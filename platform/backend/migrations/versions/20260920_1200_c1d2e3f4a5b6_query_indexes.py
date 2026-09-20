"""补齐两个查询索引

两个索引都是"接口实际怎么查"和"库里有什么索引"对不上导致的：

1. ``tickets(status, created_at)``
   工单列表是 ``WHERE status = ? ORDER BY created_at DESC``。
   已有的 ``ix_tickets_status_priority`` 第二列是 ``priority``，
   用不上这个排序，于是每次都要 filesort。

2. ``email_messages(gate_decision)``
   ``gate_decision`` 既被邮件列表用来精确过滤（含 ``LIKE 'blocked%'``），
   又被 ``email_gate_stats`` 拿去 ``GROUP BY``，两种用法都没有索引可走。

索引是纯性能改动，**不影响任何数据语义**：
downgrade 直接 drop 掉即可，不需要回填或转换。

Revision ID: c1d2e3f4a5b6
Revises: a9b8c7d6e5f4
Create Date: 2026-09-20 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c1d2e3f4a5b6'
down_revision: str | None = 'a9b8c7d6e5f4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        'ix_tickets_status_created',
        'tickets',
        ['status', 'created_at'],
        unique=False,
    )
    op.create_index(
        'ix_email_gate_decision',
        'email_messages',
        ['gate_decision'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_email_gate_decision', table_name='email_messages')
    op.drop_index('ix_tickets_status_created', table_name='tickets')
