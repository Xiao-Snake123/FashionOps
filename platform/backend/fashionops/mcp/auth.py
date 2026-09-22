"""MCP 的身份解析 —— 一把 API Key 能用到哪些工具。

## stdio 没有 HTTP 头，所以凭据只能走环境变量

MCP 客户端把服务端当**子进程**拉起（`python -m fashionops.mcp.server`），
没有请求头可放 token。所以凭据从环境变量读：`FASHIONOPS_MCP_API_KEY`。

这决定了它天然是**长期凭据**（不像 access token 30 分钟过期），所以必须支持
撤销和过期 —— 这也是为什么它绑到 `api_keys` 表而不是复用 JWT。

## 授权范围怎么算

```
用户自己的权限（角色 → 权限码） ∩ key.scopes（可空 = 不额外收紧）
```

和 `service.effective_scopes()` 同一个思路：**最终拿到的永远是调用方自己就有的
能力的子集**。一把 key 不可能让持有者获得他本人没有的权限 ——
否则"用一把 key 提权"就成了绕过 RBAC 的合法通道。

`scopes` 的作用只是**收窄**（比如给外部系统一把只能读库存的 key），
不是放大。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from sqlalchemy.orm import Session

from fashionops.db.models_identity import USER_STATUS_ACTIVE
from fashionops.security import apikeys
from fashionops.security.apikeys import ENV_API_KEY  # noqa: F401 - 对外重新导出，方便调用方只 import 一处
from fashionops.security.permissions import user_permission_codes

#: 审计里记的调用方前缀，让人一眼看出"这不是人点的，是外部系统调的"。
ACTOR_PREFIX = "mcp"


@dataclass(frozen=True)
class McpIdentity:
    """一次 MCP 会话的身份。"""

    #: 写进审计的调用方标识
    actor: str
    username: str
    #: 网关作用域集合（和权限码共用同一套词表，见 `permissions.py` 的模块说明）
    scopes: frozenset[str]
    key_name: str


def resolve(session: Session, raw_key: str | None = None) -> tuple[McpIdentity | None, str]:
    """解析凭据。返回 ``(身份, 原因)`` —— 失败时身份为 None、原因给人看。

    ## 为什么失败不抛异常

    MCP 服务器是被客户端拉起的子进程，抛异常退出只会在客户端显示一个
    语焉不详的 "server disconnected"。返回结构化的原因，客户端才能把
    "没配 key" 和 "key 过期" 分开显示 —— 这两种情况的处置完全不同。
    """
    raw = raw_key if raw_key is not None else os.environ.get(ENV_API_KEY, "")

    row, reason = apikeys.verify(session, raw)
    if row is None:
        return None, reason

    owner = apikeys.load_owner(session, row)
    if owner is None:
        # owner 被删了（外键 SET NULL）。这把 key 必须失效 ——
        # 否则它变成一个"不属于任何人"的幽灵凭据，撤销都找不到人负责
        return None, "API Key 的归属账号已不存在，这把 key 已失效"

    if owner.status != USER_STATUS_ACTIVE:
        return None, f"归属账号已被停用（status={owner.status}）"

    granted = user_permission_codes(owner)
    if row.scopes:
        # 只收窄，不放大
        granted = granted & frozenset(str(s) for s in row.scopes)

    return (
        McpIdentity(
            actor=f"{ACTOR_PREFIX}:{owner.username}",
            username=owner.username,
            scopes=granted,
            key_name=row.name,
        ),
        "",
    )


__all__ = ["ACTOR_PREFIX", "ENV_API_KEY", "McpIdentity", "resolve"]
