"""API 路由包。

按**资源**划分而不是按"读/写"划分：``business`` 管商品/工单/广告/素材这些
只读查询，``tasks`` 管任务全生命周期（含提交、人工确认、SSE 推流）。
一个资源只有一个所有者，避免两个路由都定义 ``/tasks/{no}`` 后
FastAPI 静默取先注册的那个。
"""

from __future__ import annotations

from fashionops.api.routers.business import router as business_router
from fashionops.api.routers.system import router as system_router
from fashionops.api.routers.tasks import router as tasks_router

__all__ = ["business_router", "system_router", "tasks_router"]
