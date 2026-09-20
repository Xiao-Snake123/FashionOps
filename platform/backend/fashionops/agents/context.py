"""执行上下文 —— 一次运行需要的依赖集合。

从被删掉的 `state.py` 里搬出来（那里除了它还有一整套 LangGraph 状态定义：
``TaskState`` / ``StepRecord`` / reducer 约定 / ``get_context``，
那些随图一起没了）。

## 为什么它还在

有两个**和任务执行无关**的调用方要它：

* ``fashionops.service.agent``（客服 Agent）—— 它不是任务、不需要
  断点续跑，但同样要"拿网关、拿模型、带上调用者身份"这三件事
* 测试 —— 换一个 ``RunContext`` 就能把网关和模型全换成假的

## 为什么它**不是**任务执行路径的东西

新架构的任务执行（`agents/loop.py`）**不用它**：那里面网关和模型客户端的
生命周期是"一个任务一份"，明确地在函数里建、在函数里用完，
不经过任何"上下文对象"传递。少一层间接，读代码的人少跳一次。

所以别为了让签名好看而给 loop 也套上 RunContext ——
那会把"依赖从哪来"这件事重新藏起来，而这正是删掉旧架构时想解决的一类问题。
"""

from __future__ import annotations

from typing import Any


class RunContext:
    """一次运行需要的依赖集合。"""

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


__all__ = ["RunContext"]
