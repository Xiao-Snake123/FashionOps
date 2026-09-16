"""对话 Agent 的工具目录与审批策略。

## 两件事分开

1. **目录**：这次对话里模型能看到哪些工具（受调用者权限约束）—— 在这里
2. **策略**：哪些工具在真正执行前必须过人工闸口 —— 抽到了
   `fashionops.approval.policy`（交接文档 T1），任务侧复用同一份。
   策略的判据（"做完能不能撤销"而不是"是不是写操作"）、
   默认从严的理由，全部写在那个模块的头部注释里，这里不重复。

下面保留 re-export 是为了不逼着现有调用方改 import ——
但**不要再在这里定义** `needs_approval` / `describe_preview`，
全仓只允许一份实现。
"""

from __future__ import annotations

from typing import Any

from fashionops.approval.policy import (  # noqa: F401  # re-export，见上
    APPROVAL_EXEMPT,
    MAIL_SEND,
    describe_preview,
    needs_approval,
)
from fashionops.gateway.registry import registry

#: 把复杂需求交给编排引擎的工具名。
#:
#: **它不是网关工具**，由对话 Agent 自己实现。理由：
#: 网关的工具 handler 只接收参数，拿不到"是谁在调用"（`CallContext`
#: 不传给 handler）。而提交任务的审计必须记"谁交办的"——
#: 从参数里传用户名等于让调用方自证身份，正是这个项目一直在防的事。
TASK_SUBMIT = "task.submit"

TASK_SUBMIT_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": TASK_SUBMIT.replace(".", "__"),
        "description": (
            "把一句**复杂的、需要多步完成**的运营需求交给编排引擎，"
            "它会拆成 DAG（选品 / 经营分析 / 报表 / 发信等）异步执行，"
            "涉及对外发送的步骤会停下来等人确认。"
            "适用于「帮我选 3 款秋季女装并出一份报表发给运营」这类需求。"
            "**简单的一问一答不要用它** —— 那种情况直接用查询工具自己回答。"
            "调用后把任务号告诉用户，并说明可以去任务页看实时进度。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "description": "用户的原始需求，尽量完整保留用户的原话和约束条件",
                },
                "task_type": {
                    "type": "string",
                    "description": "任务类型标签，如 generic / daily_report / after_sale",
                    "default": "generic",
                },
            },
            "required": ["request"],
        },
    },
}


def build_catalog(
    *, granted_scopes: frozenset[str], permission_codes: frozenset[str]
) -> list[dict[str, Any]]:
    """组装这次对话里模型能看到的工具目录。

    ## 为什么在**目录层面**就过滤，而不是让模型看见再被拒

    模型看到工具就会去调，调了被拒就浪费一整轮 token，还容易陷入重试循环。
    所以在目录里就不给它看见没权限的工具。

    这也是"用户权限 ∩ Agent 权限集"那条原则在对话场景的落点：
    ``granted_scopes`` 是用户权限和客服 Agent 权限集的**交集**，
    用户自己都没有的权限，对话里的 Agent 也不该有。
    """
    from fashionops.tools import register_all_tools

    if not len(registry):
        register_all_tools(registry)

    catalog = registry.openai_schemas(granted_scopes)

    # 提交任务需要的是身份域的 `task:submit` 权限，不是网关的网络作用域，
    # 所以单独判一次 —— 两个体系的权限码不是一回事，混着查会串
    if "task:submit" in permission_codes:
        catalog.append(TASK_SUBMIT_SCHEMA)

    return catalog


__all__ = [
    "APPROVAL_EXEMPT",
    "MAIL_SEND",
    "TASK_SUBMIT",
    "TASK_SUBMIT_SCHEMA",
    "build_catalog",
    "describe_preview",
    "needs_approval",
]
