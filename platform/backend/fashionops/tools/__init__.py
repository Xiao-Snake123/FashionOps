"""业务工具：挂在工具网关上、供 Agent 调用的能力。

## 为什么工具要收口到网关

Agent 直接 ``session.query(...)`` 也能跑，但那样就绕过了限流、熔断、审计、
权限。工具是**外部世界的边界**，边界上必须有治理。

## 当前都是 Mock 数据源

数据来自本机 MySQL 里的假数据（``fashionops-seed`` 灌的）。
真实接入时只要换 handler 实现，工具签名（也就是 Agent 看到的那份契约）不变。
这就是"外部依赖可插拔"的落点。

## 工具签名设计原则

**参数的粒度要匹配 Agent 的思考粒度**。给 Agent 一个
``query_candidates(category, season, limit)`` 而不是
``query_products`` + ``query_inventory`` + ``query_competitors`` 三步自己去拼 ——
后者会让 Agent 把 token 花在"怎么把三张表拼起来"这种本该由后端解决的事情上。

一次调用给一个**决策所需的最小完整数据集**，是工具设计的核心。
"""

from __future__ import annotations

import logging

from fashionops.gateway.registry import Scope, ToolRegistry

logger = logging.getLogger(__name__)

#: 各 Agent 的默认权限集。写在一处，方便对照和审查 ——
#: 权限散在各处时，很容易出现"通知 Agent 居然能改广告预算"这种越权。
AGENT_SCOPES: dict[str, frozenset[str]] = {
    # 注意：这里**没有** orchestrator 了。新架构里任务侧只有一个 Agent，
    # 它的权限集是下面的 "task"。旧代码里"按 orchestrator 权限集求交"的
    # 写法如果留着，会得到一个不含 mail:send 的集合 ——
    # 表现为通知步骤的工具目录是空的，模型压根看不见发信工具。
    "selection": frozenset({Scope.ERP_READ, Scope.ECOM_READ}),
    "analytics": frozenset({Scope.ERP_READ, Scope.ECOM_READ, Scope.ASSET_WRITE}),
    "notification": frozenset({Scope.MAIL_SEND, Scope.ASSET_WRITE}),
    "listing": frozenset({Scope.ERP_READ, Scope.ECOM_READ, Scope.ECOM_WRITE}),
    "ads": frozenset({Scope.ERP_READ, Scope.AD_READ, Scope.AD_WRITE}),
    "service": frozenset({Scope.ERP_READ, Scope.KB_READ, Scope.MAIL_SEND}),
    "creative": frozenset({Scope.ECOM_READ, Scope.ASSET_WRITE}),
    "video": frozenset({Scope.ECOM_READ, Scope.ASSET_WRITE}),
    # 对话 Agent。它是个"什么都能问一点"的角色，所以读侧权限给全；
    # 但 `mail:send` 也在这里声明 —— 因为对话里确实要能起草并发送邮件。
    # 发信不会因此变得危险：**对话层会在执行前拦住并要求人工确认**
    # （见 `fashionops.approval.policy.needs_approval`）。权限回答"有没有资格"，
    # 审批回答"这一次人同意了吗"，两者都要有。
    "chat": frozenset(
        {
            Scope.ERP_READ,
            Scope.ECOM_READ,
            Scope.AD_READ,
            Scope.KB_READ,
            Scope.ASSET_WRITE,
            Scope.MAIL_SEND,
        }
    ),
    # 任务 Agent（新架构里**唯一**的执行者，见 docs/12-AI协作交接.md T3/T4）。
    # 它要覆盖三类 todo 的工具需求：选品（ERP 读 + 打分）、分析（ERP 读 +
    # 出报表）、通知（发信）。真正执行某一步时，工具目录还会按当前 todo
    # 的 kind 再裁剪一次 —— 这里给的是它**可能**需要的全集。
    "task": frozenset(
        {
            Scope.ERP_READ,
            Scope.ECOM_READ,
            Scope.KB_READ,
            Scope.ASSET_WRITE,
            Scope.MAIL_SEND,
        }
    ),
}


def register_all_tools(registry: ToolRegistry | None = None) -> ToolRegistry:
    """把所有业务工具注册进注册表，返回注册表本身。

    传 ``None`` 就注册到进程内的默认注册表（生产路径）；
    测试里传一个新建的，避免和别的测试互相污染。
    """
    from fashionops.gateway.registry import registry as default_registry

    # 注意：`tools.skill`（提供 `skill.load` 工具）和 `fashionops.skills`
    # （技能文档包）是两个东西。前者是工具，后者是文档 —— 别混。
    from fashionops.tools import business, erp, kb, mail, report, skill

    target = registry if registry is not None else default_registry
    # 技能加载排在最前：它不依赖任何其他工具，也不碰数据库 ——
    # 只是把一份 markdown 读出来。放前面是为了让下面那些有依赖的模块
    # 保持原来"基础工具先注册、业务工具最后"的阅读顺序。
    skill.register(target)
    erp.register(target)
    report.register(target)
    mail.register(target)
    kb.register(target)
    # 业务工具（原专用 Agent 的代码部分）：依赖上面先注册好的基础工具模块，
    # 所以放最后。顺序在这里是有意义的，别随手挪。
    business.register(target)
    # 外部 MCP 服务器（office 等）：默认关闭，开了才接入。放在最后且不抛异常 ——
    # 接入失败（比如没配 mcp-servers 路径）不能拖垮整个平台启动。
    try:
        from fashionops.integrations import office

        n = office.register_office_tools(target)
        if n:
            logger.info("接入了 %d 个外部 MCP 工具", n)
    except Exception as exc:  # noqa: BLE001
        logger.warning("办公 MCP 接入跳过：%s", exc)
    return target


__all__ = ["AGENT_SCOPES", "register_all_tools"]
