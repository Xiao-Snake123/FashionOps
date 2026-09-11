"""业务数据模型 —— 15 张表。

| # | 表 | 用途 | 主要使用方 |
| --- | --- | --- | --- |
| 1 | ``products`` | 商品 SPU | 选品 Agent / 上架 Agent |
| 2 | ``skus`` | SKU（颜色 × 尺码） | 选品 / 库存 / 上架 |
| 3 | ``inventory`` | 库存流水（进销存） | 选品（库存硬约束）/ 上架 |
| 4 | ``orders`` | 订单 | 数据分析 Agent |
| 5 | ``competitors`` | 竞品及价格快照 | 选品 Agent |
| 6 | ``bi_metrics`` | 日度经营指标 | 数据分析 / 广告投放 |
| 7 | ``kb_articles`` | 客服知识库文章 | 客服 Agent（RAG 召回源） |
| 8 | ``tickets`` | 客服工单 | 客服 Agent |
| 9 | ``tasks`` | 任务（编排入口） | 总控编排 Agent |
| 10 | ``task_events`` | 任务事件流（DAG 节点状态 / SSE） | 前端 + 编排 |
| 11 | ``agent_runs`` | Agent 运行记录（token / 耗时 / 成本） | 可观测性 + 评测 |
| 12 | ``assets`` | 素材（图片 / 视频 / 报表） | 图文 / 视频 / 数据分析 Agent |
| 13 | ``ad_campaigns`` | 广告投放 | 广告投放 Agent |
| 14 | ``email_messages`` | 邮件记录 | 邮件通知 Agent |
| 15 | ``audit_logs`` | 审计日志（工具网关） | 工具网关 |

设计约定见 ``base.py``：时间存 naive UTC、金额用 ``Numeric``、比例用 0~1 小数。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
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
# 统一的列类型。SQLAlchemy 的 TypeEngine 实例是无状态的，可以安全共享。
# ---------------------------------------------------------------------------
MONEY = Numeric(14, 2)  # 金额：两位小数
RATIO = Numeric(8, 6)  # 比例：0~1，六位小数
RATE = Numeric(6, 2)  # 百分比/评分：如 4.85 分、12.50%
COST = Numeric(12, 6)  # LLM 成本：单次调用可能只有 0.000123 元，精度必须够


# ===========================================================================
# 1. 商品 SPU
# ===========================================================================
class Product(Base):
    """商品 SPU（标准产品单位）。

    服装行业的关键字段是季节 + 面料 + 风格标签 —— 选品 Agent 的
    "相似竞品匹配"和"季度适销性打分"都要靠这几个维度。
    """

    __tablename__ = "products"
    __table_args__ = (
        Index("ix_products_category_season", "category", "season"),
        Index("ix_products_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    spu_code: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    title: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(64), comment="一级类目：女装/男装/童装")
    sub_category: Mapped[str | None] = mapped_column(
        String(64), comment="二级类目：连衣裙/毛衣/风衣"
    )
    season: Mapped[str | None] = mapped_column(String(32), comment="春季/夏季/秋季/冬季/四季")
    brand: Mapped[str | None] = mapped_column(String(64))

    style_tags: Mapped[list | None] = mapped_column(JSON, comment="风格标签：法式/通勤/休闲/辣妹")
    target_audience: Mapped[str | None] = mapped_column(
        String(128), comment="目标人群：18-25 学生党"
    )
    fabric: Mapped[str | None] = mapped_column(String(128), comment="面料成分")

    cost_price: Mapped[Decimal | None] = mapped_column(MONEY, comment="生产成本")
    list_price: Mapped[Decimal | None] = mapped_column(MONEY, comment="吊牌价")
    rating: Mapped[Decimal | None] = mapped_column(RATE, comment="站内评分，0~5")

    status: Mapped[str] = mapped_column(
        String(32), default="draft", comment="draft/ready/on_sale/off_shelf"
    )
    launch_date: Mapped[date | None] = mapped_column(Date)
    description: Mapped[str | None] = mapped_column(Text)
    image_urls: Mapped[list | None] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    skus: Mapped[list[SKU]] = relationship(
        back_populates="product", cascade="all, delete-orphan", lazy="selectin"
    )


# ===========================================================================
# 2. SKU
# ===========================================================================
class SKU(Base):
    """最小库存单位 = 颜色 × 尺码。

    为什么必须拆出来：库存、订单、上架都挂在 SKU 上。
    把颜色尺码塞进商品表用 JSON 存，库存就永远对不上。
    """

    __tablename__ = "skus"
    __table_args__ = (
        UniqueConstraint("product_id", "color", "size", name="uq_sku_product_color_size"),
        Index("ix_skus_barcode", "barcode"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    sku_code: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    color: Mapped[str] = mapped_column(String(32))
    size: Mapped[str] = mapped_column(String(32), comment="XS/S/M/L/XL/XXL 或 均码")

    cost_price: Mapped[Decimal | None] = mapped_column(MONEY)
    sale_price: Mapped[Decimal | None] = mapped_column(MONEY, comment="实际售卖价（含促销）")
    barcode: Mapped[str | None] = mapped_column(String(64))
    weight_gram: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    product: Mapped[Product] = relationship(back_populates="skus")


# ===========================================================================
# 3. 库存流水
# ===========================================================================
class Inventory(Base):
    """库存变动流水（进销存）。

    刻意做成**流水表而不是余额表**：余额可以从流水算出来，
    但流水算不出来余额。选品 Agent 判断"能不能上这个款"时既要看当前可售量，
    也要看近 30 天动销速度 —— 只有流水表才给得出动销。
    """

    __tablename__ = "inventory"
    __table_args__ = (
        Index("ix_inventory_sku_occurred", "sku_id", "occurred_at"),
        Index("ix_inventory_change_type", "change_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku_id: Mapped[int] = mapped_column(ForeignKey("skus.id", ondelete="CASCADE"), index=True)
    warehouse: Mapped[str] = mapped_column(String(64), default="主仓")

    change_type: Mapped[str] = mapped_column(
        String(32),
        comment="in 入库 / out 出库 / sale 销售 / return 退货 / adjust 盘盈亏 / lock 锁定",
    )
    quantity: Mapped[int] = mapped_column(Integer, comment="变动数量，正数增加、负数减少")
    quantity_after: Mapped[int] = mapped_column(Integer, comment="变动后可用库存，冗余存以便直接读")

    ref_no: Mapped[str | None] = mapped_column(String(64), comment="关联单号：订单号/入库单号")
    note: Mapped[str | None] = mapped_column(String(255))
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ===========================================================================
# 4. 订单
# ===========================================================================
class Order(Base):
    """订单。

    ``channel`` 决定数据分析 Agent 的归因口径；
    ``status`` 里刻意保留 ``refunding``/``refunded`` —— 服装类目退货率
    普遍 25%~40%，这是这个行业最重要的一个指标，不能忽略。
    """

    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_paid_at", "paid_at"),
        Index("ix_orders_status_channel", "status", "channel"),
        Index("ix_orders_sku", "sku_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    platform: Mapped[str] = mapped_column(String(32), default="mock", comment="douyin/taobao/mock")

    buyer_nick: Mapped[str | None] = mapped_column(String(64))
    is_new_customer: Mapped[bool] = mapped_column(Boolean, default=True)

    sku_id: Mapped[int | None] = mapped_column(ForeignKey("skus.id", ondelete="SET NULL"))
    product_title: Mapped[str | None] = mapped_column(
        String(255), comment="下单时快照，商品改名后历史订单仍要显示原名"
    )
    quantity: Mapped[int] = mapped_column(Integer, default=1)

    unit_price: Mapped[Decimal] = mapped_column(MONEY)
    total_amount: Mapped[Decimal] = mapped_column(MONEY, comment="原价合计")
    discount_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    actual_amount: Mapped[Decimal] = mapped_column(MONEY, comment="实付")

    status: Mapped[str] = mapped_column(
        String(32),
        default="paid",
        comment="paid/shipped/completed/refunding/refunded/cancelled",
    )
    channel: Mapped[str] = mapped_column(
        String(64), default="自然流量", comment="自然流量/搜索/直播/广告/私域"
    )

    province: Mapped[str | None] = mapped_column(String(32))
    city: Mapped[str | None] = mapped_column(String(32))

    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ===========================================================================
# 5. 竞品
# ===========================================================================
class Competitor(Base):
    """竞品及其价格快照。

    ``price_history`` 存 JSON 数组 ``[{date, price}, ...]`` —— 不做成独立表
    是因为选品 Agent 只需要"近 30 天价格走势"这一个整体概念，
    拆表后每次都要 JOIN 聚合，收益不明显。
    """

    __tablename__ = "competitors"
    __table_args__ = (
        Index("ix_competitors_category_season", "category", "season"),
        Index("ix_competitors_platform", "platform"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(32), default="mock")
    shop_name: Mapped[str | None] = mapped_column(String(128))
    product_title: Mapped[str] = mapped_column(String(255))

    category: Mapped[str] = mapped_column(String(64))
    sub_category: Mapped[str | None] = mapped_column(String(64))
    season: Mapped[str | None] = mapped_column(String(32))
    style_tags: Mapped[list | None] = mapped_column(JSON)

    price: Mapped[Decimal] = mapped_column(MONEY)
    original_price: Mapped[Decimal | None] = mapped_column(MONEY)
    monthly_sales: Mapped[int] = mapped_column(Integer, default=0)
    review_count: Mapped[int] = mapped_column(Integer, default=0)
    rating: Mapped[Decimal | None] = mapped_column(RATE)

    url: Mapped[str | None] = mapped_column(String(512))
    price_history: Mapped[list | None] = mapped_column(JSON, comment="[{date, price}] 近 30 天")

    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ===========================================================================
# 6. 日度经营指标
# ===========================================================================
class BIMetric(Base):
    """日度经营指标（按类目切分）。

    ``(metric_date, category)`` 唯一。广告投放 Agent 调预算前必须先看这张表
    判断"这个类目最近 ROI 是在涨还是在跌"。
    """

    __tablename__ = "bi_metrics"
    __table_args__ = (
        UniqueConstraint("metric_date", "category", name="uq_bi_date_category"),
        Index("ix_bi_metric_date", "metric_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    metric_date: Mapped[date] = mapped_column(Date)
    category: Mapped[str] = mapped_column(String(64), default="全部")

    gmv: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    order_count: Mapped[int] = mapped_column(Integer, default=0)
    customer_count: Mapped[int] = mapped_column(Integer, default=0)
    avg_order_value: Mapped[Decimal | None] = mapped_column(MONEY)
    refund_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    return_rate: Mapped[Decimal | None] = mapped_column(RATIO, comment="退货率，0~1")

    uv: Mapped[int] = mapped_column(Integer, default=0, comment="访客数")
    pv: Mapped[int] = mapped_column(Integer, default=0, comment="浏览量")
    conversion_rate: Mapped[Decimal | None] = mapped_column(RATIO, comment="转化率，0~1")

    ad_spend: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    ad_revenue: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    ad_roi: Mapped[Decimal | None] = mapped_column(RATE, comment="广告 ROI")

    stockout_sku_count: Mapped[int] = mapped_column(Integer, default=0, comment="断货 SKU 数")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ===========================================================================
# 7. 客服知识库
# ===========================================================================
class KBArticle(Base):
    """知识库文章 —— 客服 Agent 的 RAG 召回源。

    ``retrieval_text`` 是专门喂给 embedding 的文本，和 ``content`` 分开：
    标题 + 关键词的拼接往往比正文裸文本检索效果更好（正文有大量寒暄废话）。
    """

    __tablename__ = "kb_articles"
    __table_args__ = (
        Index("ix_kb_category_active", "category", "is_active"),
        Index("ix_kb_article_no", "article_no"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    article_no: Mapped[str] = mapped_column(String(64), unique=True)

    title: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(
        String(64), comment="尺码/退换货/物流/材质/发票/优惠/支付"
    )
    content: Mapped[str] = mapped_column(Text)
    retrieval_text: Mapped[str | None] = mapped_column(
        Text, comment="喂给 embedding 的文本（标题+关键词+正文摘要）"
    )
    tags: Mapped[list | None] = mapped_column(JSON)

    version: Mapped[str] = mapped_column(String(16), default="v1")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ===========================================================================
# 8. 客服工单
# ===========================================================================
class Ticket(Base):
    """客服工单。

    刻意埋了 ``escalated`` / ``handler='human'`` 这些字段：
    演示时必须能看到"情绪激动 + 涉及金额纠纷 → 自动转人工"这条链路，
    否则整个客服 Agent 演示不出价值。
    """

    __tablename__ = "tickets"
    __table_args__ = (
        Index("ix_tickets_status_priority", "status", "priority"),
        Index("ix_tickets_intent", "intent"),
        Index("ix_tickets_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_no: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    channel: Mapped[str] = mapped_column(
        String(32), default="mock", comment="douyin/taobao/wechat/phone"
    )
    customer_nick: Mapped[str | None] = mapped_column(String(64))
    order_no: Mapped[str | None] = mapped_column(String(64), index=True)

    subject: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    intent: Mapped[str | None] = mapped_column(
        String(64), comment="退换货/尺码咨询/物流查询/质量投诉/发票/催发货"
    )
    sentiment: Mapped[str | None] = mapped_column(
        String(16), comment="positive/neutral/negative/angry"
    )
    priority: Mapped[str] = mapped_column(
        String(16), default="normal", comment="low/normal/high/urgent"
    )

    status: Mapped[str] = mapped_column(
        String(32), default="open", comment="open/processing/waiting/transferred/closed"
    )
    handler: Mapped[str] = mapped_column(String(32), default="agent", comment="agent/human")

    escalated: Mapped[bool] = mapped_column(Boolean, default=False)
    escalate_reason: Mapped[str | None] = mapped_column(String(255))
    resolution: Mapped[str | None] = mapped_column(Text)
    cited_article_nos: Mapped[list | None] = mapped_column(
        JSON, comment="RAG 命中的知识库文章号，用于评估召回质量"
    )
    confidence: Mapped[Decimal | None] = mapped_column(RATIO, comment="Agent 自评置信度")

    first_response_seconds: Mapped[int | None] = mapped_column(Integer)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# ===========================================================================
# 9. 任务
# ===========================================================================
class Task(Base):
    """任务 —— 编排平台的入口。

    ``dag`` 存拆解出来的 DAG 定义，``state`` 存 LangGraph state 的关键字段快照
    （真正的 checkpoint 在 Redis 里，按 ``thread_id = task_no`` 存）。
    """

    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_status_created", "status", "created_at"),
        Index("ix_tasks_type", "task_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_no: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    title: Mapped[str] = mapped_column(String(255))
    user_request: Mapped[str] = mapped_column(Text, comment="用户原始自然语言需求")
    task_type: Mapped[str] = mapped_column(
        String(64),
        default="generic",
        comment="new_product_launch/daily_report/after_sale/ad_optimize",
    )

    status: Mapped[str] = mapped_column(
        String(32),
        default="pending",
        comment="pending/running/waiting_approval/succeeded/failed/cancelled",
    )
    current_node: Mapped[str | None] = mapped_column(String(64))
    progress: Mapped[int] = mapped_column(Integer, default=0, comment="0~100")
    priority: Mapped[int] = mapped_column(Integer, default=5, comment="1 最高，9 最低")

    dag: Mapped[dict | None] = mapped_column(JSON, comment="DAG 定义：nodes + edges")
    state: Mapped[dict | None] = mapped_column(JSON, comment="LangGraph state 关键字段快照")
    result: Mapped[dict | None] = mapped_column(JSON, comment="最终产出摘要")
    error_message: Mapped[str | None] = mapped_column(Text)

    thread_id: Mapped[str | None] = mapped_column(
        String(64), comment="LangGraph checkpoint 的 thread_id，默认等于 task_no"
    )
    approval_payload: Mapped[dict | None] = mapped_column(
        JSON, comment="HITL 中断时挂起的待确认内容"
    )

    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_cost: Mapped[Decimal] = mapped_column(COST, default=Decimal("0"))

    created_by: Mapped[str] = mapped_column(String(64), default="user")
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# ===========================================================================
# 10. 任务事件流
# ===========================================================================
class TaskEvent(Base):
    """任务事件流 —— DAG 节点状态流转记录。

    两个用途：
    1. 前端 SSE 实时推送（按 ``seq`` 递增拉取即可，天然支持断线重连补数据）
    2. 事后错误归因：Agent 在哪一步开始跑偏，一看事件流就知道
    """

    __tablename__ = "task_events"
    __table_args__ = (
        UniqueConstraint("task_id", "seq", name="uq_task_event_seq"),
        Index("ix_task_events_task_seq", "task_id", "seq"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer, comment="任务内单调递增序号，SSE 靠它断点续传")

    event_type: Mapped[str] = mapped_column(
        String(32),
        comment="node_start/node_end/llm_call/tool_call/interrupt/resume/error/log/approval",
    )
    node_name: Mapped[str | None] = mapped_column(String(64))
    agent_name: Mapped[str | None] = mapped_column(String(64))
    level: Mapped[str] = mapped_column(String(16), default="info", comment="debug/info/warn/error")

    message: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# ===========================================================================
# 11. Agent 运行记录
# ===========================================================================
class AgentRun(Base):
    """单次 Agent 执行记录。

    这张表是**评测和成本归因的数据源**：
    成功率、平均步数、P95 延迟、token 成本 —— 面试要给的四个数字全从这里算。
    """

    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_agent_created", "agent_name", "created_at"),
        Index("ix_agent_runs_task", "task_id"),
        Index("ix_agent_runs_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True, comment="关联 OTel trace")

    agent_name: Mapped[str] = mapped_column(String(64), index=True)
    node_name: Mapped[str | None] = mapped_column(String(64))
    run_index: Mapped[int] = mapped_column(
        Integer, default=1, comment="同一节点第几次执行（反思迭代会 +1）"
    )

    model: Mapped[str | None] = mapped_column(String(64))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[Decimal] = mapped_column(COST, default=Decimal("0"), comment="人民币元")

    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(16), default="success", comment="success/error/timeout"
    )

    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    input_digest: Mapped[str | None] = mapped_column(String(64), comment="输入哈希，用于评测去重")
    output_digest: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ===========================================================================
# 12. 素材
# ===========================================================================
class Asset(Base):
    """素材：生成图、生成视频、Excel 报表。

    ``review_status`` 是图文/视频 Agent 的质检闭环落点：
    生成 → 自检不通过 → 重绘 → 人工抽检。
    """

    __tablename__ = "assets"
    __table_args__ = (
        Index("ix_assets_type_created", "asset_type", "created_at"),
        Index("ix_assets_task", "task_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_no: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))

    asset_type: Mapped[str] = mapped_column(
        String(32), comment="image/video/report/spreadsheet/copywriting"
    )
    name: Mapped[str] = mapped_column(String(255))

    storage_backend: Mapped[str] = mapped_column(String(32), default="local", comment="local/minio")
    storage_path: Mapped[str] = mapped_column(String(512), comment="相对存储根的路径")
    mime_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)

    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))

    source: Mapped[str] = mapped_column(
        String(32), default="generated", comment="generated/uploaded"
    )
    generator: Mapped[str | None] = mapped_column(String(64), comment="产出它的 Agent 名")
    prompt: Mapped[str | None] = mapped_column(Text, comment="生成用的原始 prompt，便于复现")

    review_status: Mapped[str] = mapped_column(
        String(32), default="pending", comment="pending/approved/rejected"
    )
    review_note: Mapped[str | None] = mapped_column(String(255))
    regenerate_count: Mapped[int] = mapped_column(Integer, default=0)

    meta: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ===========================================================================
# 13. 广告投放
# ===========================================================================
class AdCampaign(Base):
    """广告投放计划。

    改预算属于高危操作，必须走 HITL 闸口 —— 所以这张表里有
    ``approved_by`` / ``approved_at`` 和 ``status='pending_approval'`` 这套状态。
    """

    __tablename__ = "ad_campaigns"
    __table_args__ = (
        Index("ix_ad_status_start", "status", "start_at"),
        Index("ix_ad_platform", "platform"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_no: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))

    platform: Mapped[str] = mapped_column(String(32), default="mock", comment="juliang/mock")
    name: Mapped[str] = mapped_column(String(255))
    objective: Mapped[str] = mapped_column(String(32), default="gmv", comment="gmv/roi/clicks/uv")

    budget_daily: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    bid: Mapped[Decimal | None] = mapped_column(MONEY, comment="出价")
    start_at: Mapped[datetime | None] = mapped_column(DateTime)
    end_at: Mapped[datetime | None] = mapped_column(DateTime)

    status: Mapped[str] = mapped_column(
        String(32),
        default="draft",
        comment="draft/pending_approval/running/paused/ended/rejected",
    )

    # 投放效果（Mock 适配器会灌入有涨有跌的数据，用来演示"ROI 暴跌自动停投"）
    spend: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    conversions: Mapped[int] = mapped_column(Integer, default=0)
    revenue: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    roi: Mapped[Decimal | None] = mapped_column(RATE)

    target_audience: Mapped[dict | None] = mapped_column(JSON)
    creative_asset_nos: Mapped[list | None] = mapped_column(JSON)

    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    reject_reason: Mapped[str | None] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# ===========================================================================
# 14. 邮件记录
# ===========================================================================
class EmailMessage(Base):
    """邮件收发记录。

    ``gate_decision`` / ``gate_reason`` 是四层发送闸门的落库点 ——
    面试时可以直接展示"这封邮件为什么被拦下来了"。
    """

    __tablename__ = "email_messages"
    __table_args__ = (
        Index("ix_email_status_created", "status", "created_at"),
        Index("ix_email_task", "task_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_no: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))

    direction: Mapped[str] = mapped_column(
        String(8), default="outbound", comment="outbound/inbound"
    )
    to_addrs: Mapped[list | None] = mapped_column(JSON)
    cc_addrs: Mapped[list | None] = mapped_column(JSON)
    bcc_addrs: Mapped[list | None] = mapped_column(JSON)
    from_addr: Mapped[str | None] = mapped_column(String(255))

    subject: Mapped[str | None] = mapped_column(String(255))
    body_text: Mapped[str | None] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text)
    attachments: Mapped[list | None] = mapped_column(JSON, comment="[{filename, asset_no, size}]")

    status: Mapped[str] = mapped_column(
        String(32), default="draft", comment="draft/queued/sent/failed/received/blocked"
    )
    gate_decision: Mapped[str | None] = mapped_column(
        String(32), comment="allowed/dry_run/blocked_whitelist/blocked_ratelimit/blocked_size"
    )
    gate_reason: Mapped[str | None] = mapped_column(String(255))

    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ===========================================================================
# 15. 审计日志
# ===========================================================================
class AuditLog(Base):
    """工具网关审计日志。

    一次工具调用一行：谁（actor）、调了什么（tool_name）、带什么参数（脱敏后）、
    结果如何（status）、花了多久（duration_ms）。

    写审计**绝不能失败即中断业务** —— 所以调用方要吞掉这里的异常，
    只记 warning（这条原则是从 ``mcp-servers`` 的 ``common/audit.py`` 带过来的）。
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_trace", "trace_id"),
        Index("ix_audit_tool_created", "tool_name", "created_at"),
        Index("ix_audit_status", "status"),
        Index("ix_audit_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))

    actor: Mapped[str] = mapped_column(String(64), comment="agent 名 / user / system")
    tool_name: Mapped[str] = mapped_column(String(128))
    action: Mapped[str | None] = mapped_column(String(64), comment="read/write/execute/approve")
    resource: Mapped[str | None] = mapped_column(String(255))

    args_digest: Mapped[dict | None] = mapped_column(JSON, comment="脱敏后的入参快照")
    result_summary: Mapped[str | None] = mapped_column(String(512))

    status: Mapped[str] = mapped_column(
        String(16), default="success", comment="success/error/blocked"
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# 表清单（建表、文档、健康检查都用这一份，避免各处手写列表对不上）
# ---------------------------------------------------------------------------
ALL_MODELS: tuple[type[Base], ...] = (
    Product,
    SKU,
    Inventory,
    Order,
    Competitor,
    BIMetric,
    KBArticle,
    Ticket,
    Task,
    TaskEvent,
    AgentRun,
    Asset,
    AdCampaign,
    EmailMessage,
    AuditLog,
)
"""15 张表，顺序即依赖顺序（父表在前，方便按序清理数据）。"""

__all__ = [
    "ALL_MODELS",
    "AdCampaign",
    "AgentRun",
    "Asset",
    "AuditLog",
    "BIMetric",
    "Competitor",
    "EmailMessage",
    "Inventory",
    "KBArticle",
    "Order",
    "Product",
    "SKU",
    "Task",
    "TaskEvent",
    "Ticket",
]
