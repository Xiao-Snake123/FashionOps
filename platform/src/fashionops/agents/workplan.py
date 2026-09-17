"""代码版的规划器 —— 按关键词把一句话需求拆成**线性** todo 列表。

替代原来 `planner.py` 的「模型出 DAG → 校验 → 自修复重试 → 兜底」循环
（见 docs/12-AI协作交接.md §1 问题 2 和 §5.2）。为什么不再让模型规划：

1. **每个任务固定多花约 20 秒 / 2000 tokens** —— 哪怕用户只是问个库存。
2. **它会自己加用户没要的步骤**。实测：用户说"挑 5 款适合上新的秋季连衣裙"，
   一个字没提邮件，模型规划的下一步却是 `notify`（发邮件），
   于是只读问题跑一步就停下来问"要不要发邮件" —— 用户那句
   "这个编排有什么用"就是这么来的。
3. 真实需求就是"选品 → 出报表 → 发邮件"一条线，DAG 的分支/汇合
   **表达力用不上**，但为它付出的代价（图校验、环检测、传递阻断、
   自修复重试）一步不少。

所以这里是一个**纯函数**：判据全部在代码里，可审计、可回归、可解释。
认不出来的组合就退化成单步任务，而不是编一步出来 ——
"我不知道"比"我编一个"便宜得多。

## 判据（交接文档 D4）

命中 **≥2 类**意图（selection / analytics / notification）才规划成多步；
命中 **0~1 类** 直接单步执行，不付规划的代价。

## 顺序（交接文档 D5）

todo 是**线性有序列表**，固定成业务上正确的顺序
``selection → analytics → notification``，**不按关键词在句子里出现的先后**。
"发给老板再选款"和"选款再发给老板"拆出来必须是同一个计划。

## 词表的来历

`_KEYWORDS` 从被删掉的 `planner.py` 原样搬来 —— 那是按"运营真的会怎么说话"
校准过的词表，别新写一份。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 关键词 → 工作类别。用的是"运营真的会怎么说话"，不是技术术语。
#:
#: **必须用正则而不是纯子串**：运营说"帮我选 3 款女装"时中间夹了数字和空格，
#: 纯子串匹配 ``"选款"`` 命中不了 —— 结果就是选品那步被整个丢掉，
#: 而用户明明要的就是选品。这条是被真实输入逼出来的，别"优化"成 ``in`` 判断。
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "selection": (r"选\s*\d*\s*款", "选品", "挑款", "挑选", "上新", "款式"),
    "analytics": ("报表", "分析", "数据", "日报", "周报", "月报", "经营", "指标"),
    "notification": ("发给", "邮件", "通知", "告知", "汇报"),
}

def _matches_request(pattern: str, request: str) -> bool:
    return re.search(pattern, request) is not None


#: 三类工作，**业务上的正确顺序**（先有数据，再分析，最后才对外发通知）。
#: 列表顺序就是 todo 的 seq 顺序。
TODO_KINDS: tuple[str, ...] = ("selection", "analytics", "notification")

#: 每类工作默认的目标描述。一句人话，**既给用户看也喂给模型**，
#: 所以不能写内部名（见 `TaskTodo.goal` 的字段注释）。
_KIND_GOALS: dict[str, str] = {
    "selection": "筛选出符合需求的候选款式",
    "analytics": "汇总经营数据并产出分析报表",
    "notification": "把结论与报表通知给相关人",
}


@dataclass(frozen=True)
class TodoSpec:
    """规划出来的一步。落库时对应 `task_todos` 的一行。"""

    seq: int
    goal: str
    kind: str
    #: 发邮件是不可撤销的对外动作 → 必须过人工闸口。
    #: 按代码规则判，不靠模型自觉（它标漏过，见 §1 问题 2）。
    approval_required: bool


def matched_kinds(user_request: str) -> list[str]:
    """按关键词判这句话命中了哪几类工作，**按业务顺序**返回。

    词表来自 `planner.py` 的 `_KEYWORDS` —— 那是按"运营真的会怎么说话"
    校准过的：``选\\s*\\d*\\s*款`` 用正则是因为运营说"帮我选 3 款女装"
    时中间夹了数字和空格，纯子串 ``"选款"`` 命中不了，
    结果就是选品那步被丢掉 —— 而用户明明要的就是选品。
    """
    return [
        kind
        for kind in TODO_KINDS
        if any(_matches_request(p, user_request) for p in _KEYWORDS[kind])
    ]


def plan_todos(user_request: str) -> list[TodoSpec]:
    """把一句话需求拆成 todo 列表。**纯函数，不调模型。**

    * 命中 0~1 类 → **单步**：不规划，让 Agent 直接做。
      单步的 goal 用用户原话 —— 这一步"要达成什么"就是他说的那句话，
      没有比原文更忠实的表述。
    * 命中 ≥2 类 → 按固定业务顺序拆成多步，每步一个 kind。
    """
    request = (user_request or "").strip()
    kinds = matched_kinds(request)

    if len(kinds) <= 1:
        kind = kinds[0] if kinds else "generic"
        return [
            TodoSpec(
                seq=1,
                goal=request,
                kind=kind,
                # 单步 notification（用户只要发一封邮件）同样不可撤销
                approval_required=(kind == "notification"),
            )
        ]

    return [
        TodoSpec(
            seq=i + 1,
            goal=_KIND_GOALS[kind],
            kind=kind,
            approval_required=(kind == "notification"),
        )
        for i, kind in enumerate(kinds)
    ]


def plan_as_text(todos: list[TodoSpec]) -> str:
    """把 todo 列表渲染成一行文字，给日志和界面用。

    带 ``*`` 的步骤要过人工闸口 —— 和旧 `planner.plan_as_text` 的约定一致，
    看过旧界面的人不用重新学一套记法。
    """
    if not todos:
        return "（无计划）"
    parts = [f"{t.seq}.{t.kind}{'*' if t.approval_required else ''}" for t in todos]
    return " → ".join(parts)


__all__ = [
    "TODO_KINDS",
    "TodoSpec",
    "matched_kinds",
    "plan_as_text",
    "plan_todos",
]
