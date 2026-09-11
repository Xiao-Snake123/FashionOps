"""凭证管理。

核心约束：**凭证的值永不进入模型上下文。**

做法：
1. 凭证只从环境变量 / 本地密钥文件读取，不出现在任何工具参数里。
2. 工具需要用凭证时，通过 :class:`SecretStore` 取用，返回的是
   :class:`SecretLease`（带名字与作用域），审计日志只记录凭证**名字**。
3. :meth:`SecretStore.describe` 只返回"是否已配置"，可用于向模型说明能力边界，
   而不泄露任何值。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from office_mcp.common.config import Settings, get_settings
from office_mcp.common.errors import OfficeMCPError


@dataclass(frozen=True)
class SecretLease:
    """一次凭证使用的凭据句柄。``value`` 不会被写进审计日志或工具返回值。"""

    name: str
    scope: str
    value: str

    def __repr__(self) -> str:  # pragma: no cover - 防止误打印
        return f"SecretLease(name={self.name!r}, scope={self.scope!r}, value='***')"

    __str__ = __repr__


class SecretStore:
    """从环境变量装载凭证。

    命名约定：``OFFICE_MCP_SECRET_<NAME>``，例如
    ``OFFICE_MCP_SECRET_OA_TOKEN``。
    """

    ENV_PREFIX = "OFFICE_MCP_SECRET_"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._cache: dict[str, str] = {}

    # ---- 查询 ----

    def has(self, name: str) -> bool:
        """凭证是否已配置。此方法可安全暴露给模型。"""
        return bool(self._lookup(name))

    def describe(self) -> dict[str, bool]:
        """返回所有已知凭证的"是否已配置"清单，不含任何值。"""
        known = {
            "smtp": self._settings.mail_configured,
            "caldav": self._settings.calendar_configured,
        }
        for key in os.environ:
            if key.startswith(self.ENV_PREFIX):
                known[key[len(self.ENV_PREFIX) :].lower()] = True
        return known

    # ---- 取用 ----

    def lease(self, name: str, *, scope: str) -> SecretLease:
        """取用一个凭证。取不到时抛错，错误信息只说名字不说值。"""
        value = self._lookup(name)
        if not value:
            raise OfficeMCPError(
                f"凭证 {name!r} 未配置。",
                hint=(
                    f"请设置环境变量 {self.ENV_PREFIX}{name.upper()}，"
                    "或在 .env 中配置对应的连接参数。"
                ),
            )
        return SecretLease(name=name, scope=scope, value=value)

    # ---- 内部 ----

    def _lookup(self, name: str) -> str | None:
        if name in self._cache:
            return self._cache[name]

        upper = name.upper()
        value: str | None = None

        # 1) 通用密钥环境变量
        value = os.environ.get(f"{self.ENV_PREFIX}{upper}")

        # 2) 已知连接参数的别名
        if value is None:
            aliases = {
                "mail_password": self._settings.mail_password,
                "smtp_password": self._settings.mail_password,
                "caldav_password": self._settings.caldav_password,
            }
            value = aliases.get(name.lower())

        if value:
            self._cache[name] = value
        return value


@lru_cache(maxsize=1)
def get_secret_store() -> SecretStore:
    return SecretStore()
