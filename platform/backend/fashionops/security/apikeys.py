"""API Key 的签发与校验。

## 为什么现在才写它

`api_keys` 表从身份域建好就一直在，但**零使用**：没有签发接口，也没有校验路径。
它等的其实是一个"没有 HTTP 头可以放凭据"的入口 —— MCP 的 stdio 就是这么个地方
（客户端把服务端当子进程拉起，凭据只能通过环境变量传）。

所以它是 MCP 的第一个真实消费者，而不是又一个"以后可能用得上"的表。

## 两个和 refresh token 一样的原则

1. **库里只存哈希**。拿到库也换不出可用的 key。所以明文只在签发时返回一次，
   和 `create_user` 自动生成密码一样 —— 之后谁都取不回来。
2. **校验时顺手更新 `last_used_at`**。一把长期不用的 key 是安全隐患，
   而"最后使用时间"是判断它该不该被清理的唯一依据。
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from fashionops.db.base import utcnow
from fashionops.db.models_identity import APIKey, Role, User
from fashionops.security.tokens import hash_token

logger = logging.getLogger("fashionops.security.apikeys")

#: 明文 key 的前缀。给界面和日志用 —— 让人能认出"这是哪一把"，
#: 又不用把完整密钥留在库里（和 `APIKey.key_prefix` 的注释同一套思路）。
KEY_PREFIX = "fops_"

#: 环境变量名。**写在这一层而不是 MCP 层**：变量名是安全约定，
#: 用户管理 CLI（`fashionops-user apikey-create`）要打印它，
#: 而那条命令不该因为"没装 MCP 依赖"就跑不了。
ENV_API_KEY = "FASHIONOPS_MCP_API_KEY"

#: 随机部分的长度（字节）。token_urlsafe(32) ≈ 43 个字符，远超爆破成本。
_SECRET_BYTES = 32


def issue(
    session: Session,
    *,
    name: str,
    owner_id: int | None,
    owner_username: str,
    scopes: list[str] | None = None,
    expires_days: int | None = None,
    created_by: str = "",
) -> tuple[str, APIKey]:
    """签发一把 key。返回 ``(明文, 行)`` —— **明文只在这里出现一次**。"""
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    raw = f"{KEY_PREFIX}{secret}"

    row = APIKey(
        name=name.strip() or "未命名",
        key_prefix=raw[: len(KEY_PREFIX) + 6],
        key_hash=hash_token(raw),
        owner_id=owner_id,
        owner_username=owner_username,
        scopes=list(scopes) if scopes else None,
        expires_at=(utcnow() + timedelta(days=expires_days)) if expires_days else None,
        created_by=created_by,
    )
    session.add(row)
    session.flush()
    logger.info("已签发 API Key：%s（owner=%s）", row.name, owner_username)
    return raw, row


def verify(session: Session, raw: str | None) -> tuple[APIKey | None, str]:
    """校验一把 key。返回 ``(行, 原因)`` —— 原因**给人看**。

    和登录那条链路一样：不区分"key 不存在"和"key 被撤销"之外的更多细节，
    但**必须区分**这几种 —— 因为它们的处置方式完全不同：

    | 情况 | 用户该做什么 |
    | --- | --- |
    | 没配置 | 去配一个 |
    | 无效 | 换一把 |
    | 已撤销 | 找管理员重新签发 |
    | 已过期 | 重新签发 |

    全归成一句"认证失败"的话，排查只能靠猜。
    """
    if not raw or not raw.strip():
        return None, "没有提供 API Key"

    row = session.scalar(select(APIKey).where(APIKey.key_hash == hash_token(raw.strip())))
    if row is None:
        return None, "API Key 无效"
    if row.revoked_at is not None:
        return None, "API Key 已被撤销"
    if row.expires_at is not None and row.expires_at <= utcnow():
        return None, f"API Key 已于 {row.expires_at.isoformat(timespec='seconds')} 过期"

    row.last_used_at = utcnow()
    session.flush()
    return row, ""


def revoke(session: Session, key_prefix: str) -> bool:
    """撤销一把 key（按前缀定位 —— 明文已经不可恢复了）。"""
    row = session.scalar(select(APIKey).where(APIKey.key_prefix == key_prefix))
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = utcnow()
    session.flush()
    logger.info("已撤销 API Key：%s", key_prefix)
    return True


def list_keys(session: Session) -> list[APIKey]:
    return list(session.scalars(select(APIKey).order_by(APIKey.id.desc())))


def load_owner(session: Session, key: APIKey) -> User | None:
    """取 key 的归属用户，**并把角色与权限一并带回来**。

    预加载的理由和 `service.load_principal` 一致：后面要算权限集合，
    懒加载会在这一步产生 N+1；更糟的是在某些 session 状态下会算成空集，
    而空集意味着"这个人什么都做不了" —— 表现为莫名其妙的拒绝。
    """
    if key.owner_id is None:
        return None
    return session.scalar(
        select(User).options(selectinload(User.roles).selectinload(Role.permissions)).where(User.id == key.owner_id)
    )


__all__ = ["ENV_API_KEY", "KEY_PREFIX", "issue", "list_keys", "load_owner", "revoke", "verify"]
