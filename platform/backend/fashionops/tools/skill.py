"""技能加载工具 —— 把 ``fashionops/skills/content/*.md`` 读给模型。

## 为什么它是一个**工具**，而不是塞进提示词

对话链的意图在提问之前是未知的：用户可能问库存，也可能要一份报表，
还可能只是闲聊。把四份技能正文全拼进系统提示，等于**每个回合都付**
这几百个 token，而绝大多数回合一份都用不上。

做成工具之后变成按需：系统提示里放一份索引（几十个 token），
模型自己判断"这次要按规范办事"时才花一次调用把正文拉进来。
这就是技能体系里说的渐进式披露。

**注意任务链不用这个工具**：那边每一步该用哪个技能由 `todo_kind` 决定
（见 `agents/loop.py` 的 `_guide_for`），题面已知，直接注入正文。
同一条链上，是代码知道的事就不该再去问模型一次。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from fashionops.gateway.registry import ToolRegistry


class LoadSkillArgs(BaseModel):
    name: str = Field(description="技能名，**必须来自系统提示里的技能索引**，例如 sku-selection。不要猜名字")


def register(registry: ToolRegistry) -> None:
    """把技能加载工具注册进给定注册表。"""

    @registry.tool(
        name="skill.load",
        description=(
            "读取一个业务技能的完整操作规范。系统提示里列出了可用技能索引；"
            "**要按标准流程做业务动作之前先读它**，不要凭印象操作。"
            "返回里带这个技能会用到的工具名。"
        ),
        args_model=LoadSkillArgs,
        # 显式空集：技能文档里只有业务规范，**不含任何数据、不触碰任何资源**，
        # 所以读到它不需要任何权限。
        #
        # 但要说清楚：**读得到规范 ≠ 做得了那件事**。能不能做由具体工具自己的
        # 作用域决定（`gateway/registry.py` 的 `Scope`），那条判断在网关里，
        # 且只在网关里。技能里写了 `mail.send` 不代表读它的 Agent 就能发信。
        scopes=(),
        read_only=True,
        timeout_s=2.0,
        tags=("skill",),
    )
    def load_skill(name: str) -> dict[str, Any]:
        from fashionops.skills import get_skill, skill_names

        skill = get_skill(name)
        if skill is None:
            # 和 `kb.search` 的没命中一样：这是**正常结果**，不是异常。
            # 返回结构化原因 + hint，让模型下一步能自己纠正，而不是抛异常。
            return {
                "found": False,
                "reason": "skill_not_found",
                "message": f"没有名为 {name!r} 的技能",
                "hint": (f"可用技能：{'、'.join(skill_names())}。请从系统提示的技能索引里选一个，**不要猜技能名**。"),
            }

        return {
            "found": True,
            "name": skill.name,
            "title": skill.title,
            "content": skill.body,
            "tools": list(skill.tools),
        }


__all__ = ["LoadSkillArgs", "register"]
