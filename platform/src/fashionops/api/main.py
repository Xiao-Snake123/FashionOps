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
from fashionops.api.routers import business_router, system_router, tasks_router
from fashionops.config import get_settings
from fashionops.db import get_engine
from fashionops.db.models import ALL_MODELS

# Windows 控制台默认 GBK，日志里的中文会抛 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

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

    # 前端 Vite 开发服务器在 5173，需要放行。
    # 生产环境要把 allow_origins 收窄到真实域名 —— 通配符等于关掉了同源策略保护。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(system_router)
    app.include_router(business_router)
    app.include_router(tasks_router)

    # ------------------------------------------------------------------
    # 请求日志：记录耗时，慢请求打 warning
    # 阶段 3 换成 OpenTelemetry 的 span，这里先用最朴素的方式
    # ------------------------------------------------------------------
    @app.middleware("http")
    async def timing_middleware(request: Request, call_next):  # noqa: ANN001, ANN202
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Process-Time-Ms"] = f"{elapsed_ms:.2f}"
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

        try:
            existing = set(inspect(get_engine(settings_)).get_table_names())
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

        if settings_.model_provider == "mock":
            logger.info("模型提供方 = mock：无需 API Key，将返回结构合法的假响应")

    return app


app = create_app()
