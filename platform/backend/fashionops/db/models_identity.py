"""身份与权限模型 —— 用户 / 角色 / 权限 / 会话 / 外部身份 / API Key（8 张表）。

**为什么单独一个模块而不是塞进 `models.py`**：
`models.py` 是"业务域"（商品/订单/工单/任务…），这里是"身份域"，
两者的变更节奏和读者都不一样（改权限的人不看订单表）。
分开还能让 `models.py` 保持在可读的长度内。

`models.py` 会把本模块的类一并导入并挂进 `ALL_MODELS`，
所以建表、迁移、健康检查都还是走那一份清单。

## 设计上的几个决定

### 1. 权限码定义在**代码**里，角色是**数据**

权限码（`task:approve` 这种）和接口是一一对应的，接口变了权限就要变，
所以它必须跟代码一起走版本控制 —— 放进表里迟早出现
"代码里有这个权限、库里没有"的漂移。

但角色（"运营主管能做哪些事"）是**业务决策**，要能在界面上改，
所以角色和"角色 → 权限"的映射放数据库。

`permissions` 表仍然存在，作用有两个：给角色分配时提供可选清单（FK 完整性），
以及让 "哪些角色有 task:approve" 这种查询能用 SQL 做。
内容由 `security.sync_permissions()` 从代码里的枚举**幂等同步**过来。

### 2. 审计字段要**冗余存一份用户名**

`audit_logs.actor_user_id` 是外键，但**用户被删掉时外键会置空**，
审计记录就变成"某人干的"—— 这对合规毫无价值。

所以同时存 `actor_username` 快照。
审计记录的铁律：**它记录的是"当时发生了什么"，不能随后续变更而改变**。
同理，`tasks.approved_by` 也存快照。

### 3. 审批要留痕到"人"

这是 HITL 闸口存在的全部意义。只记"任务被批准了"没有意义，
必须能回答"**谁**批准了这封要发出去的邮件、什么时候、理由是什么"。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fashionops.db.base import Base, utcnow

# ---------------------------------------------------------------------------
# 用户状态。刻意用字符串而不是 Enum 列：
# 加一个状态值不需要迁移，而 MySQL 的 ENUM 改起来要 ALTER TABLE。
# 取值合法性由应用层校验（`security.UserStatus`）。
# ---------------------------------------------------------------------------
USER_STATUS_ACTIVE = "active"
USER_STATUS_DISABLED = "disabled"  # 管理员手工停用
USER_STATUS_LOCKED = "locked"  # 连续登录失败自动锁定
USER_STATUS_PENDING = "pending"  # 邀请未激活 / 等审批


# ===========================================================================
# 1. 用户
# ===========================================================================
class User(Base):
    """平台用户。

    ``password_hash`` **可空**：走钉钉/企业微信登录的用户不需要密码。
    不能因为"必须设密码"就把 SSO 用户挡在门外，也不能给他们塞一个假密码哈希 ——
    那样"这个账号有没有密码"就没法从数据上看出来了。
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(190), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(128), default="")
    phone: Mapped[str | None] = mapped_column(String(32))

    # argon2id 哈希串（自带 salt 与参数，不用额外存 salt 列）
    password_hash: Mapped[str | None] = mapped_column(String(255))
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime)

    status: Mapped[str] = mapped_column(String(32), default=USER_STATUS_ACTIVE, index=True)
    #: 超级管理员绕过所有权限检查。**只应该有一个**，用于系统初始化。
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False)

    #: 连续失败次数与锁定截止时间。用于防暴力破解。
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_login_ip: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    #: 谁创建的（自注册时为 NULL）。同样存 ID + 快照名。
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_by: Mapped[str] = mapped_column(String(64), default="")

    roles: Mapped[list[Role]] = relationship(
        secondary="user_roles", back_populates="users", lazy="selectin"
    )

    @property
    def is_active(self) -> bool:
        return self.status == USER_STATUS_ACTIVE

    @property
    def is_locked_now(self) -> bool:
        return self.locked_until is not None and self.locked_until > utcnow()

    def role_codes(self) -> list[str]:
        return [r.code for r in self.roles]

    def __repr__(self) -> str:
        return f"<User {self.username} status={self.status}>"


# ===========================================================================
# 2. 角色
# ===========================================================================
class Role(Base):
    """角色。``code`` 是稳定标识（代码里引用它），``name`` 给人看、可改。"""

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")

    #: 系统内置角色不允许在界面上删除 —— 删了会让代码里的 ``ROLE_ADMIN`` 悬空
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    users: Mapped[list[User]] = relationship(secondary="user_roles", back_populates="roles")
    permissions: Mapped[list[Permission]] = relationship(
        secondary="role_permissions", back_populates="roles", lazy="selectin"
    )

    def permission_codes(self) -> set[str]:
        return {p.code for p in self.permissions}

    def __repr__(self) -> str:
        return f"<Role {self.code}>"


# ===========================================================================
# 3. 权限
# ===========================================================================
class Permission(Base):
    """权限码。内容由 ``security.sync_permissions()`` 从代码枚举同步，不要手工插。"""

    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 形如 ``task:approve``。冒号分隔"资源:动作"。
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    #: 粗分类（task / asset / mail / admin …），界面按它分组展示
    category: Mapped[str] = mapped_column(String(32), index=True, default="")
    description: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    roles: Mapped[list[Role]] = relationship(
        secondary="role_permissions", back_populates="permissions"
    )

    def __repr__(self) -> str:
        return f"<Permission {self.code}>"


# ===========================================================================
# 4/5. 关联表
# ===========================================================================
class UserRole(Base):
    """用户 ↔ 角色。复合主键，天然防重。"""

    __tablename__ = "user_roles"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    granted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    #: 谁授的权（权限变更本身就是审计对象）
    granted_by: Mapped[str] = mapped_column(String(64), default="")


class RolePermission(Base):
    """角色 ↔ 权限。"""

    __tablename__ = "role_permissions"

    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission_id: Mapped[int] = mapped_column(
        ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True
    )


# ===========================================================================
# 6. 刷新令牌（会话）
# ===========================================================================
class RefreshToken(Base):
    """刷新令牌。

    **不存原文，只存哈希**。和密码同理：库被读走时，攻击者拿到的是哈希，
    不能直接拿来冒充用户。对比用的是哈希后的定长值，所以可以直接建唯一索引。

    为什么要入库而不是纯无状态 JWT：**要能单方面吊销**。
    员工离职、设备丢了、发现异常登录 —— 这些场景需要一个"立刻踢下线"的能力，
    纯 JWT 只能等它自然过期。企业环境下这是硬需求。
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    #: JWT 的 jti，便于把日志里的 token 和这一行对上（日志不该出现 token 原文）
    jti: Mapped[str] = mapped_column(String(64), index=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    #: 轮换时用来定位"上一个 token"，从而能检测令牌重放
    replaced_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("refresh_tokens.id", ondelete="SET NULL")
    )

    user_agent: Mapped[str] = mapped_column(String(255), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def __repr__(self) -> str:
        return f"<RefreshToken user={self.user_id} revoked={self.revoked_at is not None}>"


# ===========================================================================
# 7. 外部身份（钉钉 / 企业微信 / OIDC）
# ===========================================================================
class ExternalIdentity(Base):
    """第三方登录绑定。

    有了它，用户既能用钉钉扫码登录，也能用本地密码登录 —— 两条路指向**同一个 User**，
    而不是各建一个账号（那样权限要维护两份，离职时容易漏删）。

    唯一键是 ``(provider, external_id)`` 而不是单独的 ``external_id``：
    钉钉和微信的 ID 空间是独立的，理论上可能撞上。
    """

    __tablename__ = "external_identities"
    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_external_identity"),
        Index("ix_external_provider_user", "provider", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    #: dingtalk / wecom / oidc
    provider: Mapped[str] = mapped_column(String(32))
    #: 第三方侧的用户唯一标识
    external_id: Mapped[str] = mapped_column(String(190))
    #: 微信/钉钉的 unionid（同一企业在多个应用间一致），用于跨应用识别同一个人
    union_id: Mapped[str | None] = mapped_column(String(190), index=True)

    display_name: Mapped[str] = mapped_column(String(128), default="")
    #: 第三方返回的原始用户信息。**存下来**是为了排查"为什么这个人没对上" ——
    #: 每次重新拉取既慢，而且第三方可能改字段。
    raw_profile: Mapped[dict | None] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)


# ===========================================================================
# 8. API Key（服务间调用 / 脚本 / 第三方系统对接）
# ===========================================================================
class APIKey(Base):
    """API Key。用于没有交互式登录的场景（定时任务、外部系统回调、CI）。

    同样是**只存哈希**。另外存一个 ``key_prefix`` 明文段，
    好处是界面上能显示 ``fops_ab12****`` 让人认出是哪一把，
    而不用在库里留可用的完整密钥。
    """

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    key_prefix: Mapped[str] = mapped_column(String(32), index=True)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)

    #: 归属用户 —— 权限从该用户的角色推导，不给 API Key 单独一套权限体系
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    owner_username: Mapped[str] = mapped_column(String(64), default="")
    #: 可选的权限收紧（子集）。为空表示"跟 owner 的权限一致"。
    scopes: Mapped[list | None] = mapped_column(JSON)

    expires_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[str] = mapped_column(String(64), default="")

    def __repr__(self) -> str:
        return f"<APIKey {self.key_prefix}...>"


__all__ = [
    "USER_STATUS_ACTIVE",
    "USER_STATUS_DISABLED",
    "USER_STATUS_LOCKED",
    "USER_STATUS_PENDING",
    "APIKey",
    "ExternalIdentity",
    "Permission",
    "RefreshToken",
    "Role",
    "RolePermission",
    "User",
    "UserRole",
]
