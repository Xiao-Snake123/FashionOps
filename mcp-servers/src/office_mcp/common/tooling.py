"""MCP 工具层的公共装饰器。

把「审计 + 异常转换 + 返回值包装」这三件每个工具都要做的事收敛到一处，
避免每个 server 各写一遍、各漏一点。

统一返回值契约：

- 成功：``{"ok": true, ...业务字段}``
- 失败：``{"ok": false, "code": "...", "error": "...", "hint": "..."}``

``hint`` 是**给模型的下一步动作建议**，不是给人看的日志。实测补上 hint 后，
模型面对 ``file_not_found`` 会先去 ``list_files``，而不是换个路径继续瞎试。
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any, TypeVar

from office_mcp.common.audit import get_audit_logger
from office_mcp.common.errors import OfficeMCPError

logger = logging.getLogger("office_mcp.tooling")

F = TypeVar("F", bound=Callable[..., Any])


def guard_tool(tool_name: str) -> Callable[[F], F]:
    """包装一个 MCP 工具函数。

    - **审计**：记录工具名、参数（自动脱敏）、耗时、状态；业务异常也记为 ``error``，
      否则监控面板会把「参数错误」统计成「调用成功」。
    - **异常转换**：``OfficeMCPError`` 转成带 ``hint`` 的结构化错误；
      其他异常兜底成 ``internal_error``，不让 traceback 泄漏进模型上下文。
    - **返回包装**：统一加 ``ok`` 字段。
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(**kwargs: Any) -> dict[str, Any]:
            audit = get_audit_logger()
            with audit.tool_call(tool_name, **kwargs) as ctx:
                try:
                    result = func(**kwargs)
                except OfficeMCPError as exc:
                    ctx["status"] = "error"
                    ctx["error_type"] = type(exc).__name__
                    ctx["error_code"] = exc.code
                    logger.info("%s 业务失败[%s]: %s", tool_name, exc.code, exc.message)
                    return exc.to_payload()
                except Exception as exc:  # noqa: BLE001 - 兜底，避免 traceback 泄漏给模型
                    ctx["status"] = "error"
                    ctx["error_type"] = type(exc).__name__
                    ctx["error_code"] = "internal_error"
                    logger.exception("%s 未预期错误", tool_name)
                    return {
                        "ok": False,
                        "code": "internal_error",
                        "error": f"{type(exc).__name__}: {exc}",
                        "hint": "这是服务端内部错误，请换一种方式重试或简化输入。",
                    }

                ctx["status"] = "ok"
                if isinstance(result, dict):
                    return {"ok": True, **result}
                return {"ok": True, "result": result}

        return wrapper  # type: ignore[return-value]

    return decorator
