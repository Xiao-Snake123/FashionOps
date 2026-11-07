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
from fashionops.chat import longterm
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


#: 让模型**主动**向用户提一个澄清问题。
#:
#: **它不是网关工具**，理由和 `task.submit` 一样：网关的 handler 拿不到"是谁在问"。
#: 更重要的是它**不执行任何外部动作** —— 它只是让工具循环停下来等人。
#:
#: ## 为什么不复用审批那张表
#:
#: 审批回答的是"这个写操作要不要做"，留痕必须记"谁批的、什么时候批的"；
#: 澄清回答的是"用户到底想要什么"，答案是**用户的原话**。
#: 塞进同一张表会让 `decided_by` 这类字段语义打架，而且审批台账上会混进
#: 一堆"回答问题"的记录 —— 运维看板再也数不清到底有几个真闸口卡着人。
#:
#: 所以澄清走**普通消息**：问题落成一条 assistant 消息，用户的回答就是
#: 下一条 user 消息。历史天然自洽，不需要 schema 变更，也不需要新卡片。
ASK_USER = "ask_user"

ASK_USER_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": ASK_USER,
        "description": (
            "在你**确实无法确定**、且猜错会导致做错事时，向用户提一个澄清问题。"
            "调用后本轮立刻结束、等用户回答，你会在下一轮看到答案。"
            "**不要**用它做安全确认（写操作由系统自动弹确认卡片，不需要你问）；"
            "**不要**问你能用工具查到、或能从上下文推断出来的事；"
            "一次只问最关键的**一个**问题。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "要问的问题。一句话直接问，不要寒暄、不要复述用户的话。",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "候选答案（最多 4 个）。有明确选项时才给 —— 用户能少打字。开放性问题不要传这个字段。"
                    ),
                },
                "assumption": {
                    "type": "string",
                    "description": (
                        "你不问的话打算按什么默认理解来做。写出来，用户就可以只回一句「就按你说的」而不用打完整答案。"
                    ),
                },
            },
            "required": ["question"],
        },
    },
}


#: 让模型把**长期有效**的用户偏好记下来，以后所有对话都记得。
#:
#: 和 `task.submit` / `ask_user` 一样**不是网关工具**，理由也一样：
#: 网关的 handler 拿不到 `CallContext`，而"记在谁名下"必须来自令牌。
#: 让调用方传 user_id 等于让他自证身份 —— 那正是这个项目一直在防的事。
REMEMBER = "remember"
FORGET = "forget"

REMEMBER_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": REMEMBER,
        "description": (
            "把用户的**长期偏好或约定**记下来，以后所有对话都会记得。\n"
            "**只在用户明确表达了一条会长期有效的偏好时用**，比如"
            "「以后报表都按周出」「我们只做抖音渠道」「退货率超过 8% 就预警」。\n"
            "**不要用它记**：闲聊、一次性的查询条件（「查一下 SKU00042」）、"
            "你自己推断出来的东西、以及用户没说过的任何事。\n"
            "拿不准就别记 —— 记错的会被之后每一轮带上，而漏记只是下次再问一遍。\n"
            "同一个 topic 再记一次是**更新**：用户改主意时用它，不要新开一个 topic。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": (
                        "这条记忆的标题，也是它的键。2~8 个字的短语，"
                        "如「渠道偏好」「报表口径」「预警阈值」。"
                        "**每次都要写成同样的字**，否则会被当成两条不同的记忆。"
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "要记住的那一句话，写清具体要求。",
                },
                "kind": {
                    "type": "string",
                    "enum": ["preference", "fact", "note"],
                    "description": (
                        "preference = 长期偏好/约定（不会过期，最常用）；"
                        "fact = 有时效的数字或结论（注入时会自动标上记录时间）；"
                        "note = 其它背景备注。默认 preference。"
                    ),
                },
            },
            "required": ["topic", "content"],
        },
    },
}

FORGET_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": FORGET,
        "description": (
            "删掉一条已记住的长期记忆。用户说「别记着这个了」「取消刚才那条限制」时用它。\n"
            "topic 必须和记住时的原文一致 —— 你能在 system 里那段"
            "「你记得的关于这位用户的事」中看到确切的 topic。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "要删除的记忆 topic（原文）"},
            },
            "required": ["topic"],
        },
    },
}


def build_catalog(*, granted_scopes: frozenset[str], permission_codes: frozenset[str]) -> list[dict[str, Any]]:
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

    # 追问**不看权限**：让用户把需求说清楚不触碰任何资源，
    # 反而少问一句才会做出错误的动作。所以它对所有能对话的人都开放。
    catalog.append(ASK_USER_SCHEMA)

    # 长期记忆同理：它只写"这个用户自己的偏好"，既不碰外部资源也不跨人。
    #
    # 功能关掉时**连工具都不给它看见** —— 让模型调一个必然失败的工具没有好处：
    # 白费一轮 token，而且模型会试图向用户解释这个它本来就不该知道的失败。
    if longterm.enabled():
        catalog.append(REMEMBER_SCHEMA)
        catalog.append(FORGET_SCHEMA)

    return catalog


__all__ = [
    "APPROVAL_EXEMPT",
    "ASK_USER",
    "ASK_USER_SCHEMA",
    "FORGET",
    "FORGET_SCHEMA",
    "MAIL_SEND",
    "REMEMBER",
    "REMEMBER_SCHEMA",
    "TASK_SUBMIT",
    "TASK_SUBMIT_SCHEMA",
    "build_catalog",
    "describe_preview",
    "needs_approval",
]
