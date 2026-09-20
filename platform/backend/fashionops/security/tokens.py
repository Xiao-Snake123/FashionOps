"""JWT 签发与校验。

## Access Token 和 Refresh Token 的分工

| | Access Token | Refresh Token |
| --- | --- | --- |
| 形态 | JWT，**无状态** | JWT + **入库哈希** |
| 有效期 | 30 分钟 | 14 天 |
| 能否撤销 | **不能** | 能（改库里的 `revoked_at`） |
| 放在哪 | 前端内存 / `Authorization` 头 | 前端 `localStorage`（或 httpOnly Cookie） |
| 用途 | 每次请求带 | 只在换新 access token 时用 |

为什么不全用无状态：**企业环境必须能"立刻踢下线"** —— 员工离职、手机丢了、
发现异常登录。纯无状态 JWT 只能等它自然过期，而这个窗口对上面几种场景来说太长了。

为什么不全用有状态：每次请求查一次库，把认证变成数据库的强依赖。
数据库抖动时所有人被登出，这个代价不值得。

**折中**：access token 短（30 分钟）到"万一泄漏损失有限"，
refresh token 长但入库、可撤销、且每次刷新都轮换（rotation）。

## 刷新令牌轮换与重放检测

每次 refresh 都签发**新的** refresh token，并把旧的标记为"已被替换"
（`replaced_by_id` 指向新的那条）。

如果某天收到一个**已经被替换过**的 refresh token，说明它被泄漏了 ——
攻击者拿到的是旧的那份，而合法用户已经换过新的。这时候正确的动作是
**把这整条链全部撤销**（`revoke_family`），逼用户重新登录。
只拒绝这一次请求是不够的：攻击者仍然持有那个 token，会一直重试。

## 为什么把角色/权限写进 token

写进去就不用每次查库拿权限。但**权限是会变的** —— 管理员刚把某人的审批权
撤了，他手里的 token 还带着 `task:approve`，要 30 分钟后才失效。

这个窗口是接受的吗？**取决于风险**。审批发邮件这种动作，窗口太长不能接受。
所以本实现**只在 token 里放身份（user_id / username），权限每次请求现算**
（`service.load_principal`）。代价是一次带缓存的库查询，收益是"撤权立即生效"。

`LEAN_TOKEN` 这个名字就是提醒：token 里只放**不会变**的东西。
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from fashionops.db.base import utcnow

#: token 用途。放在 claims 里防止"拿 refresh token 当 access token 用" ——
#: 两者签名一样，不区分的话 refresh token（14 天）就能直接访问业务接口，
#: 等于把短有效期的设计意图完全绕过。
TOKEN_TYPE_ACCESS = "access"  # noqa: S105
TOKEN_TYPE_REFRESH = "refresh"  # noqa: S105

#: 允许的时钟漂移。服务器之间（或容器与宿主）时间不可能完全一致，
#: 留 30 秒容忍度，否则刚签发的 token 可能在另一端被判"还没生效"。
LEEWAY_S = 30


def to_epoch(moment: datetime) -> int:
    """把时间转成 epoch 秒。**朴素时间一律按 UTC 解释。**

    ## 这里曾经有一个"一签发就过期"的 bug

    `db.base.utcnow()` 返回的是**朴素** datetime（`tzinfo=None`），
    但它表示的是 UTC。而 `datetime.timestamp()` 对朴素时间的规则是
    **按本地时区解释** —— 所以在东八区（UTC+8）的机器上：

        朴素 UTC 时间 T  →  timestamp() 得到  epoch(T) - 8h

    于是 `exp` 比真实时间早 8 小时，JWT 库判"已过期"。
    **表现是登录接口返回 200 拿到 token，下一个请求立刻 401 "登录已过期"。**

    在 UTC 时区的机器 / 容器上这个 bug 完全不可见 —— 差 0 小时。
    本地 Windows 开发（东八区）才会稳定复现。

    反过来 `datetime.fromtimestamp()` 也有同样的问题（相反方向）。
    所以本模块**所有**时间与 epoch 的互转都必须走这两个函数，
    不能在任何地方直接调 `.timestamp()` / `fromtimestamp()`。
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp())


def from_epoch(seconds: float) -> datetime:
    """epoch → **朴素 UTC** datetime。

    返回朴素值是为了和 `db.base.utcnow()` 保持一致 ——
    库里的 `DateTime` 列存的就是朴素 UTC。混用带时区和不带时区的
    datetime 做比较会直接抛 `TypeError`，而那往往发生在一个不常走的分支里。
    """
    return datetime.fromtimestamp(seconds, tz=UTC).replace(tzinfo=None)


class TokenError(Exception):
    """令牌无效。消息**不区分**具体原因给最终用户 ——

    "签名错" / "过期了" / "用户不存在" 这些信息对攻击者有用（可以确认 token 格式对不对），
    对合法用户没用（他只能重新登录）。所以对外统一一句话，具体原因只写日志。
    """

    def __init__(self, message: str = "登录状态无效，请重新登录", *, reason: str = "") -> None:
        super().__init__(message)
        #: 内部诊断用，**不要返回给客户端**
        self.reason = reason


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime
    #: refresh token 在库里的行号。调用方拿它做"轮换时指向下一条"
    refresh_token_id: int | None = None

    @property
    def expires_in(self) -> int:
        """access token 剩余秒数。OAuth2 的 `expires_in` 字段要这个。"""
        return max(0, int((self.access_expires_at - utcnow()).total_seconds()))


@dataclass(frozen=True)
class RefreshClaims:
    """从 refresh token 里解出来的东西。"""

    user_id: int
    jti: str
    expires_at: datetime
    raw: str


class TokenService:
    """签发/校验。把 secret 和有效期包进来，避免到处传参。

    **不缓存实例**：secret 可能来自热更新的配置，缓存会让"改了密钥但还在用旧的"
    变成一个很难发现的坑。构造开销可以忽略。
    """

    def __init__(
        self,
        *,
        secret: str,
        algorithm: str = "HS256",
        access_minutes: int = 30,
        refresh_days: int = 14,
        issuer: str = "fashionops",
    ) -> None:
        if not secret:
            raise ValueError("JWT secret 不能为空")
        self._secret = secret
        self._algorithm = algorithm
        self._access_delta = timedelta(minutes=access_minutes)
        self._refresh_delta = timedelta(days=refresh_days)
        self._issuer = issuer

    # ------------------------------------------------------------------
    # 签发
    # ------------------------------------------------------------------
    def _encode(
        self,
        *,
        user_id: int,
        username: str,
        token_type: str,
        expires_at: datetime,
        jti: str,
    ) -> str:
        now = utcnow()
        payload = {
            "sub": str(user_id),
            "username": username,
            "type": token_type,
            "jti": jti,
            "iat": to_epoch(now),
            "nbf": to_epoch(now),
            "exp": to_epoch(expires_at),
            "iss": self._issuer,
        }
        return jwt.encode(payload, self._secret, algorithm=self._algorithm)

    def create_pair(self, *, user_id: int, username: str) -> TokenPair:
        """同时签发 access 和 refresh。

        两个 token 的 `jti` **必须不同**。相同的 jti 会让"按 jti 撤销"
        变成"撤销 access 顺带把 refresh 也撤了"，而调用方完全看不出来。
        """
        now = utcnow()
        access_exp = now + self._access_delta
        refresh_exp = now + self._refresh_delta
        access_jti = uuid.uuid4().hex
        refresh_jti = uuid.uuid4().hex

        return TokenPair(
            access_token=self._encode(
                user_id=user_id,
                username=username,
                token_type=TOKEN_TYPE_ACCESS,
                expires_at=access_exp,
                jti=access_jti,
            ),
            refresh_token=self._encode(
                user_id=user_id,
                username=username,
                token_type=TOKEN_TYPE_REFRESH,
                expires_at=refresh_exp,
                jti=refresh_jti,
            ),
            access_expires_at=access_exp,
            refresh_expires_at=refresh_exp,
        )

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    def decode(self, token: str, *, expected_type: str) -> dict:
        """解出 claims。任何问题都转成 `TokenError`。"""
        try:
            claims = jwt.decode(
                token,
                self._secret,
                # 只允许配置的算法。**不要用 `jwt.decode(..., algorithms=None)`**：
                # 那会接受 token 里声明的 `alg`，而攻击者可以把 `alg` 改成 `none`
                # 或者改成非对称算法来用公钥当 HMAC 密钥（算法混淆攻击）。
                algorithms=[self._algorithm],
                issuer=self._issuer,
                leeway=LEEWAY_S,
                options={"require": ["exp", "iat", "sub", "jti"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenError(reason="expired") from exc
        except jwt.InvalidIssuerError as exc:
            raise TokenError(reason="bad_issuer") from exc
        except jwt.InvalidTokenError as exc:
            raise TokenError(reason=f"invalid:{type(exc).__name__}") from exc

        actual_type = claims.get("type")
        if actual_type != expected_type:
            # 这条是防"拿 refresh token 直接访问业务接口"。必须显式检查，
            # 因为两者的签名和 issuer 完全一样，不检查就区分不出来。
            raise TokenError(reason=f"wrong_type:{actual_type}")

        return claims

    def decode_access(self, token: str) -> tuple[int, str]:
        """返回 `(user_id, jti)`。"""
        claims = self.decode(token, expected_type=TOKEN_TYPE_ACCESS)
        try:
            return int(claims["sub"]), str(claims["jti"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TokenError(reason="bad_subject") from exc

    def decode_refresh(self, token: str) -> RefreshClaims:
        claims = self.decode(token, expected_type=TOKEN_TYPE_REFRESH)
        try:
            return RefreshClaims(
                user_id=int(claims["sub"]),
                jti=str(claims["jti"]),
                expires_at=from_epoch(int(claims["exp"])),
                raw=token,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TokenError(reason="bad_subject") from exc


# ---------------------------------------------------------------------------
# refresh token 的入库形式
# ---------------------------------------------------------------------------
def hash_token(token: str) -> str:
    """令牌哈希。**用 SHA-256 而不是 argon2**，这是刻意的。

    密码用 argon2 是因为**密码熵低**（能被字典攻击），所以要慢。
    而这里输入的 token 是 200+ 字符的高熵随机 JWT —— 攻击者拿不到原文时
    唯一的办法就是暴力枚举 2^(8*200)，任何 KDF 都只是让本来不可能的事更慢。

    反过来，如果这里用 argon2：每次刷新（每 30 分钟一次 × 全部在线用户）
    都要跑一次 100ms 的哈希，认证接口会立刻成为瓶颈。
    更要命的是，**按哈希值查库**就做不到了（argon2 每次盐不同），
    只能全表扫描逐行 verify。

    SHA-256 是定长的、确定的，可以直接建唯一索引按值查。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_csrf_token() -> str:
    """生成 CSRF token（如果将来把 refresh token 放进 Cookie 时需要）。"""
    return secrets.token_urlsafe(32)


__all__ = [
    "LEEWAY_S",
    "TOKEN_TYPE_ACCESS",
    "TOKEN_TYPE_REFRESH",
    "RefreshClaims",
    "TokenError",
    "TokenPair",
    "TokenService",
    "from_epoch",
    "hash_token",
    "new_csrf_token",
    "to_epoch",
]
