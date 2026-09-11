"""API 响应模型（Pydantic v2）。

约定：**ORM 对象不直接返回**，一律经过这里的 schema。
理由有两个：

1. 数据库列变了不会悄悄泄漏到 API 契约上
2. ``AuditLog.args_digest``、``EmailMessage.gate_reason`` 这类字段可能含敏感内容，
   显式列出来才不会误暴露
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMBase(BaseModel):
    """所有从 ORM 直接映射的 schema 的基类。"""

    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    """统一分页信封。

    刻意不用裸数组：将来加分页游标、总数、慢查询耗时都是加字段，
    不用改客户端解析逻辑。
    """

    items: list[T]
    total: int
    page: int = 1
    page_size: int = 20

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.page_size))


# ---------------------------------------------------------------------------
# 系统
# ---------------------------------------------------------------------------
class DependencyStatus(BaseModel):
    name: str
    ok: bool
    detail: str
    latency_ms: float | None = None


class HealthResponse(BaseModel):
    status: str = Field(description="ok / degraded / down")
    app: str
    version: str
    env: str
    time: datetime
    dependencies: list[DependencyStatus]
    config: dict[str, str] = Field(description="依赖地址摘要，不含任何密码")


class SummaryResponse(BaseModel):
    """首页概览数字 —— 前端仪表盘第一屏用。"""

    products: int
    skus: int
    orders: int
    gmv_30d: Decimal
    return_rate_30d: Decimal | None
    ad_spend_30d: Decimal
    ad_roi_30d: Decimal | None
    open_tickets: int
    escalated_tickets: int
    stockout_skus: int
    running_tasks: int
    failed_tasks: int


# ---------------------------------------------------------------------------
# 业务实体
# ---------------------------------------------------------------------------
class ProductOut(ORMBase):
    id: int
    spu_code: str
    title: str
    category: str
    sub_category: str | None = None
    season: str | None = None
    style_tags: list | None = None
    cost_price: Decimal | None = None
    list_price: Decimal | None = None
    rating: Decimal | None = None
    status: str


class TaskOut(ORMBase):
    id: int
    task_no: str
    title: str
    user_request: str
    task_type: str
    status: str
    current_node: str | None = None
    progress: int
    dag: dict | None = None
    result: dict | None = None
    error_message: str | None = None
    llm_calls: int
    total_tokens: int
    total_cost: Decimal
    created_at: datetime
    finished_at: datetime | None = None


class TaskEventOut(ORMBase):
    id: int
    seq: int
    event_type: str
    node_name: str | None = None
    agent_name: str | None = None
    level: str
    message: str | None = None
    duration_ms: int | None = None
    created_at: datetime


class TicketOut(ORMBase):
    id: int
    ticket_no: str
    channel: str
    customer_nick: str | None = None
    order_no: str | None = None
    subject: str
    intent: str | None = None
    sentiment: str | None = None
    priority: str
    status: str
    handler: str
    escalated: bool
    escalate_reason: str | None = None
    created_at: datetime


class AdCampaignOut(ORMBase):
    id: int
    campaign_no: str
    platform: str
    name: str
    objective: str
    budget_daily: Decimal
    status: str
    spend: Decimal
    clicks: int
    conversions: int
    revenue: Decimal
    roi: Decimal | None = None


class BIMetricOut(ORMBase):
    id: int
    metric_date: date
    category: str
    gmv: Decimal
    order_count: int
    return_rate: Decimal | None = None
    uv: int
    conversion_rate: Decimal | None = None
    ad_spend: Decimal
    ad_roi: Decimal | None = None


class AssetOut(ORMBase):
    id: int
    asset_no: str
    asset_type: str
    name: str
    mime_type: str | None = None
    size_bytes: int | None = None
    generator: str | None = None
    review_status: str
    created_at: datetime


class KBArticleOut(ORMBase):
    id: int
    article_no: str
    title: str
    category: str
    tags: list | None = None
    version: str
