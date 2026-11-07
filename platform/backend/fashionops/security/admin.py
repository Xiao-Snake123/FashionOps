"""分级账号管理 —— **规则全部集中在这里**。

## 三层管理权限

| 角色 | 权限码 | 能管谁 |
| --- | --- | --- |
| 主管理员 | `is_superuser` | 所有账号，含其他管理员的密码重置 |
| 管理员 | `admin:user` | **跨部门**的所有账号，但不能碰超级管理员 |
| 部门主管 | `dept:user` | **仅本部门**的账号 |

## 为什么规则要单独成一个模块

接口（`api/routers/admin.py`）和命令行（`security/cli.py`）都要建账号、都要重置密码。
如果把"部门主管不能跨部门"写进接口，CLI 那条路就成了绕过它的后门 ——
而且这个后门不会有任何报错，它只会**正常工作**，直到有人用它建了个跨部门的号。

规则只有一份，入口有几个都不怕。

## 四条不可绕过的规则

### 1. 部门边界

`dept:user` 只能操作 `department_id` 与自己相同的账号。没有部门的人
（`department_id is None`）**谁都管不了** —— 包括"和他一样没有部门的人"。
"都没有部门"不是"同一个部门"，把两者当成相等会让所有无部门账号互相可见。

### 2. 只能管理权限不高于自己的账号

判定方式**有两条，不能只留一条**：

    2a. 对方是超级管理员 且 我不是  →  拒绝
    2b. 对方的权限 - 我的权限 ≠ ∅  →  拒绝

**为什么 2a 不能省**（这一条是被测试逼出来的，值得记下来）：
`admin` 角色持有**全部权限**，而超级管理员的 `user_permission_codes()`
返回的**也是全部权限**。两者相减是空集 —— 只留 2b 的话，
任何一个挂着 admin 角色的人都能停用超管、接管系统，
而这条越权在代码里看起来完全正常。

根因是把 `is_superuser` 当成了"权限的另一种写法"。它不是：
它是一个**绕过检查的开关**（见 `permissions.user_permission_codes` 里的提前返回），
开关和权限集合不可比，必须单独判。

2b 负责的是 2a 覆盖不到的部分：部门主管动不了同部门里挂着 admin 角色的同事
（对方多出 `admin:user`），以及以后任何"权限比我多"的新情况 —— 不用再补判断。

用角色名去比（"admin 比 dept_manager 大"）则会引入一张**需要人工维护的等级表**，
而那张表迟早会和实际的权限分配脱节：给 `operator` 悄悄加一条 `mail:send`
之后，等级表没有任何变化，但"谁高谁低"已经变了。

### 3. 不能操作自己

不能停用自己、不能改自己的角色、不能把自己挪出部门、不能重置自己的密码。

密码那一条特别说明：管理员改**自己**的密码走的是「修改密码」入口
（`POST /auth/change-password`），那条路径要求提供**原密码**。
走管理接口重置自己的密码会跳过原密码校验 ——
如果浏览器没锁屏，任何人坐过来就能把密码改掉并接管账号。

### 4. 不能授予自己没有的权限

只能授予权限集合是**自己权限子集**的角色。否则部门主管
（有 `dept:user`）可以给自己发一个 `admin` 角色，分级授权当场失效。

推论：需要"建号但暂时不授权"时，可以给空角色列表 —— 空集是任何集合的子集。

## 账号不物理删除

接口只提供"停用"。"删账号"会把 `user_roles` 级联删掉、把审计里的
`actor_user_id` 置空，而审计记录**必须保持"当时发生了什么"**。
停用之后账号登不进来、不占任何席位，效果等价，但历史留得住。

Revision 级别的删除（清理测试数据）请用 CLI，不要做成接口。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from fashionops.db.models_identity import (
    USER_STATUS_ACTIVE,
    USER_STATUS_DISABLED,
    USER_STATUS_LOCKED,
    USER_STATUS_PENDING,
    Department,
    Role,
    User,
    UserRole,
)
from fashionops.security import passwords
from fashionops.security.permissions import (
    PERM_ADMIN_USER,
    PERM_DEPT_USER,
    user_permission_codes,
)
from fashionops.security.service import Principal, create_user, set_password

logger = logging.getLogger("fashionops.security.admin")

#: 加载用户时一并把角色和权限带回来。计算"对方权限高不高"必须要它们，
#: 而列表页有 N 个用户 —— 不预加载就是 N+1 次查询。
_USER_LOAD = selectinload(User.roles).selectinload(Role.permissions)

#: 允许通过管理接口设置的状态。`locked` 是**自动**产生的（连续登录失败），
#: `pending` 是邀请流程的中间态 —— 让管理员随手把账号标成这两种，
#: 只会制造"状态是 locked 但锁定期早过了"这种自相矛盾的数据。
SETTABLE_STATUSES: frozenset[str] = frozenset({USER_STATUS_ACTIVE, USER_STATUS_DISABLED})

_UNSET: Any = object()


class AdminError(Exception):
    """账号管理规则被违反。

    `status` 让路由层不用猜该回 400 还是 403 —— 猜错的后果是用户看到
    "你的请求有误"却根本不知道自己没权限，或者反过来看到"无权限"
    其实只是名字重复。规则层最清楚是哪种，就由规则层给出。
    """

    def __init__(self, message: str, *, reason: str = "", status: int = 400) -> None:
        super().__init__(message)
        self.reason = reason
        self.status = status


def _forbidden(message: str, *, reason: str) -> AdminError:
    return AdminError(message, reason=reason, status=403)


# ===========================================================================
# 作用域
# ===========================================================================
@dataclass(frozen=True)
class AdminScope:
    """一次账号管理请求的可见范围。"""

    principal: Principal
    #: True = 跨部门（超级管理员 / `admin:user`）
    global_scope: bool
    #: `global_scope=False` 时生效的部门。None 表示"没有部门可管"
    department_id: int | None = None

    @property
    def label(self) -> str:
        if self.principal.is_superuser:
            return "全部账号（超级管理员）"
        if self.global_scope:
            return "全部账号（跨部门）"
        if self.department_id is None:
            return "未归属部门 —— 无法管理任何账号"
        return "本部门账号"


def resolve_scope(principal: Principal) -> AdminScope:
    """把主体换算成"他能管到哪"。

    `admin:user` 优先于 `dept:user`：一个人同时挂两种角色时按宽的那个算。
    这不是"取更宽松的"的随意选择 —— 他既然被显式授予了跨部门权限，
    再按部门收窄等于那个授权从来没生效过。
    """
    if principal.is_superuser or principal.has(PERM_ADMIN_USER):
        return AdminScope(principal=principal, global_scope=True)
    if principal.has(PERM_DEPT_USER):
        return AdminScope(
            principal=principal,
            global_scope=False,
            department_id=principal.department_id,
        )
    raise _forbidden("当前账号没有账号管理权限", reason="no_admin_permission")


# ===========================================================================
# 判定：能不能管这个人
# ===========================================================================
def manage_blocker(scope: AdminScope, target: User) -> str | None:
    """能不能管理 `target`。可以则返回 None，否则返回**给人看**的原因。

    做成"返回原因"而不是"返回布尔"，是因为界面上要把原因显示出来：
    一个灰色的"编辑"按钮不解释为什么，用户只会以为系统坏了。
    """
    principal = scope.principal

    # 规则 2a：超级管理员必须**单独判**，不能只靠下面的权限集合比较。
    #
    # 这条是被测试逼出来的：`admin` 角色持有全部权限，而超级管理员的
    # `user_permission_codes()` 也是全部权限 —— 两者相减是**空集**，
    # 于是"普通管理员不能碰超管"这条规则形同虚设，任何一个挂着 admin 角色
    # 的人都能停用超管并接管系统。
    #
    # 根因是把 `is_superuser` 当成了"权限的另一种表示"。它不是 ——
    # 它是一个**绕过检查的开关**（见 `permissions.user_permission_codes`），
    # 开关和权限集合不可比，必须显式比。
    if target.is_superuser and not principal.is_superuser:
        return "对方是超级管理员，只有超级管理员本人能管理该账号"

    # 规则 2b：权限集合的包含关系。覆盖面最广的一条 ——
    # 部门主管动不了同部门里的管理员，普通管理员也动不了权限比自己多的人。
    if not principal.is_superuser:
        extra = user_permission_codes(target) - principal.permissions
        if extra:
            shown = "、".join(sorted(extra)[:3])
            more = "…" if len(extra) > 3 else ""
            return f"对方拥有你没有的权限，不能管理（{shown}{more}）"

    # 规则 1：部门边界
    if not scope.global_scope:
        if scope.department_id is None:
            return "你的账号还没有归属部门，无法管理任何账号"
        if target.department_id != scope.department_id:
            return "只能管理本部门的账号"

    return None


def _assert_manageable(scope: AdminScope, target: User, *, action: str) -> None:
    blocker = manage_blocker(scope, target)
    if blocker:
        raise _forbidden(f"无法{action}：{blocker}", reason="not_manageable")


def _assert_not_self(scope: AdminScope, target: User, *, action: str) -> None:
    """规则 3：不能对自己做这些动作。"""
    if target.id == scope.principal.user_id:
        raise AdminError(
            f"不能{action}自己的账号。修改自己的密码请用「修改密码」入口（需要原密码）",
            reason="self_target",
        )


# ===========================================================================
# 角色可授予范围
# ===========================================================================
def all_roles(session: Session) -> list[Role]:
    return list(session.scalars(select(Role).order_by(Role.id)))


def assignable_roles(session: Session, scope: AdminScope) -> list[Role]:
    """规则 4：我只能授予"权限是我权限子集"的角色。

    超级管理员不受限。非超管逐个角色比较权限集合。

    **允许主管授予自己的同级角色**（部门主管可以设另一个部门主管）。
    这看起来像"可以无限复制自己"，但复制出来的人范围完全相同 ——
    都在同一个部门、都没有审批权，受影响面没有扩大，
    而"主管休假时得有人能建号"是真实需求。
    真正危险的**跨级**授予被权限子集这一条挡住了。
    """
    roles = all_roles(session)
    if scope.principal.is_superuser:
        return roles
    mine = scope.principal.permissions
    return [r for r in roles if r.permission_codes() <= mine]


def _resolve_roles(session: Session, scope: AdminScope, codes: list[str]) -> list[Role]:
    """把角色码解析成角色实体，并校验每一个都在可授予范围内。"""
    by_code = {r.code: r for r in all_roles(session)}
    unknown = [c for c in codes if c not in by_code]
    if unknown:
        raise AdminError(f"角色不存在：{'、'.join(unknown)}", reason="unknown_role")

    allowed = {r.code for r in assignable_roles(session, scope)}
    forbidden = [c for c in codes if c not in allowed]
    if forbidden:
        raise _forbidden(
            f"不能授予这些角色（超出你自己的权限范围）：{'、'.join(forbidden)}",
            reason="privilege_escalation_blocked",
        )
    # 去重但保持传入顺序：界面上勾选顺序就是用户看到的顺序
    seen: dict[str, Role] = {}
    for code in codes:
        seen[code] = by_code[code]
    return list(seen.values())


# ===========================================================================
# 读
# ===========================================================================
def list_departments(session: Session, scope: AdminScope) -> list[Department]:
    """可见的部门。

    部门主管只看到本部门 —— 下拉框里列出别的部门，等于告诉他
    "你可以试试"，试完再被后端拒掉。
    """
    stmt = select(Department).order_by(Department.id)
    if not scope.global_scope:
        if scope.department_id is None:
            return []
        stmt = stmt.where(Department.id == scope.department_id)
    return list(session.scalars(stmt))


def account_stats(session: Session, scope: AdminScope) -> dict[str, int]:
    """按状态统计**作用域内**的账号数。

    为什么这个数必须由后端算，而不是前端拿当前页 `items` 去 filter：
    分页之后，"这一页里有 3 个已停用"和"全部有 3 个已停用"在界面上
    长得一模一样，而后者是错的。统计数字看起来越正常，越不会有人去核对它。
    """
    if not scope.global_scope:
        if scope.department_id is None:
            return dict.fromkeys(("total", "active", "disabled", "locked", "pending", "superusers"), 0)
        scope_filter = [User.department_id == scope.department_id]
    else:
        scope_filter = []

    rows = session.execute(
        select(User.status, func.count()).select_from(User).where(*scope_filter).group_by(User.status)
    ).all()
    by_status = {status: int(count) for status, count in rows}

    superusers = (
        session.scalar(select(func.count()).select_from(User).where(*scope_filter, User.is_superuser.is_(True))) or 0
    )

    return {
        "total": sum(by_status.values()),
        "active": by_status.get(USER_STATUS_ACTIVE, 0),
        "disabled": by_status.get(USER_STATUS_DISABLED, 0),
        "locked": by_status.get(USER_STATUS_LOCKED, 0),
        "pending": by_status.get(USER_STATUS_PENDING, 0),
        "superusers": int(superusers),
    }


def get_account(session: Session, user_id: int) -> User | None:
    """按 id 取账号，并把角色/权限一并带回来。

    单独一个函数是为了让"取出来的 User 一定带着 roles.permissions"这件事
    只有一个来源 —— 忘了加 `_USER_LOAD` 的话，后面算"对方权限高不高"
    会退化成按需加载，甚至在某些 session 状态下直接算成空集，
    而空集意味着"这个人权限最低，谁都能管"，是个**开在权限判定上的洞**。
    """
    return session.scalar(select(User).options(_USER_LOAD).where(User.id == user_id))


def list_accounts(
    session: Session,
    scope: AdminScope,
    *,
    query: str | None = None,
    status: str | None = None,
    department_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[User], int]:
    """按作用域过滤后的账号列表 + 真实总数。

    总数**先于分页计算**：前端要显示"共 N 个"，用它减当前条数才知道
    还有没有下一页。返回 `len(items)` 会让"还有 3 个没加载"永远显示不出来。
    """
    filters = []

    if not scope.global_scope:
        # 没有部门的主管查不到任何人 —— 返回空列表而不是放行。
        # 放行会让"忘了给这个人分配部门"变成"这个人能看全公司账号"。
        if scope.department_id is None:
            return [], 0
        filters.append(User.department_id == scope.department_id)

    if department_id is not None and scope.global_scope:
        filters.append(User.department_id == department_id)

    if status:
        filters.append(User.status == status)

    if query:
        like = f"%{query.strip()}%"
        filters.append(
            or_(
                User.username.like(like),
                User.full_name.like(like),
                User.email.like(like),
            )
        )

    total = session.scalar(select(func.count()).select_from(User).where(*filters)) or 0

    items = list(
        session.scalars(select(User).options(_USER_LOAD).where(*filters).order_by(User.id).limit(limit).offset(offset))
    )
    return items, int(total)


# ===========================================================================
# 写
# ===========================================================================
@dataclass
class CreatedAccount:
    user: User
    #: 自动生成的初始密码，**只在这里出现一次**。落库的是哈希，之后再也取不出来。
    generated_password: str | None = None
    warnings: list[str] = field(default_factory=list)


def create_account(
    session: Session,
    scope: AdminScope,
    *,
    username: str,
    password: str | None = None,
    full_name: str = "",
    email: str | None = None,
    phone: str | None = None,
    department_id: int | None = None,
    role_codes: list[str] | None = None,
    is_superuser: bool = False,
    settings: Any = None,
) -> CreatedAccount:
    """新建账号。密码不传就自动生成并**只返回一次**。"""
    principal = scope.principal

    if is_superuser and not principal.is_superuser:
        raise _forbidden("只有超级管理员能创建超级管理员账号", reason="escalation_superuser")

    # 部门：部门主管建号一律落在自己部门。
    # 显式传了别的部门时**报错而不是静默改写** —— 静默改写会让调用方
    # 以为自己成功地把人放进了目标部门，直到很久以后对不上号才发现。
    if not scope.global_scope:
        if scope.department_id is None:
            raise _forbidden("你的账号还没有归属部门，无法新建账号", reason="actor_without_department")
        if department_id is not None and department_id != scope.department_id:
            raise _forbidden("只能在本部门内新建账号", reason="out_of_department")
        department_id = scope.department_id

    if department_id is not None:
        _assert_department_exists(session, department_id)

    roles = _resolve_roles(session, scope, list(role_codes or []))

    result = create_user(
        session,
        username=username,
        password=password,
        email=email,
        full_name=full_name,
        roles=(),
        is_superuser=is_superuser,
        created_by=principal.username,
        settings=settings,
    )
    user = result.user
    user.department_id = department_id
    user.phone = phone
    session.flush()

    if roles:
        _replace_roles(session, user, roles, granted_by=principal.username)

    warnings = list(result.warnings)
    if not roles and not is_superuser:
        # 没给角色的账号登进去什么都看不见。这不是错误（"先建号、后授权"
        # 是合理的流程），但必须说出来，否则会变成一张"账号建了但没有用"的工单
        warnings.append("该账号还没有任何角色，登录后将看不到任何功能")
    return CreatedAccount(user=user, generated_password=result.generated_password, warnings=warnings)


def _assert_department_exists(session: Session, department_id: int) -> Department:
    dept = session.get(Department, department_id)
    if dept is None:
        raise AdminError(f"部门 {department_id} 不存在", reason="unknown_department")
    if not dept.is_active:
        raise AdminError(f"部门「{dept.name}」已停用，不能再往里面加人", reason="department_inactive")
    return dept


def _replace_roles(session: Session, user: User, roles: list[Role], *, granted_by: str) -> None:
    """把用户的角色**替换**成给定集合（不是追加）。

    替换语义是接口唯一能表达"我把他的权限改成了什么"的方式。
    追加语义下，想收回一个角色只能靠"删除"，而删除需要另一个接口 ——
    两个接口交替调用，最终状态取决于调用顺序，排查问题时无法复现。
    """
    session.execute(UserRole.__table__.delete().where(UserRole.user_id == user.id))
    session.flush()
    for role in roles:
        session.add(UserRole(user_id=user.id, role_id=role.id, granted_by=granted_by))
    session.flush()
    session.expire(user, ["roles"])


def update_account(
    session: Session,
    scope: AdminScope,
    target: User,
    *,
    full_name: Any = _UNSET,
    email: Any = _UNSET,
    phone: Any = _UNSET,
    department_id: Any = _UNSET,
    status: Any = _UNSET,
    role_codes: Any = _UNSET,
) -> list[str]:
    """改账号资料 / 部门 / 状态 / 角色。返回给操作者看的提示（可能为空）。

    用 `_UNSET` 哨兵而不是 `None` 做默认值：`None` 是一个**合法的目标值**
    （把邮箱清空、把部门清空），两者混在一起就永远改不出"清空"这个结果。
    """
    _assert_manageable(scope, target, action="修改")

    notes: list[str] = []

    # ---- 状态 ----
    if status is not _UNSET:
        if status not in SETTABLE_STATUSES:
            raise AdminError(
                f"状态只能是 {' 或 '.join(sorted(SETTABLE_STATUSES))}",
                reason="bad_status",
            )
        if target.status != status:
            blocked_action = "停用" if status == USER_STATUS_DISABLED else "启用"
            _assert_not_self(scope, target, action=blocked_action)
            target.status = status
            if status == USER_STATUS_ACTIVE:
                # "解锁"必须一条路走完：只改状态不清锁定期，
                # 会留下"状态 active 但 locked_until 还在未来"的账号，
                # 而它下次登录仍然被拒 —— 表现是"管理员说解开了，但还是登不进"
                target.locked_until = None
                target.failed_login_count = 0
                notes.append("已同时清除登录失败计数与锁定状态")

    # ---- 角色 ----
    if role_codes is not _UNSET:
        _assert_not_self(scope, target, action="修改角色")
        roles = _resolve_roles(session, scope, list(role_codes or []))
        current = {r.id for r in target.roles}
        if current != {r.id for r in roles}:
            _replace_roles(session, target, roles, granted_by=scope.principal.username)

    # ---- 部门 ----
    if department_id is not _UNSET:
        _assert_not_self(scope, target, action="调整部门")
        if not scope.global_scope and department_id != scope.department_id:
            raise _forbidden("只能把账号留在本部门内", reason="out_of_department")
        if department_id is not None:
            _assert_department_exists(session, department_id)
        target.department_id = department_id

    # ---- 资料 ----
    if full_name is not _UNSET:
        target.full_name = (full_name or "").strip()
    if email is not _UNSET:
        from fashionops.security.service import find_user_by_email

        cleaned = (email or "").strip() or None
        if cleaned:
            existing = find_user_by_email(session, cleaned)
            if existing is not None and existing.id != target.id:
                raise AdminError(f"邮箱 {cleaned} 已被占用", reason="duplicate_email")
        target.email = cleaned
    if phone is not _UNSET:
        target.phone = (phone or "").strip() or None

    session.flush()
    return notes


def reset_password(
    session: Session,
    scope: AdminScope,
    target: User,
    *,
    password: str | None = None,
    settings: Any = None,
) -> str:
    """重置密码，返回**明文**（调用方负责只显示一次）。返回空串表示用了调用方给的值。"""
    _assert_manageable(scope, target, action="重置密码")
    _assert_not_self(scope, target, action="重置")

    generated: str | None = None
    if password is None:
        generated = passwords.generate_password()
        password = generated

    # 走 service.set_password：它负责校验强度，并且**撤销该用户全部会话**。
    # 撤销这一步不能省 —— 重置密码的动机通常是"怀疑账号被盗"，
    # 旧会话还在的话攻击者手里的 refresh token 照样能换新 access token，
    # 重置就成了心理安慰。
    set_password(session, target, password)
    logger.info("管理员 %s 重置了 %s 的密码", scope.principal.username, target.username)
    return generated or ""


# ===========================================================================
# 序列化（接口与 CLI 共用，避免两处各写一份对不上）
# ===========================================================================
def serialize_account(scope: AdminScope, user: User) -> dict[str, Any]:
    blocker = manage_blocker(scope, user)
    # 自己的账号直接标成不可管理，而不是让界面显示一排按下去必然报错的按钮。
    # 密码这类改动走 `POST /auth/change-password`（要求原密码），
    # 那是另一条路径，不该在这个页面上开一个绕过原密码校验的口子。
    if blocker is None and user.id == scope.principal.user_id:
        blocker = "这是你自己的账号（修改密码请用「修改密码」入口）"
    return {
        "user_id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "email": user.email,
        "phone": user.phone,
        "status": user.status,
        "is_superuser": user.is_superuser,
        "department_id": user.department_id,
        "roles": user.role_codes(),
        "permissions": sorted(user_permission_codes(user)),
        "last_login_at": user.last_login_at.isoformat(timespec="seconds") if user.last_login_at else None,
        "created_at": user.created_at.isoformat(timespec="seconds") if user.created_at else None,
        # 供界面决定这一行的按钮是可用还是灰掉。**不是安全边界** ——
        # 后端每次写操作都会重新判一遍。
        "can_manage": blocker is None,
        "manage_blocked_reason": blocker,
        "is_self": user.id == scope.principal.user_id,
    }


__all__ = [
    "SETTABLE_STATUSES",
    "AdminError",
    "AdminScope",
    "CreatedAccount",
    "account_stats",
    "all_roles",
    "assignable_roles",
    "create_account",
    "get_account",
    "list_accounts",
    "list_departments",
    "manage_blocker",
    "reset_password",
    "resolve_scope",
    "serialize_account",
    "update_account",
]
