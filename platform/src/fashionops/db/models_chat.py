"""对话域模型 —— 会话 / 消息 / 对话内审批（3 张表）。

**为什么又单独一个模块**：和 `models_identity.py` 同样的理由 ——
`models.py` 是"业务域"（商品/订单/工单/任务），这里是"对话域"。
对话的增长曲线和业务表完全不同：一次聊天能产生几十条消息，
而商品表一个月也未必变一次。混在一起会让 `models.py` 的读者
在"看订单表"的时候被一堆消息字段打断。

`models.py` 会把本模块的类一并导入并挂进 `ALL_MODELS`，
所以建表、迁移、健康检查还是走那一份清单。

## 设计上的几个决定

### 1. 对话**不替代**任务，而是任务的入口

用户对着对话框说需求，Agent 判断这次是"直接回答"还是"编排成一个任务"。
所以 `chat_messages.task_no` 是可空的 —— 有空值说明这条消息只是一句闲聊，
有值说明它触发了一个真实的 DAG 任务。

**为什么不把对话做成唯一的执行模型**：任务有 DAG、有断点续跑、
有产物、有审批留痕，这些是对话消息表达不了的。硬塞进去会得到
"看起来统一、实际两边都表达不清"的数据结构。

### 2. 审批单独一张表，不塞进消息

审批是**待处理事项**，必须能一条 SQL 查出"现在有几个人工闸口卡着"：

    SELECT * FROM approvals WHERE status = 'pending'

塞进 `chat_messages` 就只能全表扫描 + 按 payload 猜，而且没法加索引。
这条查询是运维看板要用，也是"这个系统有没有人在盯着"的证据。

### 3. 工具调用的**入参和结果都要存**

只存"调用了 erp.query_inventory"没有意义 —— 事后要能回答
"当时它是拿什么参数查的、下游返回了什么、它据此得出了什么结论"。
这三点缺一个，复盘就只能靠猜。

而且结果存下来之后，前端可以把"工具执行过程"折叠展示给用户看，
这也是对话式 Agent 和普通聊天机器人在体验上最大的区别。

### 4. 用户名冗余快照

和 `tasks.created_by` 同样的理由：用户被删/改名后，
历史对话上会显示成别人的名字。**"这场对话当时是谁的"是个事实。**
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fashionops.db.base import Base, utcnow

# ---------------------------------------------------------------------------
# 取值常量。和 models_identity 一样用朴素字符串：
# 加一个状态值不需要 ALTER TABLE，合法性由应用层校验。
# ---------------------------------------------------------------------------
CONVERSATION_STATUS_ACTIVE = "active"
CONVERSATION_STATUS_ARCHIVED = "archived"

#: 消息角色。前三个和 LLM 的 role 一一对应，
#: `tool` 是"工具执行结果回喂给模型"的那条 —— 它对用户不可见，
#: 但必须落库，否则刷新页面后 Agent 就"忘了自己刚才查过什么"。
MSG_ROLE_USER = "user"
MSG_ROLE_ASSISTANT = "assistant"
MSG_ROLE_TOOL = "tool"
MSG_ROLE_SYSTEM = "system"

APPROVAL_STATUS_PENDING = "pending"
APPROVAL_STATUS_APPROVED = "approved"
APPROVAL_STATUS_REJECTED = "rejected"
#: 用户等太久没点 / 会话被归档时批量作废。留着 pending 会让看板永远有鬼影。
APPROVAL_STATUS_EXPIRED = "expired"

COST = Numeric(12, 6)


# ===========================================================================
# 1. 会话
# ===========================================================================
class Conversation(Base):
    """一场对话。"""

    __tablename__ = "chat_conversations"
    __table_args__ = (
        # 首页"我的会话列表"就靠这个索引：按用户取、按更新时间倒序
        Index("ix_chat_conv_user_updated", "user_id", "updated_at"),
        Index("ix_chat_conv_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 对外编号，形如 ``CH2609160003``。**不用自增 id 对外**：
    #: 自增 id 会泄漏"系统里总共有多少会话"，而且换库时没法保持稳定。
    conversation_no: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    #: 标题。首条用户消息自动截取，用户可以改。
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    status: Mapped[str] = mapped_column(String(32), default=CONVERSATION_STATUS_ACTIVE)

    # ---- 归属 ----
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    #: 用户名快照，理由见模块顶部的第 4 条
    username: Mapped[str] = mapped_column(String(64), default="user")

    # ---- 计量 ----
    #: 这些字段是**汇总值**，每条消息上还有各自的明细。
    #: 冗余一份是为了会话列表页不用去 JOIN 聚合几十万行消息。
    model: Mapped[str] = mapped_column(String(64), default="")
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_cost: Mapped[Decimal] = mapped_column(COST, default=Decimal("0"))

    last_message_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    messages: Mapped[list[ChatMessage]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ChatMessage.seq",
    )


# ===========================================================================
# 2. 消息
# ===========================================================================
class ChatMessage(Base):
    """一条对话消息。

    这张表同时承担**两个角色**，这是刻意的：

    1. **给用户看的聊天记录** —— 只展示 ``role`` 为 user/assistant 的
    2. **Agent 循环的状态载体** —— 重放成 LLM 的 messages 数组继续跑

    为什么不分成两张表：它们的内容完全一致，分开就要保证两边同步，
    而"同步"是 bug 的温床。第 2 个用途多出来的字段（tool_call_id 等）
    在展示时忽略即可。
    """

    __tablename__ = "chat_messages"
    __table_args__ = (
        # 会话内 seq 单调递增。断线重连时前端带 `since_seq` 补数据，
        # 和 task_events 是同一套做法。
        UniqueConstraint("conversation_id", "seq", name="uq_chat_msg_seq"),
        Index("ix_chat_msg_conv_seq", "conversation_id", "seq"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer, comment="会话内单调递增，SSE 靠它断点续传")

    role: Mapped[str] = mapped_column(String(16), comment="user/assistant/tool/system")
    content: Mapped[str] = mapped_column(Text, default="")

    # ---- 工具调用（role=assistant 发出，role=tool 应答）----
    #: 模型给这次调用分配的 id。把 assistant 的请求和 tool 的结果**对上**
    #: 全靠它 —— 缺了它，一轮里调两个工具时结果会串。
    tool_call_id: Mapped[str | None] = mapped_column(String(64), index=True)
    tool_name: Mapped[str | None] = mapped_column(String(128))
    tool_arguments: Mapped[dict | None] = mapped_column(JSON)
    tool_result: Mapped[dict | None] = mapped_column(JSON)
    tool_ok: Mapped[bool | None] = mapped_column(Boolean)
    tool_duration_ms: Mapped[int | None] = mapped_column(Integer)

    # ---- 关联到别的实体 ----
    #: 这条消息（或它所在这一轮）触发的任务。见模块顶部第 1 条。
    task_no: Mapped[str | None] = mapped_column(String(64), index=True)
    #
    # ⚠ **这里刻意没有 approval_id**：
    # `approvals.message_id` 已经指向消息了，再加一个反指的外键
    # 会让两张表**互相引用** —— `create_all` 做拓扑排序时会直接报
    # CircularDependencyError。而且同一条信息存两处就要保证两处一致，
    # 那正是我们想避免的。要拿某条消息的审批，按 message_id 反查即可。

    # ---- 计量（每条消息的明细，会话上的汇总由这些累加而来）----
    model: Mapped[str | None] = mapped_column(String(64))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[Decimal] = mapped_column(COST, default=Decimal("0"))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    finish_reason: Mapped[str | None] = mapped_column(String(32))

    #: 流式生成到一半就断了（用户关页面 / 模型超时）时置 true。
    #: 下次进来能看出"这条回答是不完整的"，而不是把它当成完整回答继续接话。
    interrupted: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


# ===========================================================================
# 3. 人工审批（对话和任务共用）
# ===========================================================================
class Approval(Base):
    """一个人工闸口。对话和任务共用这一张表。

    ## 为什么要通用而不是各存一份

    最早它叫 ``chat_approvals``，只服务对话；任务那边的闸口是用
    ``tasks.approval_payload`` 这个 JSON 字段顶着的。两套的后果是：

    * "现在有几个闸口卡着人" 这一条 SQL 要写两遍（一张表 + 一个 JSON 字段），
      而 JSON 字段没法加索引，只能全表扫
    * 判断"这个工具要不要审批"的逻辑会分成两份，改一处必漏另一处
    * 任务的审批**没有独立行**，于是"谁批的、什么时候批的、批准后执行成什么了"
      三个问题在任务侧根本答不了

    所以合并成一张：``conversation_id`` 和 ``task_no`` 二选一，
    由应用层保证至少有一个（数据库层做二选一的约束很别扭，
    而且 MySQL 的 CHECK 支持得晚）。

    ## 表名为什么还叫 ``chat_approvals``

    类名已经改成 ``Approval``（它不再只服务对话），但**物理表名没改**，
    因为重命名买不到对应的价值：

    * ``conversation_id`` 上有外键。MySQL 里这个索引被外键占着，
      直接 DROP 会报 errno 1553（"Cannot drop index ... needed in a
      foreign key constraint"）—— 得先造一个新索引顶上去才能删
    * SQLite 的 ``batch_alter_table`` 是**重建整表**，重建后索引名怎么落
      取决于它自己，最后"迁移过的库"和"新建的库"索引名会分叉，
      ``alembic check`` 会一直报

    为一个纯装饰性的重命名搞出一个高风险迁移不划算。真要改，
    等下次有别的理由动这张表时一起做。

    ## 为什么对话式 Agent 更需要它

    任务式流程里，闸口天然存在：跑到需要确认的步骤就停了，
    人点"批准"才继续。**对话式没有这个天然停顿点** —— 模型说着说着
    就调了 ``mail.send``，邮件已经出去了。

    所以这里要**在工具执行之前**拦住：模型请求调用写工具时，
    我们不执行，而是落一条审批 + 推一张卡片给用户。
    用户点了才真正执行。

    这和 `gateway` 的权限检查是**两层不同的东西**：
    权限回答"这个 Agent 有没有资格发邮件"，审批回答
    "这一次、发给这个人、这个内容，人同意了吗"。
    """

    __tablename__ = "chat_approvals"
    __table_args__ = (
        # 运维看板要查"现在有几个闸口卡着"，这条索引就是为它建的
        Index("ix_chat_approval_status", "status", "created_at"),
        Index("ix_chat_approval_conv", "conversation_id", "status"),
        # 任务侧的对应索引。命名前缀跟着老名字走，理由见上面"表名"那一节。
        Index("ix_chat_approval_task", "task_no", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    approval_no: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    # ---- 归属：对话或任务，二选一 ----
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="CASCADE"), index=True
    )
    #: 任务侧用 ``task_no`` 而不是外键 id：任务对外就用 task_no，
    #: 而且历史任务可能已经被清理（对话的审批会跟着 CASCADE 删，
    #: 任务的审批要留下来当审计记录）。所以**刻意不做外键**。
    task_no: Mapped[str | None] = mapped_column(String(64), index=True)

    #: 提出这个请求的那条 assistant 消息（仅对话侧有）。
    #: 前端据此把审批卡片渲染在那条消息下面 —— 而不是在列表开头堆一堆卡片。
    message_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL")
    )

    #: 对话侧：第几轮。任务侧：第几个 todo（前端据此定位到具体步骤）。
    todo_seq: Mapped[int | None] = mapped_column(Integer)

    # ---- 待批准的这次工具调用 ----
    tool_call_id: Mapped[str] = mapped_column(String(64))
    tool_name: Mapped[str] = mapped_column(String(128))
    tool_arguments: Mapped[dict | None] = mapped_column(JSON)
    #: 给人看的一句话摘要（由工具自己生成，不是模型编的）。
    #: **不能只把 JSON 参数甩给用户** —— 让运营去读 `{"to":"a@b.com","body":"..."}`
    #: 然后点"同意"，那不叫审批，叫背锅。
    preview: Mapped[str | None] = mapped_column(Text)
    #: 只读工具不需要审批；写工具才建审批。这个字段让"为什么拦它"可查。
    reason: Mapped[str | None] = mapped_column(String(200))

    status: Mapped[str] = mapped_column(String(32), default=APPROVAL_STATUS_PENDING)

    # ---- 决定留痕（HITL 存在的全部意义）----
    decided_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    decision_reason: Mapped[str | None] = mapped_column(Text)

    #: 批准后工具真正执行的结果。驳回时为空 ——
    #: "批准了但执行失败"和"没批准"是两件事，不能都表现为空。
    execution_result: Mapped[dict | None] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
