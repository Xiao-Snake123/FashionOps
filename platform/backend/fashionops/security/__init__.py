"""身份与访问控制（认证 + 授权）。

```python
from fashionops.security import hash_password, verify_password, TokenService
from fashionops.security.deps import CurrentPrincipal, require_permission
from fashionops.security.permissions import PERM_TASK_APPROVE
```

## 这个包的分层

```
permissions.py  权限码与角色定义（代码即事实源）+ 同步进库
     ↑
passwords.py    argon2id 哈希与强度校验
tokens.py       JWT 签发/校验（access 无状态 + refresh 入库可撤销）
oauth.py        钉钉/企微扫码登录的适配位（未接真实调用）
     ↑
service.py      业务逻辑：登录、刷新轮换、重放检测、权限加载、权限向下传导
     ↑
deps.py         FastAPI 依赖：get_principal / require_permission / 接口审计
     ↑
cli.py          fashionops-user 命令行
```

上层只依赖下层的**接口**，不跳过层去直接改表。
`service.py` 是唯一的写入点，所以"登录失败计数""改密码撤销全部会话"
这类跨字段的规则只需要在一个地方保证。

## 三条贯穿全包的原则

1. **权限码在代码、角色在数据**（见 `permissions.py`）
2. **对外话术不区分原因，日志里区分到很细**（见 `service.py`）
3. **漏配导致"进不来"，不能导致"谁都能进"** ——
   所有安全开关的默认值都选更严的那个
"""

from fashionops.security.passwords import (
    PasswordPolicyError,
    generate_password,
    hash_password,
    needs_rehash,
    validate_strength,
    verify_password,
)
from fashionops.security.permissions import (
    ALL_PERMISSIONS,
    PERMISSION_CODES,
    PERMISSIONS,
    ROLE_ADMIN,
    ROLE_ANALYST,
    ROLE_CODES,
    ROLE_OPERATOR,
    ROLE_OPS_MANAGER,
    ROLE_PRESETS,
    ROLE_VIEWER,
    PermissionDef,
    RoleDef,
    sync_identity,
    sync_permissions,
    sync_roles,
    user_permission_codes,
)
from fashionops.security.service import (
    NOBODY,
    SYSTEM_PRINCIPAL,
    AuthError,
    Principal,
    authenticate,
    create_user,
    effective_scopes,
    ensure_bootstrap_admin,
    load_principal,
    login,
    logout,
    refresh,
    revoke_all,
    set_password,
)
from fashionops.security.tokens import (
    TokenError,
    TokenPair,
    TokenService,
    hash_token,
)

__all__ = [
    "ALL_PERMISSIONS",
    "NOBODY",
    "PERMISSIONS",
    "PERMISSION_CODES",
    "ROLE_ADMIN",
    "ROLE_ANALYST",
    "ROLE_CODES",
    "ROLE_OPERATOR",
    "ROLE_OPS_MANAGER",
    "ROLE_PRESETS",
    "ROLE_VIEWER",
    "SYSTEM_PRINCIPAL",
    "AuthError",
    "PasswordPolicyError",
    "PermissionDef",
    "Principal",
    "RoleDef",
    "TokenError",
    "TokenPair",
    "TokenService",
    "authenticate",
    "create_user",
    "effective_scopes",
    "ensure_bootstrap_admin",
    "generate_password",
    "hash_password",
    "hash_token",
    "load_principal",
    "login",
    "logout",
    "needs_rehash",
    "refresh",
    "revoke_all",
    "set_password",
    "sync_identity",
    "sync_permissions",
    "sync_roles",
    "user_permission_codes",
    "validate_strength",
    "verify_password",
]
