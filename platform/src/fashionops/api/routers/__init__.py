"""API 路由包。"""

from __future__ import annotations

from fashionops.api.routers.business import router as business_router
from fashionops.api.routers.system import router as system_router

__all__ = ["business_router", "system_router"]
