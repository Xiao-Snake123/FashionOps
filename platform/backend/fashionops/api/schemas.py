"""API 响应模型（Pydantic v2）。

约定：**ORM 对象不直接返回**，一律经过这里的 schema。
理由有两个：

1. 数据库列变了不会悄悄泄漏到 API 契约上
2. ``AuditLog.args_digest``、``EmailMessage.gate_reason`` 这类字段可能含敏感内容，
   显式列出来才不会误暴露
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

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

    @field_validator("result", mode="before")
    @classmethod
    def _coerce_result(cls, value: object) -> object:
        """容忍历史行里 `result` 被写成**字符串**的情况。

        库里有一行（``T260908009``）的 ``result`` 存的是字符串 ``"null"``
        —— 那是早期把 JSON 列当文本写进去留下的。``result`` 声明成
        ``dict | None``，拿到字符串就 500，而且**只在这一行落进分页区间时**
        才炸（``page_size=200`` 才覆盖得到它），所以这个 bug 藏了很久。

        处理原则是 §8.5 那条：**别为一行演示脏数据写迁移脚本**，
        在读取时兼容成本最低。解析不出来就当 None ——
        结果字段缺失只会让"产出"那一块少显示一点，
        比整个列表页 500 好得多。
        """
        if not isinstance(value, str):
            return value
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @field_validator("dag", mode="before")
    @classmethod
    def _normalize_dag(cls, value: object) -> object:
        """库里的 DAG 有新、旧两种形状，在**唯一出口**统一成新形状。

        放这里（而不是只在详情接口里调一次）的原因是：列表页和详情页
        必须拿到同一种形状，否则前端每处渲染都要写两套分支。
        """
        from fashionops.agents.recorder import normalize_dag

        if isinstance(value, dict):
            return normalize_dag(value)
        return value


class TaskTodoOut(ORMBase):
    """任务里的一步 —— 详情页的 todo 列表读这个。

    ## 为什么"计划"和"进度"合在一个对象里

    旧架构里这两个是分开的：计划来自 ``tasks.dag``（静态、模型给的图），
    进度靠从事件流里反推节点状态（间接、还要处理"最后一条 node_start
    未必是当前节点"这类猜测）。

    新架构里 ``task_todos`` 既有目标、又有状态和结果 ——
    **它同时就是计划和进度**，前端不需要两套数据，也不需要猜。
    所以这里直接把库里的行给出去，不做任何再加工。
    """

    seq: int
    goal: str
    kind: str
    status: str
    #: 这一步的结论（模型写的、给下一步看的）
    result_summary: str | None = None
    artifact_nos: list | None = None
    tool_names: list | None = None
    error: str | None = None
    #: 卡在闸口时指向 approvals.approval_no
    approval_no: str | None = None
    llm_calls: int
    total_tokens: int
    total_cost: Decimal
    duration_ms: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class TaskDetailOut(TaskOut):
    """任务详情。在列表字段之上补「挂起状态」和「审批载荷」。"""

    #: 非空 = 任务停在人工闸口，前端据此弹确认框
    approval_payload: dict | None = None
    #: 进程内是否有线程正在跑它（和 status 不同：status 落库有延迟）
    is_running: bool = False
    #: 还在迁移态（pending/running/waiting_approval）—— 即"值得继续轮询"
    is_pending: bool = False
    started_at: datetime | None = None
    #: 有序的 todo 列表。旧任务（还没有 todo 行）给空列表，
    #: 前端据此知道"这个任务是用旧架构跑的"，不用靠形状猜。
    todos: list[TaskTodoOut] = []


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

    # ---- 客服 Agent 的产出（2026-09-16 补）----
    # 这些字段表里一直有，只是原来没对外暴露。但**它们才是工单页要给人看的东西**：
    # 光看"意图=退换货、情绪=angry、转人工"只能知道结论，
    # 看不到"Agent 打算怎么回""依据是哪几篇"—— 而那正是人来判断能不能放行的依据。
    content: str | None = None
    resolution: str | None = None
    confidence: float | None = None
    cited_article_nos: list[str] | None = None
    #: Agent 最近一次处理时间（空 = 没被 Agent 处理过）
    agent_handled_at: datetime | None = None
    #: 走的是哪条路：fast（代码快车道，0 次模型调用）/ agent（工具循环）
    agent_lane: str | None = None


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
    #: 存储 key。不直接给前端拼 URL，而是下面用 download_url 算好 ——
    #: 让前端知道存储路径的话，一旦存储后端从 local 换成 MinIO，
    #: 拼 URL 的规则要同步改前端代码，而前端不该知道存储层的实现细节。
    storage_path: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def download_url(self) -> str:
        """可直接点开的下载地址（指向 ``/api/v1/assets/raw/{key}``）。"""
        return f"/api/v1/assets/raw/{self.storage_path}" if self.storage_path else ""


class EmailOut(ORMBase):
    """邮件记录。

    **被闸门拦下的邮件也要出现在列表里** —— 这是四层闸门设计最该展示的地方。
    只展示"成功发出的"，运维就看不到"还有几封被策略挡住、而业务方以为已通知"。
    """

    id: int
    message_no: str
    direction: str
    to_addrs: list | None = None
    cc_addrs: list | None = None
    bcc_addrs: list | None = None
    from_addr: str | None = None
    subject: str | None = None
    body_text: str | None = None
    attachments: list | None = None
    status: str
    gate_decision: str | None = None
    gate_reason: str | None = None
    error_message: str | None = None
    sent_at: datetime | None = None
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def blocked(self) -> bool:
        """是不是被策略拦下的。前端拿它决定要不要标红。"""
        return bool(self.gate_decision and self.gate_decision.startswith("blocked"))


class KBArticleOut(ORMBase):
    id: int
    article_no: str
    title: str
    category: str
    tags: list | None = None
    version: str
