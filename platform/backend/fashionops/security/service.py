"""认证与会话服务 —— 登录、刷新、登出、权限加载。

这个模块是"身份域"的业务逻辑层。HTTP 路由（`api/routers/auth.py`）只做
参数解析和状态码映射，所有判断都在这里，这样 CLI、测试、将来的 gRPC 入口
能复用同一套规则。

## 一条贯穿全模块的原则：对外的话术要和日志的原因分开

`AuthError` 有两个属性：
- `message` —— 给最终用户看的，**不区分具体原因**
- `reason` —— 只写日志，**区分到很细**

原因是这两类受众的需求正好相反：

| 场景 | 用户需要知道 | 攻击者需要知道 |
| --- | --- | --- |
| 用户名不存在 | 无（都一样） | **有**（能枚举有效用户名） |
| 密码错 | 无 | **有**（知道用户名对了，可以专心爆破） |
| 账号被锁 | **有**（否则他会一直试） | 有（能确认锁号攻击生效） |

处理办法：**"凭据不对"这一大类统一话术**（覆盖用户名不存在 / 密码错），
而"账号被锁"和"账号被禁用"如实告知 —— 因为这两种情况下用户必须知道
自己该去做什么（等 15 分钟 / 找管理员），而攻击者本来也能从"试对了密码却登不进"
推断出来。**不告诉用户反而把攻击者的信息优势变成了用户的痛苦。**
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fashionops.db.base import utcnow
from fashionops.db.models_identity import (
    USER_STATUS_ACTIVE,
    USER_STATUS_LOCKED,
    ExternalIdentity,
    RefreshToken,
    Role,
    User,
    UserRole,
)
from fashionops.security import passwords
from fashionops.security.permissions import (
    ALL_PERMISSIONS,
    ROLE_ADMIN,
    user_permission_codes,
)
from fashionops.security.tokens import (
    TokenError,
    TokenPair,
    TokenService,
    hash_token,
)

logger = logging.getLogger("fashionops.security.service")


# ===========================================================================
# 主体（Principal）—— "这次请求是谁在发起"
# ===========================================================================
@dataclass(frozen=True)
class Principal:
    """请求主体。**权限是每次请求现算的，不从 token 里读**。

    理由见 `tokens.py` 的模块文档：token 里只放不会变的东西（user_id/username），
    权限现算才能做到"撤权立即生效"。管理员撤销某人的审批权之后，
    不需要等他手上那个 token 过期。
    """

    user_id: int
    username: str
    full_name: str = ""
    permissions: frozenset[str] = frozenset()
    roles: tuple[str, ...] = ()
    is_superuser: bool = False
    #: 所属部门。**带在主体上是有意的**：账号管理要判"这个人归不归我管"，
    #: 每个请求都回表查一次部门既慢又多一处可能忘记查的地方。
    #: None 表示不属于任何部门（超管、对接账号）。
    department_id: int | None = None
    #: `password` / `api_key` / `oauth:dingtalk`。审计里要记"他是怎么进来的"
    auth_method: str = "password"

    def has(self, *codes: str) -> bool:
        """是否拥有**任意一个**权限（OR 语义，和工具网关一致）。"""
        if self.is_superuser:
            return True
        if not codes:
            return True
        return any(c in self.permissions for c in codes)

    def has_all(self, *codes: str) -> bool:
        if self.is_superuser:
            return True
        return all(c in self.permissions for c in codes)

    @property
    def display(self) -> str:
        return self.full_name or self.username

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "full_name": self.full_name,
            "roles": list(self.roles),
            "permissions": sorted(self.permissions),
            "is_superuser": self.is_superuser,
            "department_id": self.department_id,
            "auth_method": self.auth_method,
        }


#: 未登录主体。**不叫 `ANONYMOUS` 而叫 `NOBODY`**：前者听起来像个正常身份，
#: 后者提醒每个读到它的地方"这里没有身份，要小心"。
NOBODY = Principal(user_id=0, username="", auth_method="anonymous")

#: 系统主体。定时任务、启动自检这类没有用户的动作，用它可以走完整审计链路，
#: 而审计里能明确看出"这不是人干的"。
SYSTEM_PRINCIPAL = Principal(
    user_id=-1,
    username="system",
    full_name="系统",
    permissions=ALL_PERMISSIONS,
    is_superuser=True,
    auth_method="system",
)


class AuthError(Exception):
    """认证失败。`message` 给用户，`reason` 给日志。"""

    def __init__(self, message: str, *, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


def _invalid_credentials() -> AuthError:
    """凭据类失败的**统一**出口。

    刻意做成函数而不是常量：将来要加"失败几次后加延迟"之类的对抗手段，
    只改这一处。四处硬编码同一句话，迟早有一处被改成"用户名不存在"。
    """
    return AuthError("用户名或密码不正确", reason="invalid_credentials")


# ===========================================================================
# 用户查询
# ===========================================================================
def find_user(session: Session, username: str) -> User | None:
    """按用户名查。**大小写不敏感** ——

    用户不会记得自己注册时字母的大小写，而 MySQL 默认的 `utf8mb4_general_ci`
    排序规则本身就是大小写不敏感的，SQLite 却是敏感的。
    不显式处理的话，"本地测试能登录、MySQL 上登不进"这种差异会让排查方向跑偏。
    """
    wanted = username.strip().lower()
    return session.scalar(select(User).where(func_lower(User.username) == wanted))


def func_lower(column):  # noqa: ANN001, ANN201
    """`lower()` 的薄封装。单独一个函数是为了 SQLite/MySQL 行为一致。"""
    from sqlalchemy import func

    return func.lower(column)


def get_user(session: Session, user_id: int) -> User | None:
    return session.get(User, user_id)


def find_user_by_email(session: Session, email: str) -> User | None:
    wanted = email.strip().lower()
    return session.scalar(select(User).where(func_lower(User.email) == wanted))


def find_user_by_external(session: Session, *, provider: str, external_id: str) -> User | None:
    """按第三方身份找用户。扫码登录用。"""
    identity = session.scalar(
        select(ExternalIdentity).where(
            ExternalIdentity.provider == provider,
            ExternalIdentity.external_id == external_id,
        )
    )
    if identity is None:
        return None
    return session.get(User, identity.user_id)


# ===========================================================================
# 主体加载
# ===========================================================================
def load_principal(session: Session, user_id: int, *, auth_method: str = "password") -> Principal:
    """按 user_id 组装 `Principal`。**每次请求都会调**，所以要保持轻量。

    用 `selectinload` 一次把 roles + permissions 拉回来，避免 N+1 ——
    角色数一般 1~3 个，一次 JOIN 拿回来比懒加载划算得多。
    """
    from sqlalchemy.orm import selectinload

    user = session.scalar(
        select(User).options(selectinload(User.roles).selectinload(Role.permissions)).where(User.id == user_id)
    )
    if user is None:
        raise AuthError("登录状态无效，请重新登录", reason="user_not_found")
    if not user.is_active:
        raise AuthError("账号已被停用，请联系管理员", reason=f"status:{user.status}")

    return Principal(
        user_id=user.id,
        username=user.username,
        full_name=user.full_name or "",
        permissions=user_permission_codes(user),
        roles=tuple(user.role_codes()),
        is_superuser=user.is_superuser,
        department_id=user.department_id,
        auth_method=auth_method,
    )


# ===========================================================================
# 登录
# ===========================================================================
def authenticate(
    session: Session,
    *,
    username: str,
    password: str,
    ip: str = "",
    user_agent: str = "",
    max_failures: int = 5,
    lock_minutes: int = 15,
) -> User:
    """校验用户名密码，成功返回 `User`，失败抛 `AuthError`。

    失败计数写在这里而不是"登录接口"里：不管从哪个入口登录（HTTP / CLI / 测试），
    锁定策略都必须生效。写在路由里的话，加一个新入口就等于开了一个爆破通道。

    登录事件也在每一条退出路径上记 —— 理由同上：**入口可以有多个，
    但"谁在什么时候试过登录"只能有一个来源**。
    """
    user = find_user(session, username)

    if user is None:
        # 关键：即使用户不存在也要跑一次哈希校验。
        # 不跑的话，"用户不存在"会在 1ms 内返回，"密码错"要 100ms ——
        # 响应时间直接泄漏出哪些用户名是有效的（时序侧信道）。
        passwords.verify_password(password, None)
        _record_login_event(
            session,
            username=username,
            success=False,
            reason="unknown_user",
            ip=ip,
            user_agent=user_agent,
        )
        raise _invalid_credentials()

    if user.is_locked_now:
        remaining = int((user.locked_until - utcnow()).total_seconds() // 60) + 1
        _record_login_event(
            session,
            username=username,
            user=user,
            success=False,
            reason="locked",
            ip=ip,
            user_agent=user_agent,
        )
        raise AuthError(
            f"账号已被锁定，请 {remaining} 分钟后再试，或联系管理员解锁",
            reason="locked",
        )

    if user.status != USER_STATUS_ACTIVE:
        # 锁定期已过但状态还停在 locked → 顺手复位，省掉一个定时任务。
        # 这种"惰性恢复"比 cron 简单，也不会因为定时任务挂了而卡住用户。
        if user.status == USER_STATUS_LOCKED:
            user.status = USER_STATUS_ACTIVE
        else:
            _record_login_event(
                session,
                username=username,
                user=user,
                success=False,
                reason=f"status:{user.status}",
                ip=ip,
                user_agent=user_agent,
            )
            raise AuthError("账号已被停用，请联系管理员", reason=f"status:{user.status}")

    if not passwords.verify_password(password, user.password_hash):
        _record_failure(session, user, max_failures=max_failures, lock_minutes=lock_minutes)
        _record_login_event(
            session,
            username=username,
            user=user,
            success=False,
            reason="bad_password",
            ip=ip,
            user_agent=user_agent,
        )
        raise _invalid_credentials()

    # ---- 成功 ----
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    user.last_login_ip = ip or None
    if user.status == USER_STATUS_LOCKED:
        user.status = USER_STATUS_ACTIVE

    # 旧哈希参数变弱了就顺手升级 —— 用户什么都不用做。
    # 这是"参数演进"能真正落地的唯一方式：强制全体改密码是不可接受的。
    if user.password_hash and passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash_password(password)
        user.password_changed_at = utcnow()
        logger.info("已升级用户 %s 的密码哈希参数", user.username)

    _record_login_event(
        session,
        username=username,
        user=user,
        success=True,
        ip=ip,
        user_agent=user_agent,
    )
    session.flush()
    return user


def _record_login_event(
    session: Session,
    *,
    username: str,
    user: User | None = None,
    success: bool,
    reason: str | None = None,
    auth_method: str = "password",
    ip: str = "",
    user_agent: str = "",
) -> None:
    """写一条登录事件（成功失败都写）。

    **写失败绝不能影响登录本身**：它是旁路的审计数据，
    为了记一笔日志把正常用户挡在门外是最糟糕的失败模式。
    所以这里只 `warning`，不往上抛。
    """
    from fashionops.db.models import LoginEvent

    try:
        session.add(
            LoginEvent(
                username=(username or "")[:64],
                user_id=(user.id if user is not None else None),
                success=success,
                reason=(reason or None),
                auth_method=auth_method[:32],
                ip=(ip or None),
                user_agent=(user_agent or "")[:255] or None,
            )
        )
        session.flush()
    except Exception as exc:  # noqa: BLE001
        logger.warning("登录事件落库失败（不影响登录流程）：%s", exc)


def _record_failure(session: Session, user: User, *, max_failures: int, lock_minutes: int) -> None:
    """累计失败次数，达到阈值就锁定。"""
    user.failed_login_count = (user.failed_login_count or 0) + 1
    if user.failed_login_count >= max_failures:
        user.locked_until = utcnow() + timedelta(minutes=lock_minutes)
        user.status = USER_STATUS_LOCKED
        logger.warning(
            "用户 %s 连续失败 %d 次，锁定至 %s",
            user.username,
            user.failed_login_count,
            user.locked_until.isoformat(),
        )
    session.flush()


def login(
    session: Session,
    *,
    username: str,
    password: str,
    tokens: TokenService,
    ip: str = "",
    user_agent: str = "",
    max_failures: int = 5,
    lock_minutes: int = 15,
) -> tuple[User, TokenPair]:
    """登录成功 → 落库 refresh token 哈希 → 返回令牌对。

    **refresh token 落库的是哈希**，不是原文。数据库被读走也换不出可用令牌，
    同时仍然能按 user 撤销（企业要的"立刻踢下线"）。
    """
    user = authenticate(
        session,
        username=username,
        password=password,
        ip=ip,
        user_agent=user_agent,
        max_failures=max_failures,
        lock_minutes=lock_minutes,
    )

    pair = tokens.create_pair(user_id=user.id, username=user.username)
    session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_token(pair.refresh_token),
            jti=_jti_of(pair.refresh_token),
            expires_at=pair.refresh_expires_at,
            ip=ip or "",
            user_agent=(user_agent or "")[:255],
        )
    )
    session.flush()

    # 顺手清理这个用户已过期的会话行。放在登录路径上而不是定时任务：
    # 登录本来就是"低频 + 已经要写库"的动作，多一条 DELETE 几乎免费，
    # 而定时任务是一个额外的失效点（`refresh_tokens` 表会无限长下去）。
    _purge_expired(session, user.id)
    return user, pair


def _jti_of(token: str) -> str:
    """从 token 里取 jti，**不解签名**。

    这里只是记录用，token 是我们刚刚自己签的，签名必然正确。
    解签名的话就要传 secret 进来，多一个参数只为一次无意义的校验。
    """
    import jwt as pyjwt

    try:
        return str(pyjwt.decode(token, options={"verify_signature": False}).get("jti", ""))
    except Exception:  # noqa: BLE001
        return ""


def _purge_expired(session: Session, user_id: int) -> int:
    from sqlalchemy import delete

    result = session.execute(
        delete(RefreshToken).where(
            RefreshToken.user_id == user_id,
            RefreshToken.expires_at < utcnow(),
        )
    )
    return result.rowcount or 0


# ===========================================================================
# 刷新
# ===========================================================================
def refresh(
    session: Session,
    *,
    refresh_token: str,
    tokens: TokenService,
    ip: str = "",
    user_agent: str = "",
) -> tuple[User, TokenPair]:
    """用 refresh token 换一对新的。

    ## 三层校验，缺一不可

    1. **签名与有效期**（`tokens.decode_refresh`）—— 挡住伪造和过期的。
    2. **库里有没有这一行** —— 挡住"已登出的 token"。
       纯 JWT 做不到这一点，这正是要入库的原因。
    3. **是否已被替换过** —— 挡住**重放**。

    第 3 条是这里最容易漏的：token 有效、库里也有，但它已经被用过了。
    正常情况下不该出现（用户换过新的了），出现就说明 **token 被泄漏了** ——
    攻击者拿着旧的来换，而合法用户那份已经在他手里了。

    这时候的正确动作不是"拒绝这一次"，而是**撤销整条链**。
    只拒这一次的话，攻击者手里那个 token 还能继续试，
    而合法用户下次刷新时反而会被自己"上一次的合法操作"踢掉，
    完全不知道发生了什么。
    """
    try:
        claims = tokens.decode_refresh(refresh_token)
    except TokenError as exc:
        raise AuthError("登录已过期，请重新登录", reason=f"token:{exc.reason}") from exc

    row = session.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(refresh_token)))
    if row is None:
        # 签名对但库里没有：可能是**换了 jwt_secret**（重启后自动生成的那种），
        # 也可能是被管理员直接删了行。两种情况都得重新登录。
        raise AuthError("登录状态无效，请重新登录", reason="refresh_not_found")

    if row.revoked_at is None and row.replaced_by_id is not None:
        # 理论上不会出现（轮换时两者一起写）。真出现了说明数据被手工改过。
        logger.warning("refresh token 状态异常：未撤销但有替换者 id=%s", row.id)

    if row.replaced_by_id is not None:
        # ← 重放检测。**必须在下面那个 `revoked_at` 检查之前。**
        #
        # 轮换时会同时写 `revoked_at` 和 `replaced_by_id`，所以如果先判
        # `revoked_at`，重放永远会被归类成"已登出"而不是"重放" ——
        # 后果是**重放检测永远不会触发**，而它恰恰是发现 token 泄漏的唯一信号。
        logger.error(
            "检测到 refresh token 重放：user_id=%s jti=%s，撤销该用户全部会话",
            row.user_id,
            claims.jti,
        )
        revoke_all(session, row.user_id, reason="refresh_token_reuse")
        raise AuthError("检测到异常登录，已强制下线，请重新登录", reason="refresh_replay")

    if row.revoked_at is not None:
        raise AuthError("登录状态已失效，请重新登录", reason="refresh_revoked")

    if row.expires_at <= utcnow():
        raise AuthError("登录已过期，请重新登录", reason="refresh_expired")

    user = session.get(User, row.user_id)
    if user is None:
        raise AuthError("登录状态无效，请重新登录", reason="user_not_found")
    if not user.is_active:
        raise AuthError("账号已被停用，请联系管理员", reason=f"status:{user.status}")

    # ---- 轮换：签新的，标记旧的 ----
    pair = tokens.create_pair(user_id=user.id, username=user.username)
    new_row = RefreshToken(
        user_id=user.id,
        token_hash=hash_token(pair.refresh_token),
        jti=_jti_of(pair.refresh_token),
        expires_at=pair.refresh_expires_at,
        ip=ip or row.ip,
        user_agent=(user_agent or row.user_agent)[:255],
    )
    session.add(new_row)
    session.flush()

    row.revoked_at = utcnow()
    row.replaced_by_id = new_row.id
    session.flush()

    return user, pair


# ===========================================================================
# 登出 / 撤销
# ===========================================================================
def logout(session: Session, *, refresh_token: str) -> bool:
    """撤销单个 refresh token。**返回 False 不报错** ——

    登出是"让当前会话失效"，不是"证明这个 token 有效"。
    拿一个已经登出的 token 再登出一次，结果应该是"它确实无效了"，
    而不是 401 —— 前端在 401 时会跳登录页，而用户明明已经登出了，
    再跳一次登录页是纯粹的多余动作（还会覆盖掉"已登出"的提示）。
    """
    row = session.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(refresh_token)))
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = utcnow()
    session.flush()
    return True


def revoke_all(session: Session, user_id: int, *, reason: str = "") -> int:
    """撤销某用户的全部有效会话。用于"踢下线"和安全事件响应。"""
    rows = list(
        session.scalars(
            select(RefreshToken).where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
            )
        )
    )
    now = utcnow()
    for row in rows:
        row.revoked_at = now
    session.flush()
    if rows:
        logger.info("已撤销用户 %s 的 %d 个会话（%s）", user_id, len(rows), reason or "-")
    return len(rows)


def list_sessions(session: Session, user_id: int, *, include_revoked: bool = False) -> list[RefreshToken]:
    """某用户当前的登录设备列表。界面上"退出其他设备"要用。"""
    stmt = select(RefreshToken).where(RefreshToken.user_id == user_id)
    if not include_revoked:
        stmt = stmt.where(RefreshToken.revoked_at.is_(None), RefreshToken.expires_at > utcnow())
    return list(session.scalars(stmt.order_by(RefreshToken.created_at.desc())))


# ===========================================================================
# 权限向下传导
# ===========================================================================
def effective_scopes(principal: Principal, agent_scopes: frozenset[str]) -> frozenset[str]:
    """**用户权限 ∩ Agent 权限集**。

    为什么必须做这个交集，而不是直接用 `AGENT_SCOPES[agent]`：

    提交任务时，Agent 是用"服务身份"跑的。如果 Agent 的权限完全不受提交人影响，
    那么一个只有 `task:submit` 的实习生，就能通过"写个提示词让模型去发邮件"
    达成他自己没有的 `mail:send` —— **权限体系被绕过，而且绕得很自然**。

    交集之后，用户能触发的动作严格是他自己权限的子集。
    这条件码只有一行，但它是整个 RBAC 设计里**唯一真正防住越权**的地方；
    接口上的 `require_permission` 只挡住了"直接调接口"这条路。
    """
    if principal.is_superuser:
        return agent_scopes
    return frozenset(agent_scopes & principal.permissions)


# ===========================================================================
# 用户管理（CLI 与 admin 接口共用）
# ===========================================================================
@dataclass
class CreateUserResult:
    user: User
    generated_password: str | None = None
    warnings: list[str] = field(default_factory=list)


def create_user(
    session: Session,
    *,
    username: str,
    password: str | None = None,
    email: str | None = None,
    full_name: str = "",
    roles: tuple[str, ...] = (),
    is_superuser: bool = False,
    created_by: str = "",
    settings: Any = None,
) -> CreateUserResult:
    """建用户。不传密码就生成一个并**返回明文**（调用方负责显示一次）。"""
    username = username.strip()
    if not username:
        raise AuthError("用户名不能为空", reason="empty_username")
    if len(username) > 64:
        raise AuthError("用户名最长 64 字符", reason="username_too_long")
    if find_user(session, username) is not None:
        raise AuthError(f"用户名 {username} 已存在", reason="duplicate_username")

    warnings: list[str] = []
    generated: str | None = None
    if password is None:
        generated = passwords.generate_password()
        password = generated
    else:
        passwords.validate_strength(
            password,
            min_length=getattr(settings, "password_min_length", 10),
            require_classes=getattr(settings, "password_require_classes", 2),
        )

    if email and find_user_by_email(session, email) is not None:
        raise AuthError(f"邮箱 {email} 已被占用", reason="duplicate_email")

    user = User(
        username=username,
        email=email,
        full_name=full_name or "",
        password_hash=passwords.hash_password(password),
        password_changed_at=utcnow(),
        is_superuser=is_superuser,
        created_by=created_by,
    )
    session.add(user)
    session.flush()

    for code in roles:
        assign_role(session, user, code, granted_by=created_by, warnings=warnings)

    if is_superuser:
        warnings.append("这是超级管理员账号，拥有全部权限，请妥善保管密码")
    return CreateUserResult(user=user, generated_password=generated, warnings=warnings)


def assign_role(
    session: Session,
    user: User,
    role_code: str,
    *,
    granted_by: str = "",
    warnings: list[str] | None = None,
) -> bool:
    """给用户挂角色。已挂过就返回 False（幂等）。"""
    role = session.scalar(select(Role).where(Role.code == role_code))
    if role is None:
        raise AuthError(f"角色 {role_code} 不存在", reason="role_not_found")

    existing = session.scalar(select(UserRole).where(UserRole.user_id == user.id, UserRole.role_id == role.id))
    if existing is not None:
        return False

    session.add(UserRole(user_id=user.id, role_id=role.id, granted_by=granted_by))
    session.flush()

    # 给刚拿管理员角色的人提个醒：这个角色能不能批邮件，取决于角色授权，
    # 而角色授权是可以在界面上被改的 —— 结果就是"看起来是管理员，却批不了邮件"。
    if role_code == ROLE_ADMIN and warnings is not None:
        warnings.append(
            "已授予 admin 角色。注意：**角色授权存在数据库里，可以被界面修改**，"
            "如果发现权限不生效，先跑 `fashionops-user show <username>` 看实际权限"
        )
    return True


def set_password(session: Session, user: User, password: str, *, must_change: bool = False) -> None:
    """改密码并**撤销全部会话**。

    为什么一定要撤销会话：改密码的常见动机是"我怀疑密码泄漏了"。
    如果旧会话还有效，攻击者手上那个 refresh token 还能继续换新 access token ——
    改密码就成了心理安慰。**改密码必须让所有旧登录失效**，这没有例外。
    """
    del must_change  # 保留参数位，将来加"下次登录必须改密码"的标记
    passwords.validate_strength(password)
    user.password_hash = passwords.hash_password(password)
    user.password_changed_at = utcnow()
    user.failed_login_count = 0
    user.locked_until = None
    if user.status == USER_STATUS_LOCKED:
        user.status = USER_STATUS_ACTIVE
    revoke_all(session, user.id, reason="password_changed")
    session.flush()


def ensure_bootstrap_admin(session: Session, settings: Any) -> User | None:
    """首次初始化时建第一个管理员。

    **密码留空就不建**，而不是自动生成一个打印到日志里 ——
    日志会被采集到 ELK/Loki，自动生成的密码写进日志等于把凭据公开。
    宁可让运维显式跑一次 `fashionops-user create-admin`。
    """
    username = (getattr(settings, "bootstrap_admin_username", "") or "").strip()
    password = getattr(settings, "bootstrap_admin_password", None)
    if not username or not password:
        return None

    existing = find_user(session, username)
    if existing is not None:
        # 已存在则**只确保它是超管**，不改密码 ——
        # 否则每次重启都会把运维改过的密码覆盖回配置里那个。
        if not existing.is_superuser:
            existing.is_superuser = True
            session.flush()
        return existing

    result = create_user(
        session,
        username=username,
        password=password,
        full_name="初始管理员",
        roles=(ROLE_ADMIN,),
        is_superuser=True,
        created_by="bootstrap",
        settings=settings,
    )
    logger.info("已创建初始管理员 %s", username)
    return result.user


__all__ = [
    "NOBODY",
    "SYSTEM_PRINCIPAL",
    "AuthError",
    "CreateUserResult",
    "Principal",
    "assign_role",
    "authenticate",
    "create_user",
    "effective_scopes",
    "ensure_bootstrap_admin",
    "find_user",
    "find_user_by_email",
    "find_user_by_external",
    "get_user",
    "list_sessions",
    "load_principal",
    "login",
    "logout",
    "refresh",
    "revoke_all",
    "set_password",
]
