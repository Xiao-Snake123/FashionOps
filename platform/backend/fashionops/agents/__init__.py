"""Agent 层。

## 现在的执行架构（见 docs/12-AI协作交接.md）

**一个 Agent + 显式 todo 列表**：

```
agents/workplan.py   代码出计划（纯函数，按关键词判）
  → agents/loop.py   逐步执行：同一个 Agent，每步按 kind 裁工具目录
  → agents/recorder.py   旁路记账（事件流 + agent_runs，失败不影响业务）
agents/execution.py  提交 / 去重 / 续跑（HTTP 与对话共用）
```

## 留在这里的纯逻辑模块

* ``scoring`` / ``analytics_rules`` —— **算数和判定**，可复现、可审计。
  这两个模块是"表达交给模型、算数交给代码"那条线的落点，改之前先读它们的头部注释。
* ``business`` 工具在 ``fashionops.tools.business``（它们是网关工具，不放这里）

## 已经删掉的（别找回来）

``planner`` / ``orchestrator`` / ``runner`` / ``checkpoint`` / ``state`` ——
它们是"模型出 DAG → hub 路由 → 专用节点"那套架构的机器。
换成线性 todo 之后，图校验、环检测、传递阻断、checkpoint 恢复全部不再需要；
理由和证据写在交接文档 §1 与 §5。**词表和兼容逻辑没有丢**：
前者搬进了 ``workplan.py``，后者搬进了 ``recorder.normalize_dag``。
"""

from __future__ import annotations

from fashionops.agents.scoring import (
    HardConstraints,
    Rejection,
    ScoredCandidate,
    ScoreWeights,
    SelectionOutcome,
    apply_constraints,
    rank_candidates,
    score_candidates,
)
from fashionops.agents.workplan import TodoSpec, matched_kinds, plan_as_text, plan_todos

__all__ = [
    "HardConstraints",
    "Rejection",
    "ScoreWeights",
    "ScoredCandidate",
    "SelectionOutcome",
    "TodoSpec",
    "apply_constraints",
    "matched_kinds",
    "plan_as_text",
    "plan_todos",
    "rank_candidates",
    "score_candidates",
]
