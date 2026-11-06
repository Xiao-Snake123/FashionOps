"""API 路由包。

按**资源**划分而不是按"读/写"划分：``business`` 管商品/工单/广告/素材这些
只读查询，``tasks`` 管任务全生命周期（含提交、人工确认、SSE 推流），
``auth`` 管身份（登录/刷新/登出/我的信息/扫码登录适配位），
``chat`` 管对话（会话/消息/对话内审批），
``actions`` 管**会改状态的**业务动作（广告处置、邮件策略与重发）。

一个资源只有一个所有者，避免两个路由都定义 ``/tasks/{no}`` 后
FastAPI 静默取先注册的那个。
"""

from __future__ import annotations

from fashionops.api.routers.actions import router as actions_router
from fashionops.api.routers.admin import router as admin_router
from fashionops.api.routers.auth import router as auth_router
from fashionops.api.routers.business import router as business_router
from fashionops.api.routers.chat import router as chat_router
from fashionops.api.routers.memory import router as memory_router
from fashionops.api.routers.service import router as service_router
from fashionops.api.routers.system import router as system_router
from fashionops.api.routers.tasks import router as tasks_router

__all__ = [
    "actions_router",
    "admin_router",
    "auth_router",
    "business_router",
    "chat_router",
    "memory_router",
    "service_router",
    "system_router",
    "tasks_router",
]
