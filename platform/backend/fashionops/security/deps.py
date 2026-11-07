"""FastAPI 鉴权依赖 —— 路由层唯一需要 import 的鉴权入口。

## 用法

```python
from fashionops.security.deps import CurrentPrincipal, require_permission
from fashionops.security.permissions import PERM_TASK_APPROVE

@router.post("/tasks/{no}/approve")
def approve(
    no: str,
    principal: Principal = Depends(require_permission(PERM_TASK_APPROVE)),
): ...
```

## 为什么是"在函数签名里显式写出来"而不是中间件统一拦

中间件按**路径**匹配来放行，而权限要按**动作**判断。
`/tasks/{no}/approve` 和 `/tasks/{no}` 前缀相同但一个是写一个是读，
按前缀匹配必然要么过宽要么过窄。更麻烦的是，中间件写在别处，
**review 这个接口的人看不到它需要什么权限** ——
而后端接口的权限问题，恰恰是 review 时最该看见的东西。

写成依赖之后，权限要求就长在路由上，和参数校验并排。
`tests/test_security.py` 里有一条测试**遍历全部路由**，
要求每个非白名单路由都声明了权限 —— 漏掉一个会被测试挡下来，
而不是等上线后被人发现。

## 未登录时的行为

`auth_required=False`（本地纯演示）时，任何请求都拿到 `SYSTEM_PRINCIPAL`，
并在响应头里打 `X-Auth-Disabled: 1`。
**这个头很重要**：让"忘了开鉴权"这件事在浏览器 DevTools 里一眼可见，
而不是要靠读配置才知道。
"""

from __future__ import annotations

import logging
import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from fashionops.config import Settings, get_settings
from fashionops.db import get_db
from fashionops.security import service
from fashionops.security.service import NOBODY, SYSTEM_PRINCIPAL, AuthError, Principal
from fashionops.security.tokens import TokenError, TokenService

logger = logging.getLogger("fashionops.security.deps")

#: 生成的临时密钥在进程内缓存，保证"同一次运行里签发的 token 能验过"。
#: 不缓存的话每次调用 `get_token_service()` 都生成新密钥，
#: 登录拿到的 token 下一个请求就验不过 —— 表现是"登录成功但立刻 401"。
_EPHEMERAL_SECRET: str | None = None

#: 审计写入失败次数。正常应该恒为 0；不为 0 说明 `audit_logs` 表结构不对
#: 或库出了别的毛病 —— 而审计是旁路，它坏掉时不会有任何用户可见的症状。
#: 所以必须有一个能被健康检查读到的计数器。见 `record_audit`。
AUDIT_WRITE_FAILURES = 0


def get_token_service(settings: Settings | None = None) -> TokenService:
    """按配置造 TokenService。

    `jwt_secret` 留空时**生成一个临时密钥并打 warning**，而不是拒绝启动。
    理由和 `extra="ignore"` 一样：漏配必须导致"能起来但不安全"，
    不能导致"服务起不来" —— 后者会让开发者在慌乱中把安全配置整个关掉。

    但临时密钥的后果要说清楚：**重启后所有 token 失效**，
    且多进程部署时各进程密钥不同（token 互相验不过）。
    """
    global _EPHEMERAL_SECRET  # noqa: PLW0603

    settings = settings or get_settings()
    secret = (settings.jwt_secret or "").strip()
    if not secret:
        if _EPHEMERAL_SECRET is None:
            _EPHEMERAL_SECRET = secrets.token_urlsafe(48)
            logger.warning(
                "FASHIONOPS_JWT_SECRET 未配置，已生成临时密钥。"
                "**重启后所有登录状态会失效**，多进程部署时各进程互不接受对方的 token。"
                "生产环境必须显式配置："
                'python -c "import secrets;print(secrets.token_urlsafe(48))"'
            )
        secret = _EPHEMERAL_SECRET

    return TokenService(
        secret=secret,
        algorithm=settings.jwt_algorithm,
        access_minutes=settings.access_token_minutes,
        refresh_days=settings.refresh_token_days,
        issuer=settings.app_name or "fashionops",
    )


def reset_ephemeral_secret() -> None:
    """仅供测试：清掉缓存的临时密钥。"""
    global _EPHEMERAL_SECRET  # noqa: PLW0603
    _EPHEMERAL_SECRET = None


# ---------------------------------------------------------------------------
# 解析 Authorization 头
# ---------------------------------------------------------------------------
def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        # 不是 Bearer 就当作没有凭据，而不是报"格式错" ——
        # 报格式错会泄漏"这个接口需要 Bearer 认证"，对未认证的探测者没必要给这个信息
        return None
    token = parts[1].strip()
    return token or None


def _unauthorized(detail: str, *, reason: str) -> HTTPException:
    """401 响应带上 `WWW-Authenticate`。

    前端靠它判断"要跳登录页"还是"要显示无权限"：
    401 = 没登录（跳登录），403 = 登录了但没权限（显示提示）。
    两者混用会让用户看到"请重新登录"，然后重新登录后还是不行。
    """
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": f'Bearer error="{reason}"'},
    )


def get_principal(
    request: Request,
    session: Annotated[Session, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """解析当前请求主体。**不做权限判断**，只负责"你是谁"。

    分开的理由：有些接口只需要"知道是谁"（比如记录操作人），
    不需要"必须有什么权限"。合成一个依赖会逼着这些地方也要挂一个权限码，
    而随便挑一个权限码比显式声明"不需要权限"更糟。
    """
    settings = get_settings()
    token = _extract_bearer(authorization)

    if token is None:
        if not settings.auth_required:
            # 开发模式：把开关状态显式放进 request.state，供响应头中间件读取
            request.state.auth_disabled = True
            return SYSTEM_PRINCIPAL
        raise _unauthorized("请先登录", reason="missing_token")

    try:
        user_id, _jti = get_token_service(settings).decode_access(token)
    except TokenError as exc:
        logger.info("访问令牌无效：%s", exc.reason)
        # 过期和伪造给同一句话。区分了对攻击者有用、对用户没用。
        if exc.reason == "expired":
            raise _unauthorized("登录已过期，请重新登录", reason="expired") from exc
        raise _unauthorized("登录状态无效，请重新登录", reason="invalid_token") from exc

    try:
        return service.load_principal(session, user_id)
    except AuthError as exc:
        logger.info("加载主体失败（user_id=%s）：%s", user_id, exc.reason)
        raise _unauthorized(str(exc), reason=exc.reason) from exc


#: 便捷别名：只要"当前登录用户"，不要求具体权限
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def get_stream_principal(
    request: Request,
    session: Annotated[Session, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
    token: Annotated[
        str | None,
        Query(
            description=(
                "仅用于 SSE 的令牌传递方式。浏览器的原生 EventSource **不支持自定义请求头**，所以只能从查询串带令牌"
            )
        ),
    ] = None,
) -> Principal:
    """SSE 专用：允许从查询串 `?token=` 拿令牌。

    ## 为什么必须开这个口子

    浏览器原生的 `EventSource` **不能设置 `Authorization` 头**（这是 Web 标准
    的限制，不是浏览器 bug）。所以 SSE 端点只有三条路：

    1. 用查询串带令牌 ← **选这个**
    2. 让前端改用 fetch + ReadableStream 手工实现 SSE ——
       代价是断线重连、`Last-Event-ID` 续传、心跳全部要自己写，
       而这恰恰是当初选原生 `EventSource` 的原因
    3. SSE 不做鉴权 —— 绝对不行，事件流里含任务内容和错误详情

    ## 这个口子的代价，以及怎么控制

    **URL 会进很多地方**：浏览器历史、Referer 头、nginx 的 access log、
    各种 APM 的 URL 采样。令牌进 URL 就是进这些地方。

    控制手段（三条一起才够）：
    - 只用 **access token**，绝不用 refresh token —— 它 30 分钟就过期，
      泄漏的窗口有限，而且 refresh 那条能"长期续命"的路没有被打开
    - 这个依赖**只在 SSE 端点上挂**，其他接口一律只认 `Authorization` 头 ——
      避免"URL 带令牌"变成一个到处都能用的通用方式
    - `logging_setup` 里对 `/stream` 路径的查询串做了脱敏（见 `_redact_query`）

    这是权衡，不是"最佳实践"。如果将来前端换成了 fetch + ReadableStream，
    **应该把这个函数删掉**，而不是留着"以防万一"。
    """
    raw = _extract_bearer(authorization) or (token or "").strip()
    if not raw:
        settings = get_settings()
        if not settings.auth_required:
            request.state.auth_disabled = True
            return SYSTEM_PRINCIPAL
        raise _unauthorized("请先登录", reason="missing_token")

    try:
        user_id, _jti = get_token_service(get_settings()).decode_access(raw)
    except TokenError as exc:
        logger.info("SSE 令牌无效：%s", exc.reason)
        raise _unauthorized("登录状态无效，请重新登录", reason=exc.reason) from exc

    try:
        return service.load_principal(session, user_id)
    except AuthError as exc:
        raise _unauthorized(str(exc), reason=exc.reason) from exc


#: SSE 专用别名
StreamPrincipal = Annotated[Principal, Depends(get_stream_principal)]


def require_permission(*codes: str):
    """生成一个"必须拥有其中任意一个权限"的依赖。

    **OR 语义**，和工具网关的 `ToolSpec.scopes` 保持一致。
    权限码写多个的典型场景是"改和删任一即可"。

    `codes` 为空时报错而不是放行 —— 空参数几乎一定是误用
    （比如把枚举拼成了字符串变量结果传了个空元组），
    静默放行会让一个笔误变成权限漏洞。
    """
    if not codes:
        raise ValueError(
            "require_permission() 至少需要一个权限码。不需要权限的接口请用 CurrentPrincipal，不要用这个函数"
        )

    def dependency(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        if not principal.has(*codes):
            logger.warning(
                "权限不足：user=%s 需要 %s 之一，实际拥有 %s",
                principal.username or "(匿名)",
                " / ".join(codes),
                " / ".join(sorted(principal.permissions)) or "（无）",
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"当前账号（{principal.display or '匿名'}）没有执行该操作的权限。"
                    f"需要以下任一权限：{' / '.join(codes)}"
                ),
            )
        return principal

    # 打一个标记，供 `tests/test_security.py` 的"路由覆盖检查"读取。
    #
    # 这条测试遍历**全部路由**，要求每个非白名单路由都声明了权限 ——
    # 靠人 review 是挡不住的：新增一个接口时"忘了加鉴权"和"故意不加"
    # 在 diff 里长得一模一样，而前者是漏洞。
    # 有了这个标记，漏掉的路由会在 CI 上直接变成红色。
    dependency.__fashionops_permissions__ = tuple(codes)  # type: ignore[attr-defined]
    return dependency


def require_superuser():
    """只有超级管理员能用。用于危险运维接口（清库、改密钥）。"""

    def dependency(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        if not principal.is_superuser:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="该操作仅限超级管理员",
            )
        return principal

    return dependency


def record_audit(
    session: Session,
    *,
    principal: Principal,
    action: str,
    resource: str = "",
    tool_name: str = "",
    detail: dict | None = None,
    status: str = "success",
    task_id: int | None = None,
    request: Request | None = None,
) -> None:
    """写一条接口级审计。

    **和网关审计是两个层次，都要有**：
    - 网关审计回答"Agent 调了哪些工具"
    - 接口审计回答"哪个人点了哪个按钮"

    只有前者的话，"这封邮件是谁批准的"答不上来 —— 而 HITL 闸口存在的
    全部意义就是这个问题的答案。

    复用 `audit_logs` 表而不是另建一张：查询时经常要"把某个任务的
    工具调用和人工操作按时间排在一起看"，分成两张表就得做 UNION，
    而字段本来就高度重合。`tool_name` 字段对接口审计填 `api:<路由>`，
    这样两种来源在同一个列表里也能区分。

    ## 写失败**不能**中断业务调用

    这是从 `mcp-servers` 带过来的原则：审计是**旁路**，
    它坏掉不应该让用户的操作失败。但只管吞异常会掩盖自己的 bug
    （"记录失败不影响业务"的经典反噬），所以**同时维护一个计数器**
    `AUDIT_WRITE_FAILURES`，健康检查可以读它。
    任何 fail-safe 都必须配一个可观测的信号，否则它迟早掩护一个真缺陷。
    """
    from fashionops.db.models import AuditLog

    global AUDIT_WRITE_FAILURES  # noqa: PLW0603

    try:
        entry = AuditLog(
            tool_name=(tool_name or f"api:{resource}")[:128],
            action=action,
            resource=resource[:255] if resource else None,
            task_id=task_id,
            # `actor` 是"哪个执行体"，网关那条链路填 Agent 名；这里填接口调用方
            actor=(principal.username or "system")[:64],
            actor_user_id=principal.user_id if principal.user_id > 0 else None,
            # 快照：用户被删后外键会置空，而审计记录必须保持"当时发生了什么"
            actor_username=(principal.username or "system")[:64],
            args_digest=detail or None,
            status=status,
            ip=(request.client.host if request and request.client else None),
        )
        session.add(entry)
        session.flush()
    except Exception as exc:  # noqa: BLE001
        AUDIT_WRITE_FAILURES += 1
        logger.warning(
            "写审计失败（不影响业务，累计 %d 次）：action=%s resource=%s err=%s",
            AUDIT_WRITE_FAILURES,
            action,
            resource,
            exc,
        )
        # 会话可能已经处于失败状态（比如 flush 报了约束冲突），
        # 不清理的话后续操作全部会拿到 PendingRollbackError
        import contextlib

        with contextlib.suppress(Exception):
            session.rollback()


__all__ = [
    "CurrentPrincipal",
    "NOBODY",
    "StreamPrincipal",
    "get_principal",
    "get_stream_principal",
    "get_token_service",
    "record_audit",
    "require_permission",
    "require_superuser",
    "reset_ephemeral_secret",
]
