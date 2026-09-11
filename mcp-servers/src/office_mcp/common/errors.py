"""异常类型。

统一异常便于 MCP 层把内部错误转成结构化、对模型友好的提示，
而不是把 Python traceback 直接丢给模型。
"""

from __future__ import annotations


class OfficeMCPError(Exception):
    """本工程所有可预期错误的基类。"""

    #: 给模型看的简短错误码，便于上层做分支处理
    code: str = "office_mcp_error"

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        #: 给模型的修复建议，写进工具返回值里能显著降低重试次数
        self.hint = hint

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"ok": False, "code": self.code, "error": self.message}
        if self.hint:
            payload["hint"] = self.hint
        return payload


class PathViolationError(OfficeMCPError):
    """文件路径越出允许的沙箱根目录。"""

    code = "path_violation"


class FileNotInSandboxError(OfficeMCPError):
    """目标文件在沙箱内不存在。

    与 :class:`PathViolationError` 区分开，模型才能分辨
    「你越权了」和「这个文件不存在」，从而选择正确的下一步动作。
    """

    code = "file_not_found"


class UpstreamUnavailableError(OfficeMCPError):
    """外部依赖（邮箱服务器、日历服务）不可用。"""

    code = "upstream_unavailable"


class UnsupportedFormatError(OfficeMCPError):
    """不支持的文档格式。"""

    code = "unsupported_format"


class DocumentParseError(OfficeMCPError):
    """文档解析失败（文件损坏、加密、结构异常）。"""

    code = "document_parse_error"
