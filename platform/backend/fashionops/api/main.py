"""FastAPI 应用入口。

启动::

    uv run uvicorn fashionops.api.main:app --reload --port 8000

文档：<http://127.0.0.1:8000/docs>
健康：<http://127.0.0.1:8000/health>
"""

from __future__ import annotations

import logging
import sys
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError

from fashionops import __version__
from fashionops.api.routers import (
    admin_router,
    auth_router,
    business_router,
    chat_router,
    memory_router,
    service_router,
    system_router,
    tasks_router,
)
from fashionops.config import get_settings
from fashionops.config_doctor import check_config
from fashionops.db import get_engine
from fashionops.db.models import ALL_MODELS
from fashionops.logging_setup import new_request_id, request_context, setup_logging

# Windows 控制台默认 GBK，日志里的中文会抛 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 日志必须在**任何模块打第一条日志之前**配好。放模块顶层而不是 startup 事件里：
# startup 事件在 lifespan 之后才跑，而 import 期就可能已经有日志被丢掉了。
setup_logging()

logger = logging.getLogger("fashionops")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "服装电商多 Agent 运营中台 —— 后端 API。\n\n"
            "阶段 0 全部是只读查询接口，用于让前端有真实数据可渲染。\n"
            "写入侧（提交任务、人工确认、改预算）在阶段 1 接入，统一走工具网关。"
        ),
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # 前端 Vite 开发服务器在 3000，需要放行。
    # 生产环境要把 allow_origins 收窄到真实域名 —— 通配符等于关掉了同源策略保护。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(system_router)
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(business_router)
    app.include_router(tasks_router)
    app.include_router(chat_router)
    app.include_router(memory_router)
    app.include_router(service_router)

    # ------------------------------------------------------------------
    # 请求日志：分配 request_id 并绑定到上下文，记录耗时，慢请求打 warning
    #
    # request_id 绑进 contextvars 之后，**这次请求里任何地方打的日志都会自动带上它** ——
    # 网关、Agent、工具层都不需要把它当参数一路透传。
    # 排查线上问题时，"把一次请求涉及的所有日志捞出来"是最常用的动作。
    #
    # 客户端传了 X-Request-ID 就用它的（便于和上游/网关的链路对上），
    # 没传就生成一个。回写在响应头里，方便前端把它记进 bug 报告。
    #
    # 阶段 3 换成 OpenTelemetry 的 span，这里先用最朴素的方式。
    # ------------------------------------------------------------------
    @app.middleware("http")
    async def timing_middleware(request: Request, call_next):  # noqa: ANN001, ANN202
        request_id = request.headers.get("X-Request-ID") or new_request_id()
        started = time.perf_counter()

        with request_context(request_id, method=request.method, path=request.url.path):
            response = await call_next(request)
            elapsed_ms = (time.perf_counter() - started) * 1000
            response.headers["X-Process-Time-Ms"] = f"{elapsed_ms:.2f}"
            response.headers["X-Request-ID"] = request_id

            # 鉴权被关掉时，在**每一个**响应上打标记。
            #
            # 只在启动日志里说一句“鉴权已关闭”是不够的 —— 没人会去看启动日志。
            # 放进响应头之后，它在浏览器 DevTools 里一眼可见，
            # 开发时顺手看一眼就知道“哦，现在没鉴权”，不会把这种状态带到线上。
            if not get_settings().auth_required:
                response.headers["X-Auth-Disabled"] = "1"

            if elapsed_ms > 500:
                logger.warning(
                    "慢请求 %s %s -> %s 用时 %.0fms",
                    request.method,
                    request.url.path,
                    response.status_code,
                    elapsed_ms,
                )
            return response

    # ------------------------------------------------------------------
    # 数据库异常统一转 503，不要把 SQL 原文和连接串糊到响应里
    # ------------------------------------------------------------------
    @app.exception_handler(SQLAlchemyError)
    async def sqlalchemy_error_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.exception("数据库异常: %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=503,
            content={
                "detail": "数据库暂时不可用，请检查 MySQL / Redis 是否在运行",
                "error_type": type(exc).__name__,
                "hint": "运行 deploy\\scripts\\services.ps1 status 查看依赖状态",
            },
        )

    # ------------------------------------------------------------------
    # 启动自检
    # ------------------------------------------------------------------
    @app.on_event("startup")
    def _startup_check() -> None:
        settings_ = get_settings()
        logger.info("启动 %s v%s (env=%s)", settings_.app_name, __version__, settings_.env)
        logger.info("数据库: %s", settings_.dependency_summary()["database"])

        # 配置体检。拼错的配置项会被 pydantic 静默丢掉（extra="ignore"），
        # 如果不在启动时报出来，就只能等到"邮件怎么发不出去"的时候发现 ——
        # 而那时排查方向会跑到邮件代码上，真正的原因在一个字母里。
        report = check_config(settings_)
        for issue in report.errors:
            logger.error("配置错误 [%s] %s %s", issue.key, issue.message, issue.suggestion)
        for issue in report.warnings:
            logger.warning("配置可疑 [%s] %s %s", issue.key, issue.message, issue.suggestion)
        if report.ok:
            logger.info("配置体检通过（%d 个已知配置项）", report.known_count)

        try:
            # **不传 settings**，走 `get_engine()` 的无参形式。
            #
            # 这里曾经写的是 `get_engine(settings_)`，而 `settings_` 来自
            # `config.get_settings()`。看起来等价（正常运行时两者是同一个对象），
            # 但它制造了**两个 settings 来源**：`db.base` 内部用的是自己那份
            # `get_settings`，而这里显式传进去的是另一份。
            #
            # 一旦两者不一致（测试里 patch 其中一处、多环境配置、将来有人给
            # `get_settings` 加缓存参数），后果是**引擎单例被第一份 settings 占住**，
            # 之后所有请求都连到那个库上去 —— 而表现只是"数据不对"，
            # 不会有任何报错。本次就是这样：测试本以为连的是临时 SQLite，
            # 实际连上了开发用的 MySQL。
            #
            # 一个东西只能有一个获取途径。要换库就换配置，不要绕过它传参。
            existing = set(inspect(get_engine()).get_table_names())
        except Exception as exc:  # noqa: BLE001
            logger.error("连不上数据库：%s", exc)
            logger.error("检查 .env 里的 FASHIONOPS_DB_HOST / DB_PASSWORD，或跑 init-mysql.ps1")
            return

        missing = [m.__tablename__ for m in ALL_MODELS if m.__tablename__ not in existing]
        if missing:
            logger.warning("有 %d 张表还没建：%s", len(missing), ", ".join(missing))
            logger.warning("跑这条命令建表并灌假数据：uv run fashionops-db init && uv run fashionops-seed")
        else:
            logger.info("数据表检查通过（%d 张）", len(ALL_MODELS))

        # ------------------------------------------------------------------
        # 身份域：把代码里的权限码/内置角色同步进库，并确认"至少有一个能登录的账号"
        #
        # 为什么同步放在启动时而不是只放在 `fashionops-db init`：
        # 发版时会新增权限码（比如新加一个工具就要加一个 scope）。
        # 如果只在 init 时同步，那么"升级代码但没重跑 init"就会得到
        # 一个**新接口没人有权限访问**的系统 —— 表现是"功能上线了但所有人都 403"，
        # 而报错信息里不会告诉你"是因为权限码还没进库"。
        # ------------------------------------------------------------------
        _sync_identity_and_warn(settings_, existing)

        # 启动回收：后端重启后，内存里的 _running 集合清空，但数据库里
        # 那些 running 任务还留在"执行中"。不回收的话界面会永远转圈
        # （见 docs/12 §8.6）。表还没建时跳过，别在 init 之前报错。
        if "tasks" in existing and "task_todos" in existing:
            from fashionops.agents.execution import recover_orphaned_tasks

            recover_orphaned_tasks()

        if settings_.model_provider == "mock":
            logger.info("模型提供方 = mock：无需 API Key，将返回结构合法的假响应")

    return app


def _sync_identity_and_warn(settings, existing_tables: set[str]) -> None:  # noqa: ANN001
    """同步权限/角色，并在"没有任何用户"时给出可执行的下一步。

    一个**登不进任何账号**的系统和一个坏掉的系统没有区别。所以这里必须
    明确打印出"跑哪一条命令" —— 而 `docs` 里的说明常常没被读到。
    """
    from sqlalchemy import func, select

    from fashionops.db import session_scope
    from fashionops.db.models_identity import User
    from fashionops.security.permissions import sync_identity
    from fashionops.security.service import ensure_bootstrap_admin

    if "permissions" not in existing_tables or "users" not in existing_tables:
        logger.warning("身份相关的表还没建，跳过权限同步（先跑 fashionops-db init）")
        return

    try:
        with session_scope() as session:
            stats = sync_identity(session)
            created_admin = ensure_bootstrap_admin(session, settings)
            user_count = session.scalar(select(func.count()).select_from(User)) or 0

        perms = stats["permissions"]
        if perms["created"] or perms["removed"]:
            logger.info(
                "权限码已同步：新增 %d / 移除 %d（移除意味着某些接口的权限不再存在，"
                "检查是不是误删了 PERMISSIONS 里的项）",
                perms["created"],
                perms["removed"],
            )
        if stats["roles"]["created"]:
            logger.info("已创建 %d 个内置角色", stats["roles"]["created"])

        # 角色预设的缺口：新加的权限码不会自动进到已有角色里
        # （理由见 `permissions.sync_roles` 的 docstring）。
        # **必须报出来**：不报的话表现是"升级后新功能对所有人 403"，
        # 而报错长得很像"角色配错了"，排查方向会跑到角色表上去。
        role_gap = stats["roles"].get("missing") or {}
        if role_gap:
            logger.warning("=" * 78)
            logger.warning("**内置角色缺少代码里预设的权限，新功能可能对所有人 403。**")
            for code, perms in role_gap.items():
                logger.warning("   %s 缺少：%s", code, ", ".join(perms))
            logger.warning("   补上：.venv\\Scripts\\fashionops-user.exe sync --apply-presets")
            logger.warning("=" * 78)

        if created_admin is not None:
            logger.info("已按配置创建初始管理员 %s", created_admin.username)
            user_count = max(user_count, 1)

        if user_count == 0:
            logger.warning("=" * 78)
            logger.warning("**系统里还没有任何用户，没人能登录。**")
            logger.warning("先建一个管理员（密码会打印在终端，只显示一次）：")
            logger.warning("    .venv\\Scripts\\fashionops-user.exe create-admin admin")
            logger.warning("=" * 78)
        elif not settings.auth_required:
            logger.warning("**FASHIONOPS_AUTH_REQUIRED=false：所有接口都没有鉴权检查**")
            logger.warning("响应头里会带 X-Auth-Disabled: 1。生产环境必须改回 true。")
    except Exception as exc:  # noqa: BLE001
        # 身份同步失败**不能阻止服务启动** —— 已经登录的用户手里有 token，
        # 服务起来了他们还能用；而启动失败是全站不可用。两害相权取其轻。
        logger.error("身份域初始化失败（服务继续启动，但权限可能不完整）：%s", exc)
        logger.exception("详细堆栈")


app = create_app()
