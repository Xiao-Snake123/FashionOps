"""外部系统集成层。

目前只有 `office`：把 `mcp-servers/` 里的办公 MCP 服务器接入平台工具网关。
后续接别的外部系统（如真实 ERP / 广告平台适配器）也放这里。
"""
from __future__ import annotations

from fashionops.integrations import office

__all__ = ["office"]
