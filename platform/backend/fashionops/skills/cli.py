"""技能命令行。

用法::

    fashionops-skills list              # 有哪些技能、分别给哪条链用
    fashionops-skills render <name>     # 打印**渲染后**的正文（占位符已填成真实数字）
    fashionops-skills facts             # 可引用的事实及当前值（值从代码现取）
    fashionops-skills check             # 技能里声明的工具是否真实存在

## 为什么这个 CLI 不做日志初始化、不读配置

技能是**纯文档**：不碰数据库、不碰向量库、不碰模型。所以这里刻意不 import
`config` / `logging_setup` —— 配置写错了、外部依赖挂了，也应该能读技能文档。
排查环境问题时，"这条命令还能不能用"本身就是一条有用的分界线。
"""

from __future__ import annotations

import sys
from argparse import ArgumentParser, Namespace

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def cmd_list(_args: Namespace) -> int:
    from fashionops.skills import all_skills

    skills = all_skills()
    print(f"共 {len(skills)} 个技能\n")
    for skill in skills:
        used_by = f"任务链 step={skill.todo_kind}" if skill.todo_kind else "仅对话链按需加载"
        print(f"[{skill.name}] {skill.title}")
        print(f"    用途  : {skill.summary}")
        print(f"    场景  : {used_by}")
        print(f"    工具  : {'、'.join(skill.tools) or '（无）'}")
        print(f"    正文  : {len(skill.body)} 字")
        print(f"    文件  : {skill.source.name}")
        print()
    return 0


def cmd_render(args: Namespace) -> int:
    from fashionops.skills import get_skill

    skill = get_skill(args.name)
    if skill is None:
        from fashionops.skills import skill_names

        print(f"没有名为 {args.name!r} 的技能。可用：{'、'.join(skill_names())}")
        return 1

    print(f"# {skill.title}（{skill.name}）")
    print(f"# 用途：{skill.summary}")
    print(f"# 工具：{'、'.join(skill.tools) or '（无）'}")
    print("-" * 70)
    print(skill.body)
    return 0


def cmd_facts(_args: Namespace) -> int:
    """列出技能文档里能引用的事实。

    每一项的值都**从代码现取**（改常量这里立刻跟着变）。
    想看某个事实是谁提供的：去 `skills/facts.py` 里找它对应的 import。
    """
    from fashionops.skills import describe

    items = describe()
    print(f"共 {len(items)} 项可引用事实（值来自代码，不手写）\n")
    width = max(len(name) for name, _ in items)
    for name, value in items:
        print(f"  {{{{{name:<{width}}}}}}  →  {value}")
    return 0


def cmd_check(_args: Namespace) -> int:
    """校验技能文档和工具注册表是否一致。

    这个检查的价值在于：技能里写错一个工具名，模型会照着去调，
    然后拿到 `TOOL_NOT_FOUND` —— 报错点在运行时，离原因（md 里打错一个字）很远。
    """
    from fashionops.gateway.registry import registry
    from fashionops.skills import all_skills, validate_skill_tools
    from fashionops.tools import register_all_tools

    if not len(registry):
        register_all_tools(registry)

    problems = validate_skill_tools(registry)

    print(f"技能 {len(all_skills())} 个 · 注册表里的工具 {len(registry)} 个")
    if problems:
        print("\n发现问题：")
        for problem in problems:
            print(f"  ✗ {problem}")
        return 1

    print("✓ 技能里声明的工具都存在")
    return 0


_COMMANDS = {
    "list": cmd_list,
    "render": cmd_render,
    "facts": cmd_facts,
    "check": cmd_check,
}


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(
        prog="fashionops-skills",
        description="技能文档：列出、渲染、查看事实、校验工具声明的对账",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出全部技能")
    render = sub.add_parser("render", help="打印某个技能渲染后的正文")
    render.add_argument("name", help="技能名，见 list")
    sub.add_parser("facts", help="列出可引用的事实及当前值")
    sub.add_parser("check", help="校验技能里声明的工具是否真实存在")

    args = parser.parse_args(argv)
    return _COMMANDS[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["main"]
