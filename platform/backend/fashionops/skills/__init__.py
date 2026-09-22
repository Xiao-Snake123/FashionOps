"""技能（SKILL.md）—— **把业务 SOP 从提示词里搬进可版本控制的文档**。

## 它解决的是什么问题

在这之前，业务规范散在四个地方：

| 位置 | 装了什么 | 模型看得见吗 |
| --- | --- | --- |
| `agents/loop.py` 的 `_KIND_GUIDE` | 每一步怎么做 | ✅ 看得见 |
| `chat/agent.py` 的 `SYSTEM_PROMPT` | 通用铁律 | ✅ 看得见 |
| `agents/analytics_rules.py` 的常量 | 退货率红线、样本下限… | ❌ **看不见** |
| `agents/scoring.py` 的默认值 | 库存下限、毛利率下限、五维权重 | ❌ **看不见** |

模型看不见的那两行是关键：它知道"要引用具体数字"，
却不知道"20 单以下不下结论"。所以它能**复述**告警，但解释不了为什么 ——
而运营真正会问的恰恰是"为什么这个类目你不管"。

## 三个决定

**1. 文档在代码里，不在数据库里。**
和权限码同理：技能和工具、和阈值常量的对应关系是代码结构的一部分，
改代码就要一起改。放数据库里，代码改了库没改**没有任何机制能发现**。
（角色授权放数据库，因为那是业务决策；技能放代码，因为它是实现规范。）

**2. 数字用占位符，值从代码现取。** 见 `facts.py`。
技能文档里写 ``{{ min_stock }}`` 而不是 ``30`` ——
否则改 `scoring.py` 的默认值之后，md 里那个 30 会静静地变成假的，
而模型会照着假的讲给运营听，讲得有根有据。

**3. 两条链的用法不同，是刻意的。**

- **任务链**（`agents/loop.py`）：某一步对应哪个技能由 `todo_kind` 决定，
  **代码直接注入正文**。题面已知，没有"要不要读"这个决策，多一次工具调用纯属浪费。
- **对话链**（`chat/agent.py`）：意图未知，只给**索引**，正文由模型
  自己判断要不要用 `skill.load` 拉。这是渐进式披露 ——
  索引几十个 token，四份正文几百个；而大多数对话回合一个技能都用不上。

## 边界：技能不是权限

技能说"该怎么做"，不决定"能不能做"。能做不能做由工具网关的作用域决定
（见 `gateway/registry.py` 的 `Scope`）。一个技能里写了 `mail.send`
不代表读了这个技能的 Agent 就能发信 —— 那条判断在网关里，且只在网关里。

技能里声明 `tools` 的作用是**对账**：`validate_skill_tools()` 会拿它和工具注册表
比对，防止文档里写了一个不存在的工具名，让模型照着调、拿到 `TOOL_NOT_FOUND`，
而报错点离原因（md 里打错一个字）很远。
"""
from __future__ import annotations

from fashionops.skills.facts import describe, facts
from fashionops.skills.loader import (
    CONTENT_DIR,
    Skill,
    SkillError,
    all_skills,
    get_skill,
    index_block,
    reload,
    render,
    skill_for_kind,
    skill_names,
    validate_skill_tools,
)

__all__ = [
    "CONTENT_DIR",
    "Skill",
    "SkillError",
    "all_skills",
    "describe",
    "facts",
    "get_skill",
    "index_block",
    "reload",
    "render",
    "skill_for_kind",
    "skill_names",
    "validate_skill_tools",
]
