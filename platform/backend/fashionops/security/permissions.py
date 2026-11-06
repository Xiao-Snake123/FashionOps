"""权限目录 —— **全系统权限码的单一事实源**。

## 为什么权限码写在代码里、角色写在数据库里

这是本模块最重要的一个决定，值得单独说明。

**权限码跟着代码走。** `task:approve` 这种码和某一行 `Depends(...)` 是一一对应的。
接口改了、权限码没改，就会出现"某个码谁都没有、但代码还在要它"的死码。
放进数据库的话，这种漂移**没有任何机制能发现** —— 只能靠人去比对，而没人会去比对。

**角色和"角色→权限"的映射是业务决策。** "运营主管能不能批邮件"这个问题
应该由业务方在界面上改，不该要求改代码 + 发版。所以它放数据库。

两者兼顾的做法：
- 权限码集合在本模块里硬编码（`PERMISSIONS`），由 `sync_permissions()` 幂等同步进表；
- 角色和授权关系在表里，可由 `fashionops-user` CLI 或界面维护；
- 内置角色的**默认授权**也在本模块里（`ROLE_PRESETS`），但只在角色**首次创建**时写入 ——
  之后改了以数据库为准。这样"升级代码时把管理员的自定义授权覆盖掉"不会发生。

## 为什么权限码和工具 Scope 共用一套词表

工具网关的 `Scope.ERP_READ` 是 `"erp:read"`，本模块的 `PERM_ERP_READ` 也是 `"erp:read"`。
**故意让它们相等**，因为它们是同一件事的两种问法：

- 网关问："这个 Agent 能不能调 `erp.query_*`？"
- API 问："这个用户能不能提交一个会调 `erp.query_*` 的任务？"

如果两套词表，就得维护一张映射表，而映射表迟早会和两边都不一致。
共用一套之后，用户的权限集合可以**直接和 Agent 的权限集合求交集**（见
`service.effective_scopes()`），"用户权限向下传导给 Agent"这件事就变成一行代码。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from fashionops.db.models_identity import Permission, Role, RolePermission

# ===========================================================================
# 1. 权限码
# ===========================================================================
# ---- 任务（我们的核心资源）----
PERM_TASK_READ: Final = "task:read"
PERM_TASK_SUBMIT: Final = "task:submit"
PERM_TASK_APPROVE: Final = "task:approve"
PERM_TASK_CANCEL: Final = "task:cancel"

#: 用对话式 Agent（建会话、发消息、确认闸口）。
#: 它跟 `task:*` 分开是有意的：对话能直接提问并拿到数据，
#: 和"提交一个任务"是两种不同的能力，将来可能只给一部分人开。
#: 但发信这类写动作仍然由 `mail:send` 和对话内的闸口双重拦住，
#: 所以给了 `chat:use` 不等于给了对外发声的能力。
PERM_CHAT_USE: Final = "chat:use"

#: 处理客服工单（跑两层客服流程）。
#: 它是一个**写操作** —— 会把判断写回工单（覆盖 intent/sentiment/escalated）。
#: 所以不能只给只读角色：`viewer` / `analyst` 看得到工单，但不能重跑它的判断。
PERM_TICKET_HANDLE: Final = "ticket:handle"

# ---- 业务数据（与工具网关的 Scope 同名同义）----
PERM_ERP_READ: Final = "erp:read"
PERM_ERP_WRITE: Final = "erp:write"
PERM_ECOM_READ: Final = "ecom:read"
PERM_ECOM_WRITE: Final = "ecom:write"
PERM_AD_READ: Final = "ad:read"
PERM_AD_WRITE: Final = "ad:write"
PERM_KB_READ: Final = "kb:read"

# ---- 产物与对外动作 ----
PERM_ASSET_WRITE: Final = "asset:write"
PERM_MAIL_SEND: Final = "mail:send"

#: 改邮件**策略本身**：白名单 / 黑名单 / 真发开关 / 收件人上限。
#:
#: 为什么必须和 `mail:send` 分开：能发信的人如果也能自己放宽白名单，
#: 那白名单就不是闸门而是装饰 —— 他随时能把 `*` 加进去再发。
#: "能发"和"能放宽发信规则"得是两拨人，否则策略形同虚设。
PERM_MAIL_ADMIN: Final = "mail:admin"

#: 外部办公能力（office MCP 服务器经网关接入）。
#: 字符串与网关的 `Scope.OFFICE_READ` / `Scope.OFFICE_WRITE` 同名同义 ——
#: 按本模块顶部「共用一套词表」的原则故意保持一致。
PERM_OFFICE_READ: Final = "office:read"
PERM_OFFICE_WRITE: Final = "office:write"

# ---- 管理面 ----
PERM_ADMIN_USER: Final = "admin:user"  # 增删用户、改状态、重置密码（**跨部门**）
PERM_ADMIN_ROLE: Final = "admin:role"  # 建角色、改授权
PERM_ADMIN_AUDIT: Final = "admin:audit"  # 看审计日志

#: **范围受限**的账号管理：只能管本部门的账号。
#:
#: 为什么和 `admin:user` 拆成两个码，而不是共用一个码再判断"他是不是主管"：
#: 权限码回答的是"有没有这个资格"，这个问题的答案应该只有一个来源。
#: 合用一个码的话，每个账号相关接口都要自己记得再查一次部门，
#: **漏掉一个就是一次跨部门越权**；而漏掉的接口在 review 时看起来完全正常。
#: 拆开之后，"只能管本部门"这件事由角色授权决定，接口只需要认码。
PERM_DEPT_USER: Final = "dept:user"


@dataclass(frozen=True)
class PermissionDef:
    """权限码的元信息。``category`` 用于界面分组展示。"""

    code: str
    name: str
    category: str
    description: str = ""


#: **唯一的权限清单**。加权限只改这里，`sync_permissions()` 会同步到库。
#: 顺序按类别排列，方便 review 时对照接口清单。
PERMISSIONS: Final[tuple[PermissionDef, ...]] = (
    PermissionDef(PERM_TASK_READ, "查看任务", "task", "查看任务列表、详情与事件流"),
    PermissionDef(PERM_TASK_SUBMIT, "提交任务", "task", "提交新的 Agent 任务"),
    PermissionDef(
        PERM_TASK_APPROVE,
        "审批任务",
        "task",
        "批准/驳回停在人工闸口的任务。**批准会真的触发对外动作（发邮件、上下架）**",
    ),
    PermissionDef(PERM_TASK_CANCEL, "取消任务", "task", "取消正在跑的任务"),
    PermissionDef(
        PERM_CHAT_USE,
        "使用对话助手",
        "task",
        "开对话、提问、确认对话里的写操作闸口",
    ),
    PermissionDef(
        PERM_TICKET_HANDLE,
        "处理客服工单",
        "task",
        "跑客服 Agent（会**覆盖**工单的意图/情绪/是否转人工判断）",
    ),
    PermissionDef(PERM_ERP_READ, "读取商品与订单", "data", "查询商品、库存、销量"),
    PermissionDef(PERM_ERP_WRITE, "写入商品与订单", "data", "改商品信息、库存"),
    PermissionDef(PERM_ECOM_READ, "读取平台数据", "data", "查询电商平台侧数据"),
    PermissionDef(PERM_ECOM_WRITE, "写入平台数据", "data", "在电商平台上架/改价"),
    PermissionDef(PERM_AD_READ, "读取广告数据", "data", "查询广告计划与消耗"),
    PermissionDef(PERM_AD_WRITE, "调整广告投放", "data", "改预算、暂停计划"),
    PermissionDef(PERM_KB_READ, "检索知识库", "data", "客服知识库语义检索"),
    PermissionDef(PERM_ASSET_WRITE, "生成产物", "asset", "导出报表、生成图片视频等产物"),
    PermissionDef(PERM_MAIL_SEND, "发送邮件", "mail", "**对外发信**。默认只有主管及以上有"),
    PermissionDef(
        PERM_MAIL_ADMIN,
        "配置邮件策略",
        "mail",
        "改白名单/黑名单、关掉 dry_run 转真发。**比发信更敏感**：放宽规则等于给所有人开对外通道",
    ),
    PermissionDef(
        PERM_OFFICE_READ,
        "使用办公能力（读）",
        "office",
        "经网关接入的办公 MCP 服务器里的只读动作（查邮件、看日程、读文档）",
    ),
    PermissionDef(
        PERM_OFFICE_WRITE,
        "使用办公能力（写）",
        "office",
        "**对外动作**：发邮件、建/改日程、写文件。默认只有主管及以上有",
    ),
    PermissionDef(
        PERM_ADMIN_USER,
        "管理全部账号",
        "admin",
        "**跨部门**新建/修改/停用账号、重置密码。不能操作超级管理员账号",
    ),
    PermissionDef(
        PERM_DEPT_USER,
        "管理本部门账号",
        "admin",
        "只对本部门账号生效：新建/修改/停用、重置密码。"
        "**不能跨部门，也不能授予自己没拥有的权限**",
    ),
    PermissionDef(PERM_ADMIN_ROLE, "管理角色", "admin", "创建角色、分配权限"),
    PermissionDef(PERM_ADMIN_AUDIT, "查看审计", "admin", "查看操作审计与登录记录"),
)

PERMISSION_CODES: Final[frozenset[str]] = frozenset(p.code for p in PERMISSIONS)
ALL_PERMISSIONS: Final[frozenset[str]] = PERMISSION_CODES


# ===========================================================================
# 2. 内置角色
# ===========================================================================
ROLE_ADMIN: Final = "admin"
ROLE_OPS_MANAGER: Final = "ops_manager"

#: 部门主管。和 `ops_manager`（运营主管）是两种不同的"主管"：
#: 前者管的是**人**（本部门账号），后者管的是**事**（审批业务动作）。
#: 合成一个角色会让"谁有权批邮件"和"谁有权建账号"绑死在一起 ——
#: 而这两件事在企业里通常不是同一批人定的。
ROLE_DEPT_MANAGER: Final = "dept_manager"

ROLE_OPERATOR: Final = "operator"
ROLE_ANALYST: Final = "analyst"
ROLE_VIEWER: Final = "viewer"

#: 只读权限集合（多个角色共用）
_READONLY: Final = frozenset(
    {PERM_TASK_READ, PERM_ERP_READ, PERM_ECOM_READ, PERM_AD_READ, PERM_KB_READ}
)


@dataclass(frozen=True)
class RoleDef:
    code: str
    name: str
    description: str
    permissions: frozenset[str]
    #: 系统内置角色不允许在界面上删除 —— 删了会让代码里的常量悬空
    is_system: bool = True


#: 内置角色及其**首次创建时**的默认授权。
#:
#: 注意 `operator` 刻意**没有** `task:approve` 和 `mail:send`：
#: "能发起"和"能批准"必须分开，否则人工闸口就退化成点一下就过的形式主义。
#: 这也是 RBAC 在这个系统里存在的**唯一实际理由**，不只是装饰。
ROLE_PRESETS: Final[tuple[RoleDef, ...]] = (
    RoleDef(
        ROLE_ADMIN,
        "系统管理员",
        "全部权限。用于初始化与运维",
        ALL_PERMISSIONS,
    ),
    RoleDef(
        ROLE_OPS_MANAGER,
        "运营主管",
        "能发起并**审批**任务，含对外发信",
        frozenset(
            _READONLY
            | {
                PERM_TASK_SUBMIT,
                PERM_TASK_APPROVE,
                PERM_TASK_CANCEL,
                PERM_CHAT_USE,
                PERM_TICKET_HANDLE,
                PERM_ASSET_WRITE,
                PERM_MAIL_SEND,
                PERM_OFFICE_READ,
                PERM_OFFICE_WRITE,
            }
        ),
    ),
    RoleDef(
        ROLE_DEPT_MANAGER,
        "部门主管",
        "管理**本部门**的账号；能发起任务、用对话助手，但**不能审批、不能对外发信**",
        # 为什么刻意不给 `task:approve` / `mail:send` —— 这两个权限**没有部门维度**。
        # 本系统的人工闸口是全局的：有审批权的人能批**任何**部门提交的任务。
        # 把审批权给部门主管，实际效果是"给了他全公司的审批权"，
        # 而不是"他能批本部门的事"。在没有做出部门级审批隔离之前，
        # 宁可他做不了，也不要他做过了 —— 后者出事时没人能追溯清楚。
        frozenset(
            _READONLY
            | {
                PERM_DEPT_USER,
                PERM_CHAT_USE,
                PERM_TASK_SUBMIT,
                PERM_TICKET_HANDLE,
                PERM_ASSET_WRITE,
                PERM_OFFICE_READ,
            }
        ),
    ),
    RoleDef(
        ROLE_OPERATOR,
        "运营专员",
        "能发起任务、生成报表、用对话助手、处理客服工单，**不能审批、不能对外发信**",
        frozenset(
            _READONLY
            | {PERM_TASK_SUBMIT, PERM_CHAT_USE, PERM_TICKET_HANDLE, PERM_ASSET_WRITE, PERM_OFFICE_READ}
        ),
    ),
    RoleDef(
        ROLE_ANALYST,
        "数据分析",
        "能发起任务并导出报表、用对话助手，不能审批",
        frozenset(_READONLY | {PERM_TASK_SUBMIT, PERM_CHAT_USE, PERM_ASSET_WRITE, PERM_OFFICE_READ}),
    ),
    RoleDef(
        ROLE_VIEWER,
        "只读访客",
        "只能看，不能发起任何任务",
        _READONLY | {PERM_OFFICE_READ},
    ),
)

ROLE_CODES: Final[frozenset[str]] = frozenset(r.code for r in ROLE_PRESETS)


def role_permissions(role_code: str) -> frozenset[str]:
    """内置角色的默认授权。未知角色返回空集，**不抛异常** ——

    自定义角色是在数据库里建的，不在 `ROLE_PRESETS` 里。
    这里若抛异常，调用方就得先判断"是不是内置角色"，多一处容易忘的分支。
    """
    for preset in ROLE_PRESETS:
        if preset.code == role_code:
            return preset.permissions
    return frozenset()


# ===========================================================================
# 3. 同步进数据库
# ===========================================================================
#: 权限码被**移除**时的处理方式：删掉行（连带 role_permissions 级联）。
#: 默认开启 —— 留着已消失的权限码，会让"这个角色到底能干什么"变得无法回答。


def sync_permissions(session: Session) -> dict[str, int]:
    """把 `PERMISSIONS` 幂等同步进 `permissions` 表。

    返回统计字典，供 CLI 打印。**幂等**是硬要求：
    这个方法在每次 `fashionops-db init` 后都会跑，重复执行不能产生重复行。
    """
    existing = {p.code: p for p in session.scalars(select(Permission))}
    created = 0
    updated = 0

    for spec in PERMISSIONS:
        row = existing.get(spec.code)
        if row is None:
            session.add(
                Permission(
                    code=spec.code,
                    name=spec.name,
                    category=spec.category,
                    description=spec.description,
                )
            )
            created += 1
            continue
        # 名称/分类是给人看的，改了就直接覆盖（代码是事实源）
        if (row.name, row.category) != (spec.name, spec.category):
            row.name = spec.name
            row.category = spec.category
            row.description = spec.description
            updated += 1

    # 代码里已经不存在的权限码：删掉
    removed = 0
    for code, row in existing.items():
        if code not in PERMISSION_CODES:
            session.delete(row)
            removed += 1

    session.flush()
    return {"created": created, "updated": updated, "removed": removed}


def sync_roles(session: Session) -> dict[str, int]:
    """幂等创建内置角色，并**仅在首次创建时**写入默认授权。

    为什么不在每次同步时重写授权：运维在界面上给"运营主管"多加了一项权限，
    下次发版跑一遍同步就被抹掉了 —— 这种"代码悄悄改数据"的行为会让运维
    再也不敢用界面改配置。**内置角色的默认值只在出生时生效。**

    ## 但这个决定有一个必须知道的副作用

    新加一个权限码（比如 `chat:use`）时，**已存在的角色不会自动拿到它** ——
    表现是升级之后新功能对所有人都是 403，而报错长得很像"角色配错了"，
    排查方向会跑到角色表上去。

    处理方式是**把缺口报出来**（返回里的 ``missing``），而不是默默补上：
    补上就回到了"代码改数据"；不报出来就是静默失效。
    真要补，用 :func:`apply_role_presets` —— 那是一个**显式**动作。
    """
    # 先把权限码查出来（需要实体，不能只拿 code）
    perm_by_code = {p.code: p for p in session.scalars(select(Permission))}
    created = 0
    granted = 0
    missing: dict[str, list[str]] = {}

    for preset in ROLE_PRESETS:
        row = session.scalar(select(Role).where(Role.code == preset.code))
        if row is not None:
            # 已存在：只同步"名字/描述"这些展示字段，不动授权
            if (row.name, row.description) != (preset.name, preset.description):
                row.name = preset.name
                row.description = preset.description
            # 但要把"预设里有、库里没有"的差集报出来（见 docstring）
            gap = preset.permissions - set(row.permission_codes())
            if gap:
                missing[preset.code] = sorted(gap)
            continue

        row = Role(
            code=preset.code,
            name=preset.name,
            description=preset.description,
            is_system=preset.is_system,
        )
        session.add(row)
        session.flush()
        created += 1

        for code in sorted(preset.permissions):
            perm = perm_by_code.get(code)
            if perm is None:
                # 理论上不会发生（sync_permissions 先跑）。真发生了说明调用顺序错了，
                # 这里静默跳过 + 由调用方对账，比抛异常让整个初始化失败要好
                continue
            session.add(RolePermission(role_id=row.id, permission_id=perm.id))
            granted += 1

    session.flush()
    return {"created": created, "granted": granted, "missing": missing}


def apply_role_presets(session: Session) -> dict[str, int]:
    """把内置角色的预设授权**补充**到已有角色上（**只加不删**）。

    ## 什么时候用

    升级后新加的权限码没进到已有角色里（`sync_roles` 不会自动补），
    新功能因此对所有人 403。这时候跑一次这个。

    ## 为什么不删

    删权限会让"某个人昨天还能做的事今天做不了"—— 而那种问题
    在出事前**没人会发现**。加权限最坏是"多点了一次确认"，
    删权限最坏是"业务停摆"。代价不对等，所以只加不删。
    """
    perm_by_code = {p.code: p for p in session.scalars(select(Permission))}
    added = 0
    touched: set[str] = set()

    for preset in ROLE_PRESETS:
        row = session.scalar(select(Role).where(Role.code == preset.code))
        if row is None:
            continue
        gap = preset.permissions - set(row.permission_codes())
        for code in sorted(gap):
            perm = perm_by_code.get(code)
            if perm is None:
                continue
            session.add(RolePermission(role_id=row.id, permission_id=perm.id))
            added += 1
            touched.add(preset.code)

    session.flush()
    return {"added": added, "roles": len(touched)}


def sync_identity(session: Session) -> dict[str, dict[str, int]]:
    """权限 + 角色一起同步。调用方（`fashionops-db init` / 启动自检）只需要这一个入口。"""
    return {"permissions": sync_permissions(session), "roles": sync_roles(session)}


def user_permission_codes(user) -> frozenset[str]:  # noqa: ANN001
    """把一个用户的权限算出来：所有角色的权限**并集**。

    超级管理员直接返回全部权限。用 `is_superuser` 而不是"给他挂 admin 角色"：
    初始化时还没有任何角色，如果两者耦合，第一个管理员就创建不出来（先有鸡还是先有蛋）。
    """
    if getattr(user, "is_superuser", False):
        return ALL_PERMISSIONS
    out: set[str] = set()
    for role in user.roles:
        out.update(role.permission_codes())
    return frozenset(out)


__all__ = [
    "ALL_PERMISSIONS",
    "PERMISSIONS",
    "PERMISSION_CODES",
    "ROLE_ANALYST",
    "ROLE_ADMIN",
    "ROLE_CODES",
    "ROLE_DEPT_MANAGER",
    "ROLE_OPERATOR",
    "ROLE_OPS_MANAGER",
    "ROLE_PRESETS",
    "ROLE_VIEWER",
    "PERM_ADMIN_AUDIT",
    "PERM_ADMIN_ROLE",
    "PERM_ADMIN_USER",
    "PERM_DEPT_USER",
    "PERM_AD_WRITE",
    "PERM_AD_READ",
    "PERM_ASSET_WRITE",
    "PERM_ECOM_READ",
    "PERM_ECOM_WRITE",
    "PERM_ERP_READ",
    "PERM_ERP_WRITE",
    "PERM_KB_READ",
    "PERM_MAIL_ADMIN",
    "PERM_MAIL_SEND",
    "PERM_OFFICE_READ",
    "PERM_OFFICE_WRITE",
    "PERM_TASK_APPROVE",
    "PERM_TASK_CANCEL",
    "PERM_TASK_READ",
    "PERM_TASK_SUBMIT",
    "PermissionDef",
    "RoleDef",
    "role_permissions",
    "sync_identity",
    "sync_permissions",
    "sync_roles",
    "user_permission_codes",
]
