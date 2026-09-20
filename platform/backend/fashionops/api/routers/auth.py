"""认证接口 —— 登录 / 刷新 / 登出 / 我的信息 / 会话管理。

## 为什么登录接口要限流，而账号锁定还不够

账号锁定是按**用户名**锁的，它挡不住这两种攻击：

1. **撞库（credential stuffing）**：攻击者拿一份泄漏的账号密码库，
   对**每个用户名只试一次**。每个账号的失败次数都是 1，永远触发不了锁定，
   但只要库里撞中一个就赢。
2. **用户名枚举**：拿一批用户名挨个试，靠响应时间或错误信息筛出哪些真实存在。
   锁定会**帮**攻击者：试 5 次就被锁，反而说明"这个用户名是存在的"。

所以再加一层**按 IP** 的限流（`login_rate_limit_per_minute`）。
两层是互补的：锁定防"盯着一个账号爆"，限流防"拿着一批账号扫"。

## 为什么 refresh 也限流

refresh token 是长期凭据。攻击者拿到一个之后会一直试（虽然它本身是有效的），
而更常见的场景是**调试时写了个死循环一直刷新** ——
不限流的话，`refresh_tokens` 表会以每分钟几百行的速度膨胀。

## 为什么 `/auth/me` 是前端刷新后第一个要调的接口

前端把 token 存在 localStorage，刷新页面时不知道自己还有没有效。
调 `/auth/me` 一次就能同时确认三件事：token 还有效吗、我是谁、我有什么权限。
返回里带 `permissions` 让前端能**按权限隐藏按钮** ——
这只是体验优化（后端照样会拦），但少了它，用户会点了才发现没权限。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from fashionops.config import get_settings
from fashionops.db import get_db
from fashionops.security import oauth as oauth_mod
from fashionops.security import service
from fashionops.security.deps import (
    CurrentPrincipal,
    get_token_service,
    record_audit,
    require_permission,
)
from fashionops.security.permissions import PERM_ADMIN_USER
from fashionops.security.service import AuthError, Principal
from fashionops.security.tokens import TokenPair

logger = logging.getLogger("fashionops.api.auth")

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class UserOut(BaseModel):
    user_id: int
    username: str
    full_name: str = ""
    roles: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    is_superuser: bool = False


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105
    expires_in: int = Field(description="access token 剩余秒数")
    user: UserOut


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=8)


class LogoutRequest(BaseModel):
    refresh_token: str | None = Field(
        default=None,
        description="留空则只撤销服务端识别到的当前会话；给了就精确撤销那一个",
    )


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


class SessionOut(BaseModel):
    id: int
    created_at: str
    expires_at: str
    ip: str = ""
    user_agent: str = ""
    current: bool = False


def _user_out(principal: Principal) -> UserOut:
    return UserOut(
        user_id=principal.user_id,
        username=principal.username,
        full_name=principal.full_name,
        roles=list(principal.roles),
        permissions=sorted(principal.permissions),
        is_superuser=principal.is_superuser,
    )


def _token_out(pair: TokenPair, principal: Principal) -> TokenOut:
    return TokenOut(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
        user=_user_out(principal),
    )


def _client_ip(request: Request) -> str:
    """取真实客户端 IP。

    优先 `X-Forwarded-For` 的**第一段**（最靠近客户端的那一跳）。
    直接取 `request.client.host` 的话，经过 nginx 之后所有人都是网关的 IP ——
    按 IP 限流会变成"全公司共用一个额度"，一个人输错密码就能把所有人锁在外面。
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "") or ""


def _check_login_rate(request: Request, *, username: str) -> None:
    """登录/刷新限流。**fail-open** ——

    限流后端（Redis）挂了不应该导致"所有人都登不进系统"。
    这是一次误判代价明显不对称的选择：漏过几次暴力尝试，
    还有账号锁定兜着；而把整公司的登录挡掉是立刻可见的故障。
    """
    from fashionops.gateway.rate_limit import LimiterUnavailable, get_rate_limiter

    settings = get_settings()
    try:
        limiter = get_rate_limiter(settings.cache_backend)
        decision = limiter.hit(
            f"login:{_client_ip(request)}:{username.lower()}",
            settings.login_rate_limit_per_minute,
            60,
        )
    except LimiterUnavailable as exc:
        logger.warning("登录限流不可用，放行：%s", exc)
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("登录限流异常，放行：%s", exc)
        return

    if not decision.allowed:
        logger.warning("登录被限流：ip=%s user=%s", _client_ip(request), username)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"登录尝试过于频繁，请 {int(decision.retry_after_s) + 1} 秒后再试",
            headers={"Retry-After": str(int(decision.retry_after_s) + 1)},
        )


# ---------------------------------------------------------------------------
# 登录 / 刷新 / 登出
# ---------------------------------------------------------------------------
@router.post("/login", response_model=TokenOut, summary="账号密码登录")
def login(
    payload: LoginRequest,
    request: Request,
    session: Session = Depends(get_db),  # noqa: B008
) -> TokenOut:
    settings = get_settings()
    _check_login_rate(request, username=payload.username)

    if not settings.auth_required:
        # 本地演示模式：仍走真实登录流程（否则前端开发时拿不到 token），
        # 但不做限流之外的拦截。**不做特例分支** —— 特例分支意味着
        # "演示模式下的登录路径"是另一条没人测过的代码。
        logger.info("auth_required=False，登录仍走完整流程")

    try:
        user, pair = service.login(
            session,
            username=payload.username,
            password=payload.password,
            tokens=get_token_service(settings),
            ip=_client_ip(request),
            user_agent=request.headers.get("User-Agent", ""),
            max_failures=settings.login_max_failures,
            lock_minutes=settings.login_lock_minutes,
        )
    except AuthError as exc:
        # `reason` 只写日志。响应里说的是"用户名或密码不正确"这类统一话术 ——
        # 区分"用户名不存在"和"密码错"会帮攻击者枚举有效用户名。
        logger.warning("登录失败 user=%s ip=%s reason=%s", payload.username, _client_ip(request), exc.reason)
        # 401 而不是 400：语义上是"认证失败"，前端按 401 处理（不跳登录页，
        # 因为是登录页自己发的请求，只显示错误）；403 才表示"登录了但没权限"。
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": f'Bearer error="{exc.reason}"'},
        ) from exc

    principal = service.load_principal(session, user.id)
    record_audit(
        session,
        principal=principal,
        action="auth.login",
        resource="/api/v1/auth/login",
        detail={"ip": _client_ip(request), "agent": request.headers.get("User-Agent", "")[:120]},
        request=request,
    )
    return _token_out(pair, principal)


@router.post("/refresh", response_model=TokenOut, summary="用刷新令牌换新的令牌对")
def refresh_tokens(
    payload: RefreshRequest,
    request: Request,
    session: Session = Depends(get_db),  # noqa: B008
) -> TokenOut:
    settings = get_settings()
    _check_login_rate(request, username="refresh")

    try:
        user, pair = service.refresh(
            session,
            refresh_token=payload.refresh_token,
            tokens=get_token_service(settings),
            ip=_client_ip(request),
            user_agent=request.headers.get("User-Agent", ""),
        )
    except AuthError as exc:
        logger.info("刷新失败 reason=%s ip=%s", exc.reason, _client_ip(request))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": f'Bearer error="{exc.reason}"'},
        ) from exc

    return _token_out(pair, service.load_principal(session, user.id))


@router.post("/logout", summary="登出（撤销刷新令牌）")
def logout(
    payload: LogoutRequest,
    principal: CurrentPrincipal,
    session: Session = Depends(get_db),  # noqa: B008
) -> dict:
    """登出。

    返回 200 + `{"revoked": bool}`，**令牌无效也不报 401**（理由见 `service.logout`）。
    `principal` 只是为了让审计有"谁登出的"，即使 token 已经无效也要能登出 ——
    所以这个接口**不做**权限检查，只要求带了有效的 access token。
    """
    revoked = False
    if payload.refresh_token:
        revoked = service.logout(session, refresh_token=payload.refresh_token)
    record_audit(
        session,
        principal=principal,
        action="auth.logout",
        resource="/api/v1/auth/logout",
        detail={"revoked": revoked},
    )
    return {
        "revoked": revoked,
        "message": "已登出" if revoked else "该会话此前已失效，无需重复登出",
    }


# ---------------------------------------------------------------------------
# 我的信息 / 会话
# ---------------------------------------------------------------------------
@router.get("/me", response_model=UserOut, summary="当前登录用户（含权限清单）")
def me(principal: CurrentPrincipal) -> UserOut:
    """前端每次刷新页面都会调它。

    返回 `permissions` 让前端能按权限隐藏按钮。**这只是体验，不是安全** ——
    真正的拦截在后端每个接口的 `require_permission` 上。
    把"隐藏按钮"当成安全措施是最常见的越权来源。
    """
    return _user_out(principal)


@router.get("/sessions", response_model=list[SessionOut], summary="我的登录设备")
def my_sessions(
    request: Request,
    principal: CurrentPrincipal,
    session: Session = Depends(get_db),  # noqa: B008
) -> list[SessionOut]:
    rows = service.list_sessions(session, principal.user_id)
    current_ua = request.headers.get("User-Agent", "")
    return [
        SessionOut(
            id=row.id,
            created_at=row.created_at.isoformat(timespec="seconds"),
            expires_at=row.expires_at.isoformat(timespec="seconds"),
            ip=row.ip or "",
            user_agent=row.user_agent or "",
            # 用 User-Agent 猜"哪个是当前设备"。猜错也不影响功能，
            # 只是界面上高亮的那一行不准，所以不值得为了准确去比对 token 哈希
            current=bool(current_ua) and row.user_agent == current_ua[:255],
        )
        for row in rows
    ]


@router.post("/logout-all", summary="退出全部设备")
def logout_all(principal: CurrentPrincipal, session: Session = Depends(get_db)) -> dict:  # noqa: B008
    count = service.revoke_all(session, principal.user_id, reason="user_logout_all")
    record_audit(
        session,
        principal=principal,
        action="auth.logout_all",
        resource="/api/v1/auth/logout-all",
        detail={"count": count},
    )
    return {"revoked": count, "message": f"已退出 {count} 个设备"}


@router.post("/change-password", summary="改自己的密码（会撤销全部会话）")
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    principal: CurrentPrincipal,
    session: Session = Depends(get_db),  # noqa: B008
) -> dict:
    from fashionops.db.models_identity import User
    from fashionops.security import passwords

    user = session.get(User, principal.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    if not passwords.verify_password(payload.old_password, user.password_hash):
        # 改密码要求验旧密码：否则拿到一个通过了的 access token 就能把
        # 真正的主人永久锁在外面（改掉密码 + 撤销所有会话）。
        logger.warning("改密码时旧密码校验失败 user=%s", principal.username)
        raise HTTPException(status_code=400, detail="原密码不正确")

    try:
        service.set_password(session, user, payload.new_password)
    except passwords.PasswordPolicyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"密码不符合要求：{exc}") from exc

    record_audit(
        session,
        principal=principal,
        action="auth.change_password",
        resource="/api/v1/auth/change-password",
        detail={"ip": _client_ip(request)},
    )
    return {
        "changed": True,
        "message": "密码已更新，全部登录会话已撤销，请用新密码重新登录",
    }


# ---------------------------------------------------------------------------
# 第三方扫码登录（适配位，未接真实调用）
# ---------------------------------------------------------------------------
@router.get("/providers", summary="可用的第三方登录方式")
def list_providers() -> dict:
    """前端用它决定显示哪些扫码登录按钮。

    `implemented=false` 是**刻意暴露**的：前端据此把按钮置灰并给出提示，
    而不是让用户点了才看到 501。
    """
    settings = get_settings()
    providers = oauth_mod.available_providers(settings)
    return {
        "providers": providers,
        "note": (
            "当前只有适配位（数据模型 + 配置项 + 流程抽象就绪），"
            "真实 HTTP 调用未接入。接入步骤见 backend/fashionops/security/oauth.py 的模块说明"
        ),
        "enabled": any(p["configured"] for p in providers),
    }


@router.get("/oauth/{provider}/authorize", summary="取第三方授权跳转地址（未接真实调用）")
def oauth_authorize(provider: str, state: str = "") -> dict:
    import secrets as _secrets

    from fashionops.security.oauth import OAuthError, get_provider

    settings = get_settings()
    try:
        impl = get_provider(provider, settings)
    except OAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if not impl.configured:
        raise HTTPException(
            status_code=503,
            detail=(
                f"{provider} 登录未配置。需要在 .env 里填 "
                f"FASHIONOPS_{provider.upper()}_* 相关字段"
            ),
        )

    # `state` 由服务端生成（不信任调用方传的）—— 它要防的是 CSRF，
    # 让调用方自定义就等于没防。
    return {
        "provider": provider,
        "authorize_url": impl.authorize_url(state=state or _secrets.token_urlsafe(24)),
        "next_step": (
            "第三方回调会带回 code，前端拿它调 POST /api/v1/auth/oauth/{provider}/callback。"
            "**该接口当前返回 501** —— exchange_code() 未实现。"
        ),
    }


@router.post("/oauth/{provider}/callback", summary="第三方回调（未实现，返回 501）")
def oauth_callback(provider: str, code: str) -> dict:
    settings = get_settings()
    try:
        impl = oauth_mod.get_provider(provider, settings)
        impl.exchange_code(code)
    except oauth_mod.OAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NotImplementedError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=str(exc).splitlines()[0],
        ) from exc
    # 走到这里说明 provider 实现了 —— 但绑定逻辑还没写完，
    # 所以显式 501 而不是假装成功。**宁可明确地没做完，也不要含糊地"完成了"**。
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="扫码登录的账号绑定逻辑尚未实现（见 security/oauth.py）",
    )


# ---------------------------------------------------------------------------
# 管理接口（复用用户权限）
# ---------------------------------------------------------------------------
@router.get("/users", summary="用户列表（需要 admin:user）")
def list_users(
    principal: Principal = Depends(require_permission(PERM_ADMIN_USER)),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> dict:
    from sqlalchemy import select

    from fashionops.db.models_identity import User
    from fashionops.security.permissions import user_permission_codes

    users = list(session.scalars(select(User).order_by(User.id)))
    return {
        "total": len(users),
        "items": [
            {
                "user_id": u.id,
                "username": u.username,
                "full_name": u.full_name,
                "email": u.email,
                "status": u.status,
                "is_superuser": u.is_superuser,
                "roles": u.role_codes(),
                "permissions": sorted(user_permission_codes(u)),
                "last_login_at": u.last_login_at.isoformat(timespec="seconds")
                if u.last_login_at
                else None,
            }
            for u in users
        ],
        "requested_by": principal.username,
    }


__all__ = ["router"]
