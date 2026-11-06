"""tickets 加人工侧字段；新建 login_events 登录审计表

Revision ID: e5f6a7b8c9d0
Revises: d3e4f5a6b7c8
Create Date: 2026-10-27 10:00:00.000000

两件事放在同一个迁移里，是因为它们服务同一个界面需求：
客服工单页要显示"这条归谁、人做到哪一步了"（tickets 加列），
账号管理页要显示"这个账号有没有异常登录"（新建 login_events）。
分开写没有额外好处，只多了两次上线窗口。

## 为什么 login_events 要单独一张表

`users` 上已有 `last_login_at` / `last_login_ip`，但它们只能回答
"**最后一次**是什么时候、从哪个 IP"。异常登录判断需要的是**历史序列**：

* "这个账号一小时里从两个城市登录过" —— 只看最后一次永远看不出来
* "连续 5 次密码错误后突然成功了" —— 成功的那次会把失败记录覆盖掉

所以历史必须按事件存。审计日志 `audit_logs` 里其实也有登录记录，
但它是**通用**表（工具调用、接口操作混在一起），
按"登录"这个维度查要全表扫 `tool_name`，而且它没有 `success` / `reason`
这类登录专用的语义字段。登录是安全事件，值得有自己的表。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'e5f6a7b8c9d0'
down_revision: str | None = 'd3e4f5a6b7c8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- 1. tickets：人工侧字段（全部可空，不动已有数据）----
    with op.batch_alter_table('tickets', schema=None) as batch_op:
        batch_op.add_column(sa.Column('assignee', sa.String(length=64), nullable=True, comment='人工处理负责人（用户名）'))
        batch_op.add_column(sa.Column('assigned_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('human_status', sa.String(length=32), nullable=True, comment='人工处理阶段'))
        batch_op.add_column(sa.Column('human_note', sa.String(length=255), nullable=True, comment='最新一条人工备注'))
        batch_op.add_column(sa.Column('human_updated_at', sa.DateTime(), nullable=True))
        batch_op.create_index(batch_op.f('ix_tickets_assignee_created'), ['assignee', 'created_at'], unique=False)

    # ---- 2. 登录事件表 ----
    op.create_table(
        'login_events',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('username', sa.String(length=64), nullable=False, comment='登录时填的用户名。用户不存在时也要记 —— 探测账号名的行为本身就是信号'),
        sa.Column('user_id', sa.Integer(), nullable=True, comment='登录成功才有。用 SET NULL，用户被删后事件仍然保留'),
        sa.Column('success', sa.Boolean(), nullable=False, comment='是否登录成功'),
        sa.Column('reason', sa.String(length=64), nullable=True, comment='失败原因：bad_password/disabled/locked/unknown_user'),
        sa.Column('auth_method', sa.String(length=32), nullable=False, comment='password/api_key/oauth:dingtalk'),
        sa.Column('ip', sa.String(length=64), nullable=True),
        # UA 只截前 255：它是客户端完全可控的字段，存全文等于给别人一个
        # 往库里塞超长字符串的地方（MySQL 的 TEXT 也不便宜）
        sa.Column('user_agent', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    # 查询模式固定是"按用户看最近 N 次"和"按时间倒序看全部" —— 两个索引各管一个
    op.create_index(op.f('ix_login_events_username_created'), 'login_events', ['username', 'created_at'], unique=False)
    op.create_index(op.f('ix_login_events_created'), 'login_events', ['created_at'], unique=False)
    op.create_index(op.f('ix_login_events_user'), 'login_events', ['user_id'], unique=False)
    # "扫失败登录"是安全排查里最常做的动作，单独一个索引避免全表扫
    op.create_index(op.f('ix_login_events_success_created'), 'login_events', ['success', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_login_events_success_created'), table_name='login_events')
    op.drop_index(op.f('ix_login_events_user'), table_name='login_events')
    op.drop_index(op.f('ix_login_events_created'), table_name='login_events')
    op.drop_index(op.f('ix_login_events_username_created'), table_name='login_events')
    op.drop_table('login_events')

    with op.batch_alter_table('tickets', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tickets_assignee_created'))
        batch_op.drop_column('human_updated_at')
        batch_op.drop_column('human_note')
        batch_op.drop_column('human_status')
        batch_op.drop_column('assigned_at')
        batch_op.drop_column('assignee')
