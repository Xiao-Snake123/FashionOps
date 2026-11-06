"""对话域 —— 会话、消息、对话内审批，以及对话 Agent 本身。

对外只需要这两个入口（用法不变）：

    from fashionops.chat import agent, store
    agent.stream_turn(conversation_no="CH...", principal=p, user_text="...")
"""
from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 只给类型检查和 IDE 补全用，运行时不执行
    from fashionops.chat import agent, store

__all__ = ["agent", "store"]


def __getattr__(name: str) -> Any:
    """按需导入子模块（PEP 562）。

    ## 为什么这里**不能**在顶层写 `from fashionops.chat import agent, store`

    因为那会造出一个**取决于导入顺序**的循环：

        chat.__init__  →  chat.agent  →  fashionops.orchestration
                                              ↓
                                        chat.store（导入它又要先跑 chat.__init__）

    平台正常启动时是先导入 `chat` 的，这条环侥幸被绕过去，看起来一切正常。
    但只要**有人先导入 `orchestration`**（脚本、测试、探针都会），就会炸在

        ImportError: cannot import name 'SENTINEL' from partially
        initialized module 'fashionops.orchestration'

    这种"看谁先被导入才决定炸不炸"的问题最难查，所以直接把环拆掉：
    入口改成按需加载，`chat` 的导入不再牵连 `agent`。

    用法完全不变 —— `from fashionops.chat import agent, store` 依旧可用，
    因为 `from package import submodule` 本来就会在属性缺失时去导入子模块。
    """
    if name in __all__:
        # 子模块导入后会自动挂到本包上，所以只有第一次会走到这里。
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
