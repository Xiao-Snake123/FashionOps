"""技能加载器 —— 发现 ``content/*.md``、解析 frontmatter、渲染占位符。

## 技能是什么

**把业务 SOP 从提示词字符串里搬出来，放进可版本控制、可 diff、可被模型读到的文档。**

在这之前，这些规范散在四处：`agents/loop.py` 的 `_KIND_GUIDE`（Python dict）、
`chat/agent.py` 的 `SYSTEM_PROMPT` 字符串、`analytics_rules.py` 的常量、
以及 docs 里。模型**只看得见其中一部分** —— 所以它知道"要引用具体数字"，
却不知道"20 单以下不下结论"，只能复述告警、解释不了为什么。

## 两条链的用法不一样，是刻意的

| | 注入什么 | 为什么 |
| --- | --- | --- |
| **任务链** | 该 todo 对应技能的**正文** | 题面已知（`kind` 由代码判定），没有"要不要读"这一步 |
| **对话链** | 只有**索引** + 一个 `skill.load` 工具 | 意图未知。正文全塞进提示词是每轮都付的固定开销，多数回合用不上 |

对话链那条是真正的**渐进式披露**：索引约几十个 token，
模型判断需要时才花一次工具调用把正文拉进来。

## frontmatter 为什么不用 PyYAML

只需要 ``key: value`` 和 ``key:`` + ``- item`` 两种形状。为这点语法引一个
YAML 解析器（顺带承担它的锚点/多行/类型推断等全部行为）不划算 ——
真出问题时，"我们的 frontmatter 只支持这两种写法"是个更好排查的约束。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from fashionops.skills.facts import facts

#: 技能文档目录。**放在包内**而不是项目的 data 目录：
#: 技能是代码的一部分（和权限码同理），要跟代码一起进版本控制与 code review。
CONTENT_DIR = Path(__file__).resolve().parent / "content"

#: 占位符：``{{ min_stock }}``，两边的空格可省。
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")

#: frontmatter 里必须有的键。
_REQUIRED_KEYS = ("name", "title", "summary")


class SkillError(Exception):
    """技能文档本身有问题。**一律抛错，不降级** ——

    技能被静默跳过时，Agent 会退回到"没有规范"的状态继续干活，
    而那个状态看起来完全正常。宁可启动就炸，也不要一个少了规范还照跑的 Agent。
    """


@dataclass(frozen=True)
class Skill:
    """一个技能。``body`` 是**已渲染**的正文（占位符已填成真实数字）。"""

    name: str
    title: str
    #: 一句话说明，进技能索引
    summary: str
    #: 对应任务链里的哪一步（`workplan.TODO_KINDS`）。None 表示只在对话里按需加载
    todo_kind: str | None
    #: 这个技能会用到的工具（**会在校验时和工具注册表对账**）
    tools: tuple[str, ...]
    body: str
    source: Path

    def __str__(self) -> str:  # 日志里好认
        return f"{self.name}（{self.title}）"


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def _parse_frontmatter(text: str, source: Path) -> tuple[dict[str, Any], str]:
    """拆出 frontmatter 和正文。

    只支持 ``key: value`` 与 ``key:`` + ``- item`` 两种写法（见模块 docstring）。
    遇到不认识的形状**直接报错并指出行号** —— 静默忽略一行会让技能缺一个字段，
    而缺字段的表现（比如少了 tools）要到很后面才看得出来。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillError(f"{source.name}：缺少 frontmatter，文件必须以 --- 开头")

    meta: dict[str, Any] = {}
    key: str | None = None
    end: int | None = None

    for index, raw in enumerate(lines[1:], start=2):  # 行号从 2 起（1 是 ---）
        line = raw.rstrip()
        if line.strip() == "---":
            end = index - 1
            break
        if not line.strip():
            continue

        stripped = line.lstrip()
        if stripped.startswith("- "):
            if key is None:
                raise SkillError(f"{source.name} 第 {index} 行：列表项前面没有键名")
            if not isinstance(meta[key], list):
                raise SkillError(f"{source.name} 第 {index} 行：{key} 不是列表")
            meta[key].append(stripped[2:].strip())
            continue

        if ":" not in line:
            raise SkillError(f"{source.name} 第 {index} 行不是 key: value —— {raw!r}")
        name, _, value = line.partition(":")
        key = name.strip()
        value = value.strip()
        # 空值先占成列表：后面跟 `- item` 就填它，否则最终会被当成缺字段
        meta[key] = value if value else []

    if end is None:
        raise SkillError(f"{source.name}：frontmatter 没有收尾的 ---")

    body = "\n".join(lines[end + 1 :]).strip()
    if not body:
        raise SkillError(f"{source.name}：正文是空的")
    return meta, body


def render(text: str, source: Path) -> str:
    """把 ``{{ fact }}`` 换成代码里的真实值。

    未知占位符**报错并列出现有全部事实名** —— 拼错一个名字却静默留空，
    表现是技能里少了一个数字，模型会自己去补一个，而补出来的数字没人能核对。
    """
    known = facts()

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in known:
            available = "、".join(sorted(known))
            raise SkillError(f"{source.name}：占位符 {{{{{name}}}}} 不存在。可用的事实：{available}")
        return known[name]

    return _PLACEHOLDER.sub(replace, text)


def _build(meta: dict[str, Any], body: str, source: Path) -> Skill:
    missing = [k for k in _REQUIRED_KEYS if not meta.get(k)]
    if missing:
        raise SkillError(f"{source.name}：frontmatter 缺 {'、'.join(missing)}")

    tools = meta.get("tools") or []
    if not isinstance(tools, list):
        raise SkillError(f"{source.name}：tools 必须是列表")

    todo_kind = meta.get("todo_kind") or None
    if todo_kind == "":
        todo_kind = None

    return Skill(
        name=str(meta["name"]).strip(),
        title=str(meta["title"]).strip(),
        summary=str(meta["summary"]).strip(),
        todo_kind=str(todo_kind).strip() if todo_kind else None,
        tools=tuple(str(t).strip() for t in tools),
        body=body,
        source=source,
    )


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------
def _load_uncached() -> tuple[Skill, ...]:
    if not CONTENT_DIR.is_dir():
        raise SkillError(f"技能目录不存在：{CONTENT_DIR}")

    skills: list[Skill] = []
    seen: dict[str, Path] = {}
    for path in sorted(CONTENT_DIR.glob("*.md")):
        raw_meta, body = _parse_frontmatter(path.read_text(encoding="utf-8"), path)
        skill = _build(raw_meta, render(body, path), path)

        if skill.name in seen:
            raise SkillError(f"技能名重复：{skill.name}（{seen[skill.name].name} 和 {path.name}）")
        seen[skill.name] = path
        skills.append(skill)

    if not skills:
        raise SkillError(f"技能目录是空的：{CONTENT_DIR}")
    return tuple(skills)


@lru_cache(maxsize=1)
def all_skills() -> tuple[Skill, ...]:
    """全部技能，按名字排序。进程内缓存。"""
    return tuple(sorted(_load_uncached(), key=lambda s: s.name))


def reload() -> tuple[Skill, ...]:
    """丢掉缓存重新加载。**测试和 CLI 用** —— 生产路径不需要它。"""
    all_skills.cache_clear()
    return all_skills()


def get_skill(name: str) -> Skill | None:
    wanted = (name or "").strip()
    for skill in all_skills():
        if skill.name == wanted:
            return skill
    return None


def skill_for_kind(kind: str) -> Skill | None:
    """任务链里某一步该用哪个技能。没配就返回 None（调用方退回内置短提示）。"""
    wanted = (kind or "").strip()
    for skill in all_skills():
        if skill.todo_kind == wanted:
            return skill
    return None


def skill_names() -> list[str]:
    return [s.name for s in all_skills()]


# ---------------------------------------------------------------------------
# 给提示词用的两个块
# ---------------------------------------------------------------------------
def index_block() -> str:
    """技能索引 —— 进对话链的系统提示。

    刻意**只给名字和一句话说明**，不给正文：正文要模型自己判断需要时
    用 ``skill.load`` 拉。这是渐进式披露的全部意义 ——
    索引的固定开销是几十个 token，而正文是几百个。
    """
    skills = all_skills()
    lines = [f"- `{s.name}`：{s.summary}" for s in skills]
    return (
        "## 可用技能\n"
        "下面是本系统的业务操作规范。**需要按标准流程做事之前，"
        "先用 `skill.load` 读出对应技能的完整内容**，不要凭印象操作：\n" + "\n".join(lines)
    )


def validate_skill_tools(registry: Any) -> list[str]:
    """拿技能里声明的工具和**工具注册表**对账，返回问题列表。

    这个检查值得单独存在：技能里写了一个不存在的工具名，
    模型会照着去调，然后拿到 ``TOOL_NOT_FOUND`` —— 而报错点在运行时、
    离原因（md 里打错一个字）很远。

    单独一个函数而不是塞进 `load_skills`：加载技能不该依赖"工具注册表已经
    装好"这个前提（CLI 渲染技能时不需要注册表）。调用方决定什么时候对账。
    """
    problems: list[str] = []
    for skill in all_skills():
        for tool in skill.tools:
            if tool not in registry:
                problems.append(f"技能 {skill.name} 声明了不存在的工具：{tool}")
    return problems


__all__ = [
    "CONTENT_DIR",
    "Skill",
    "SkillError",
    "all_skills",
    "get_skill",
    "index_block",
    "reload",
    "render",
    "skill_for_kind",
    "skill_names",
    "validate_skill_tools",
]
