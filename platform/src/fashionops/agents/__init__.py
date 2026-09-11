"""Agent 层：状态定义、checkpointer、以及后续的 9 个业务 Agent。

阶段 1（W2）先交付**编排内核**：

* ``state``      —— 跨节点流转的状态定义 + 预算控制 + 依赖上下文
* ``checkpoint`` —— 任务状态持久化，支撑"中断可恢复"与 HITL

9 个业务 Agent 在 W3/W4 落地，每个都只负责自己的决策逻辑，
外部调用统一走 ``fashionops.gateway``。

关键约定（写在最前面，因为很容易做错）：

1. **状态里只放可序列化的数据**。网关、模型客户端、数据库会话走
   ``RunContext``，通过 ``config['configurable']['ctx']`` 传递。
2. **节点不做治理**。限流、熔断、重试、审计全部在工具网关里，
   Agent 只管"要调什么、拿到结果后怎么决策"。
3. **预算在状态里累计**。没有预算约束的 Agent 会一直重试到把配额烧光。
"""

from __future__ import annotations

from fashionops.agents.checkpoint import (
    get_checkpointer,
    history,
    latest_state,
    pending_nodes,
    reset_checkpointer_cache,
    thread_config,
)
from fashionops.agents.state import (
    STATE_DEFAULTS,
    ApprovalRequest,
    Artifact,
    BudgetExceeded,
    RunContext,
    StepRecord,
    TaskState,
    budget_status,
    check_budget,
    get_context,
    new_state,
)

__all__ = [
    "STATE_DEFAULTS",
    "ApprovalRequest",
    "Artifact",
    "BudgetExceeded",
    "RunContext",
    "StepRecord",
    "TaskState",
    "budget_status",
    "check_budget",
    "get_checkpointer",
    "get_context",
    "history",
    "latest_state",
    "new_state",
    "pending_nodes",
    "reset_checkpointer_cache",
    "thread_config",
]
