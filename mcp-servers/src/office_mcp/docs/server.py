"""文档处理 MCP 服务器。

对外暴露的工具（供 Agent 通过 MCP 调用）：

============================  ==========================================
工具                          作用
============================  ==========================================
``list_files``                列出工作区文件
``read_document``             读文档 → Markdown（docx/xlsx/pptx/md/txt/csv/json）
``create_word_document``      按结构化块生成 Word
``create_excel_workbook``     生成多工作表 Excel
``create_presentation``       生成 PPT（16:9）
``write_text_file``           写 md/txt/csv/json
``get_workspace_info``        返回工作区与能力信息（不含任何凭证）
============================  ==========================================

运行::

    python -m office_mcp.docs.server            # stdio（默认，供 MCP 客户端拉起）
    OFFICE_MCP_TRANSPORT=http python -m office_mcp.docs.server   # HTTP
"""

from __future__ import annotations

import functools
import logging
import os
from collections.abc import Callable
from typing import Any, TypeVar

from fastmcp import FastMCP

from office_mcp.common import (
    OfficeMCPError,
    get_audit_logger,
    get_secret_store,
    get_settings,
)
from office_mcp.common.paths import safe_filename
from office_mcp.docs import office

logger = logging.getLogger("office_mcp.docs")

mcp = FastMCP(
    name="office-docs",
    instructions=(
        "办公文档处理服务器。可以读取与生成 Word/Excel/PowerPoint/Markdown 文件。"
        "所有文件操作都限制在配置的工作区目录内。"
        "生成文档时优先使用结构化工具（create_word_document / create_excel_workbook / "
        "create_presentation）而不是手写 XML。"
    ),
)

F = TypeVar("F", bound=Callable[..., Any])


def _guard(tool_name: str) -> Callable[[F], F]:
    """统一处理审计、异常转换与返回值包装。

    - 审计：记录工具名、参数（自动脱敏）、耗时、状态
    - 异常：``OfficeMCPError`` 转成带 ``hint`` 的结构化错误，其他异常兜底
    - 返回值：统一为 ``{"ok": true, ...}`` 形态，模型更容易判断成功与否
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(**kwargs: Any) -> dict[str, Any]:
            audit = get_audit_logger()
            with audit.tool_call(tool_name, **kwargs) as ctx:
                try:
                    result = func(**kwargs)
                except OfficeMCPError as exc:
                    # 业务异常已被转成模型友好的返回值，但审计里必须仍是 error，
                    # 否则监控面板会把“参数错误”当成“调用成功”。
                    ctx["status"] = "error"
                    ctx["error_type"] = type(exc).__name__
                    ctx["error_code"] = exc.code
                    logger.info("%s 业务失败: %s", tool_name, exc.message)
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


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


@mcp.tool
@_guard("list_files")
def list_files(pattern: str = "*", recursive: bool = True, limit: int = 200) -> dict[str, Any]:
    """列出工作区内的文件。

    Args:
        pattern: 通配符，例如 ``*.docx``、``report/*``。
        recursive: 是否递归子目录。
        limit: 最多返回多少条。
    """
    settings = get_settings()
    files = office.list_files(
        settings.workspace_root, pattern=pattern, recursive=recursive, limit=limit
    )
    return {"root": str(settings.workspace_root), "count": len(files), "files": files}


@mcp.tool
@_guard("read_document")
def read_document(path: str) -> dict[str, Any]:
    """读取文档并转成 Markdown 文本。

    支持 .docx / .xlsx / .xlsm / .pptx / .md / .txt / .csv / .json。

    Args:
        path: 相对工作区的路径，例如 ``uploads/周报.xlsx``。
    """
    settings = get_settings()
    target = office.resolve(settings.workspace_root, path, must_exist=True)
    size = target.stat().st_size
    if size > settings.max_read_bytes:
        size_mb = size / 1024 / 1024
        limit_mb = settings.max_read_bytes / 1024 / 1024
        raise OfficeMCPError(
            f"文件过大（{size_mb:.1f} MB），超过 {limit_mb:.0f} MB 上限。",
            hint="请指定具体工作表或文件名读取，或让用户先拆分文件。",
        )
    content = office.read_document(target)
    return {
        "path": office.relative(settings.workspace_root, target),
        "suffix": target.suffix.lower(),
        "size_bytes": size,
        "chars": len(content),
        "markdown": content,
    }


@mcp.tool
@_guard("create_word_document")
def create_word_document(
    filename: str,
    title: str,
    blocks: list[office.DocxBlock],
    overwrite: bool = True,
) -> dict[str, Any]:
    """生成 Word 文档（.docx）。

    ``blocks`` 是一个内容块列表，支持的 kind：
    ``heading``（配 level）、``paragraph``、``bullets``（配 items）、
    ``numbered``（配 items）、``table``（配 rows，第一行为表头）、
    ``quote``、``page_break``。

    Args:
        filename: 输出文件名，如 ``周报.docx``。
        title: 文档标题（会作为居中大标题）。
        blocks: 内容块列表。
        overwrite: 同名文件是否覆盖。
    """
    settings = get_settings()
    target = _output_path(settings.workspace_root, filename, ".docx", overwrite)
    if settings.dry_run:
        return {"dry_run": True, "would_create": str(target), "blocks": len(blocks)}
    info = office.create_docx(target, title, blocks)
    return {**info, "path": office.relative(settings.workspace_root, target)}


@mcp.tool
@_guard("create_excel_workbook")
def create_excel_workbook(
    filename: str,
    sheets: list[office.SheetSpec],
    overwrite: bool = True,
) -> dict[str, Any]:
    """生成 Excel 工作簿（.xlsx），支持多个工作表。

    Args:
        filename: 输出文件名，如 ``数据分析.xlsx``。
        sheets: 工作表列表，每项含 name、rows（二维数组，第一行为表头）、header。
        overwrite: 同名文件是否覆盖。
    """
    settings = get_settings()
    target = _output_path(settings.workspace_root, filename, ".xlsx", overwrite)
    if settings.dry_run:
        return {"dry_run": True, "would_create": str(target), "sheets": len(sheets)}
    info = office.create_xlsx(target, sheets)
    return {**info, "path": office.relative(settings.workspace_root, target)}


@mcp.tool
@_guard("create_presentation")
def create_presentation(
    filename: str,
    title: str,
    slides: list[office.SlideSpec],
    overwrite: bool = True,
) -> dict[str, Any]:
    """生成 PowerPoint 演示文稿（.pptx，16:9 宽屏）。

    Args:
        filename: 输出文件名，如 ``方案汇报.pptx``。
        title: 封面标题。
        slides: 幻灯片列表，每项含 title、bullets（要点）、notes（备注，可选）。
        overwrite: 同名文件是否覆盖。
    """
    settings = get_settings()
    target = _output_path(settings.workspace_root, filename, ".pptx", overwrite)
    if settings.dry_run:
        return {"dry_run": True, "would_create": str(target), "slides": len(slides)}
    info = office.create_pptx(target, title, slides)
    return {**info, "path": office.relative(settings.workspace_root, target)}


@mcp.tool
@_guard("write_text_file")
def write_text_file(filename: str, content: str, overwrite: bool = True) -> dict[str, Any]:
    """写文本文件（.md / .txt / .csv / .json）。

    Args:
        filename: 输出文件名。
        content: 文件内容。
        overwrite: 同名文件是否覆盖。
    """
    settings = get_settings()
    target = _output_path(settings.workspace_root, filename, ".md", overwrite)
    if settings.dry_run:
        return {"dry_run": True, "would_create": str(target), "chars": len(content)}
    info = office.write_text(target, content, overwrite=overwrite)
    return {**info, "path": office.relative(settings.workspace_root, target)}


@mcp.tool
@_guard("get_workspace_info")
def get_workspace_info() -> dict[str, Any]:
    """返回工作区信息与当前可用能力（不含任何凭证内容）。"""
    settings = get_settings()
    return {
        "workspace_root": str(settings.workspace_root),
        "dry_run": settings.dry_run,
        "max_read_bytes": settings.max_read_bytes,
        "supported_formats": sorted(office.SUPPORTED_SUFFIXES),
        "audit_enabled": settings.audit_log is not None,
        "secrets_configured": get_secret_store().describe(),
    }


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _output_path(root: Any, filename: str, default_ext: str, overwrite: bool) -> Any:
    """把用户给的文件名规范化后放进 ``outputs/`` 目录。"""
    from pathlib import Path

    from office_mcp.common.errors import DocumentParseError

    name = safe_filename(filename, default_ext=default_ext)
    target: Path = Path(root) / "outputs" / name
    if target.exists() and not overwrite:
        raise DocumentParseError(
            f"文件已存在：outputs/{name}",
            hint="请换一个文件名，或把 overwrite 设为 true。",
        )
    return target


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("OFFICE_MCP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    transport = os.environ.get("OFFICE_MCP_TRANSPORT", "stdio").lower()
    if transport in {"http", "streamable-http", "sse"}:
        mcp.run(
            transport="http",
            host=os.environ.get("OFFICE_MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("OFFICE_MCP_PORT", "8765")),
        )
    else:
        mcp.run()


if __name__ == "__main__":
    main()
