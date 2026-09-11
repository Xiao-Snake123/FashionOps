"""横切能力：配置 / 审计 / 路径沙箱 / 凭证。"""

from office_mcp.common.audit import AuditLogger, get_audit_logger
from office_mcp.common.config import Settings, get_settings
from office_mcp.common.errors import (
    DocumentParseError,
    FileNotInSandboxError,
    OfficeMCPError,
    PathViolationError,
    UnsupportedFormatError,
    UpstreamUnavailableError,
)
from office_mcp.common.paths import resolve_in_root, safe_filename
from office_mcp.common.secrets import SecretStore, get_secret_store

__all__ = [
    "AuditLogger",
    "DocumentParseError",
    "FileNotInSandboxError",
    "OfficeMCPError",
    "PathViolationError",
    "SecretStore",
    "Settings",
    "UnsupportedFormatError",
    "UpstreamUnavailableError",
    "get_audit_logger",
    "get_secret_store",
    "get_settings",
    "resolve_in_root",
    "safe_filename",
]
