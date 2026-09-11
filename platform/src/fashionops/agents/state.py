"""LangGraph 状态定义。

## 最重要的一条约束：状态必须可序列化

LangGraph 的 checkpointer 会把状态**写进 Redis**（或者 SQLite）以实现
"任务中断可恢复"。所以状态里只能放 JSON 能表达的东西：字符串、数字、
布尔、列表、字典、以及能序列化的 Pydantic 模型。

**绝对不能放**：数据库 Session、HTTP 客户端、工具网关、模型客户端。
这些对象一旦进了状态，checkpoint 就写不进去；就算能写，
从 checkpoint 恢复时反序列化出来的也是个残缺对象，会在很难预料的地方炸。

那这些依赖怎么传给节点？走 ``config["configurable"]`` —— 见文件末尾的
``RunContext``。这是 LangGraph 的惯用法：**状态是数据，上下文是依赖**。

## 为什么用 reducer

有些字段是"往列表里追加"的语义（执行轨迹、产出物、错误）。
如果直接赋值，每次节点更新都会把上一次的覆盖掉。
用 ``Annotated[list[X], operator.add]`` 声明后，LangGraph 会把这些
更新**合并**而不是替换 —— 这才符合"日志"的直觉。

``messages`` 用 LangChain 的 ``add_messages``，它比 ``operator.add`` 更聪明：
能按 id 去重、能处理"更新已有消息"。
"""

from __future__ import annotations

import copy
import operator
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

from fashionops.db.base import utcnow

# ---------------------------------------------------------------------------
# 状态里携带的结构化对象
# ---------------------------------------------------------------------------
TaskStatus = Literal[
    "pending", "running", "waiting_approval", "succeeded", "failed", "cancelled"
]

NodeStatus = Literal["pending", "running", "succeeded", "failed", "skipped"]


class Artifact(BaseModel):
    """Agent 产出的东西：报表、素材、上架结果……

    刻意记 ``produced_by`` 和 ``produced_at`` ——
    出错时要能回答"这个错误结论是哪个 Agent 在什么时候产出的"。
    """

    kind: Literal["report", "image", "video", "copywriting", "listing", "metric", "other"]
    name: str
    ref: str = Field(description="引用标识：asset_no / listing_id / 指标名")
    produced_by: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)
    produced_at: datetime = Field(default_factory=utcnow)


class StepRecord(BaseModel):
    """一次节点执行的轨迹。

    这就是"执行轨迹"落库前的形态。有了它，事后复盘才说得清
    "在哪一步、哪个 Agent、用了什么参数、得到了什么结论"。
    """

    node: str
    agent: str
    status: NodeStatus = "pending"
    summary: str = ""
    tool_calls: list[str] = Field(default_factory=list)
    attempts: int = 1
    duration_ms: int = 0
    tokens: int = 0
    cost: Decimal = Decimal("0")
    error: str | None = None
    started_at: datetime = Field(default_factory=utcnow)


class ApprovalRequest(BaseModel):
    """挂在 HITL 闸口上等待人工确认的内容。

    设计要点：**必须写清"要不要批准什么"和"批了会发生什么"**。
    只说"需要确认"的审批请求，人根本没法做决定。
    """

    node: str
    action: str = Field(description="要执行的动作，如「把日预算从 800 调到 1500」")
    reason: str = Field(description="为什么需要这一步 —— 给审批人看的理由")
    risk: Literal["low", "medium", "high"] = "medium"
    payload: dict[str, Any] = Field(default_factory=dict, description="具体参数，供审批人核对")
    requested_by: str = ""
    requested_at: datetime = Field(default_factory=utcnow)
    #: 审批结果由 resume 时写回
    approved: bool | None = None
    approved_by: str | None = None
    reject_reason: str | None = None


# ---------------------------------------------------------------------------
# 主状态
# ---------------------------------------------------------------------------
class TaskState(TypedDict, total=False):
    """跑一个任务时在 9 个 Agent 之间流转的状态。"""

    # ---- 任务标识 ----
    # step 只增不减，前端可以拿它做"进度条到哪了"
    task_no: str
    thread_id: str
    user_request: str
    task_type: str

    # ---- 对话 ----
    # add_messages 而不是 operator.add：能按 id 去重、能更新已有消息
    messages: Annotated[list[AnyMessage], add_messages]

    # ---- 编排 ----
    plan: dict[str, Any] | None
    """总控 Agent 拆出来的 DAG：``{"nodes": [...], "edges": [...]}``"""

    current_node: str
    completed_nodes: Annotated[list[str], operator.add]
    """**累积**而不是替换：从 checkpoint 恢复后还要知道之前跑过哪些节点。"""

    route: str | None
    """条件边用的路由决策，由当前节点写入、由路由函数读取。"""

    # ---- 中间数据 ----
    # 这个名字很重要：这里放"给下一个 Agent 用的精炼结论"，不是原始数据。
    # 原始数据太大，会撑爆上下文；精炼结论才是 Agent 之间该传递的东西。
    findings: dict[str, Any]
    """各节点写入的结构化结论，键是节点名。"""

    candidates: list[dict[str, Any]]
    selected: list[dict[str, Any]]

    # ---- 产出 ----
    artifacts: Annotated[list[Artifact], operator.add]

    # ---- 人工闸口 ----
    approval: ApprovalRequest | None
    pending_approval: bool

    # ---- 轨迹与错误 ----
    steps: Annotated[list[StepRecord], operator.add]
    errors: Annotated[list[str], operator.add]
    retry_count: int

    # ---- 预算 ----
    # 预算必须在状态里，才能跨节点累计、跨 checkpoint 保留。
    # 没有预算约束的 Agent 会一直重试到把配额烧光。
    llm_calls: int
    total_tokens: int
    total_cost: Decimal
    budget_tokens: int
    budget_cost: Decimal

    # ---- 生命周期 ----
    status: TaskStatus
    progress: int
    result: dict[str, Any] | None
    started_at: datetime | None
    finished_at: datetime | None


#: 新建状态时的默认值。集中一处，避免每个入口各写一遍。
STATE_DEFAULTS: dict[str, Any] = {
    "messages": [],
    "completed_nodes": [],
    "artifacts": [],
    "steps": [],
    "errors": [],
    "findings": {},
    "candidates": [],
    "selected": [],
    "plan": None,
    "route": None,
    "approval": None,
    "pending_approval": False,
    "retry_count": 0,
    "llm_calls": 0,
    "total_tokens": 0,
    "total_cost": Decimal("0"),
    "budget_tokens": 500_000,
    "budget_cost": Decimal("5.0"),
    "current_node": "",
    "status": "pending",
    "progress": 0,
    "result": None,
    "started_at": None,
    "finished_at": None,
    "task_type": "generic",
}


def new_state(
    *,
    task_no: str,
    user_request: str,
    task_type: str = "generic",
    budget_tokens: int | None = None,
    budget_cost: Decimal | None = None,
) -> TaskState:
    """造一份初始状态。

    **必须是深拷贝。** ``{**STATE_DEFAULTS}`` 只是浅拷贝，
    里面的 list / dict 仍然是同一个对象 —— 于是两个任务会共享同一个
    ``steps`` 列表，A 任务追加的轨迹出现在 B 任务里。
    这类"任务之间串数据"的 bug 非常难查，因为它只在并发或者连续处理时暴露。
    """
    state: TaskState = copy.deepcopy(STATE_DEFAULTS)  # type: ignore[assignment]
    state["task_no"] = task_no
    state["thread_id"] = task_no  # 约定：thread_id 就用 task_no，方便反查
    state["user_request"] = user_request
    state["task_type"] = task_type
    state["status"] = "running"
    state["started_at"] = utcnow()
    if budget_tokens is not None:
        state["budget_tokens"] = budget_tokens
    if budget_cost is not None:
        state["budget_cost"] = budget_cost
    return state


# ---------------------------------------------------------------------------
# 预算
# ---------------------------------------------------------------------------
class BudgetExceeded(RuntimeError):
    """预算用完了。

    这**不是一个技术故障**，而是一个业务决策点 —— 上层应该把它当作
    "需要人工介入"来处理，而不是当作异常重试。
    """


def budget_status(state: TaskState) -> dict[str, Any]:
    """当前预算使用情况。节点开头应该先检查它。"""
    used_tokens = int(state.get("total_tokens", 0))
    used_cost = Decimal(str(state.get("total_cost", 0)))
    max_tokens = int(state.get("budget_tokens", 0))
    max_cost = Decimal(str(state.get("budget_cost", 0)))

    token_ratio = used_tokens / max_tokens if max_tokens else 0.0
    cost_ratio = float(used_cost / max_cost) if max_cost else 0.0
    ratio = max(token_ratio, cost_ratio)

    return {
        "used_tokens": used_tokens,
        "max_tokens": max_tokens,
        "used_cost": float(used_cost),
        "max_cost": float(max_cost),
        "ratio": round(ratio, 4),
        "exhausted": ratio >= 1.0,
        # 提前预警阈值：留出余量让上层能优雅收尾（输出已有结论）而不是硬中断
        "warning": ratio >= 0.8,
    }


def check_budget(state: TaskState) -> None:
    """预算用完就抛 ``BudgetExceeded``。"""
    status = budget_status(state)
    if status["exhausted"]:
        raise BudgetExceeded(
            f"任务预算已用完（tokens {status['used_tokens']}/{status['max_tokens']}，"
            f"成本 ¥{status['used_cost']:.4f}/¥{status['max_cost']:.2f}）"
        )


# ---------------------------------------------------------------------------
# 依赖上下文（**不属于状态**）
# ---------------------------------------------------------------------------
class RunContext:
    """节点执行时需要的依赖集合。

    **刻意不放进 TaskState**：网关、模型客户端、数据库会话这些东西
    无法序列化，塞进状态会让 checkpoint 写失败。它们通过
    ``config["configurable"]["ctx"]`` 传给节点。

    这也让测试变简单了：换一个 ``RunContext`` 就能把网关和模型全换成假的，
    不用去构造一份假状态。
    """

    def __init__(
        self,
        *,
        gateway: Any,
        model: Any,
        actor: str,
        scopes: frozenset[str] = frozenset(),
        task_id: int | None = None,
        router: Any = None,
    ) -> None:
        self.gateway = gateway
        self.model = model
        self.actor = actor
        self.scopes = scopes
        self.task_id = task_id
        self.router = router

    def __repr__(self) -> str:
        return f"<RunContext actor={self.actor} scopes={sorted(self.scopes)}>"


def get_context(config: dict[str, Any] | None) -> RunContext:
    """从 LangGraph 的 ``RunnableConfig`` 里取出 ``RunContext``。

    取不到就报一个**说人话**的错误。默认的 ``KeyError: 'ctx'``
    对调用方毫无帮助。
    """
    configurable = (config or {}).get("configurable") or {}
    ctx = configurable.get("ctx")
    if ctx is None:
        raise RuntimeError(
            "调用图时没有传 RunContext。正确写法：\n"
            "    graph.invoke(state, config={'configurable': {'thread_id': task_no, 'ctx': run_ctx}})"
        )
    return ctx
