"""办公文档读写核心。

支持格式：``.docx`` / ``.xlsx`` / ``.pptx`` / ``.md`` / ``.txt`` / ``.csv`` / ``.json``

设计要点：
- **统一读取入口** :func:`read_document` 返回 Markdown 文本，便于直接喂给模型。
- **结构化写入** —— 用 Pydantic 模型描述内容，MCP 层可据此自动生成 JSON Schema。
- 所有文件操作都走路径沙箱，且写入采用「先写临时文件再替换」，避免半成品文件。
"""

from __future__ import annotations

import csv
import json
import re
import tempfile
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.shared import Pt
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from pptx import Presentation
from pptx.util import Inches
from pptx.util import Pt as PptPt
from pydantic import BaseModel, Field

from office_mcp.common.errors import (
    DocumentParseError,
    UnsupportedFormatError,
)
from office_mcp.common.paths import resolve_in_root, safe_filename

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".csv", ".json", ".log", ".yaml", ".yml"}
DOCX_SUFFIX = ".docx"
XLSX_SUFFIXES = {".xlsx", ".xlsm"}
PPTX_SUFFIX = ".pptx"
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | {DOCX_SUFFIX, PPTX_SUFFIX} | XLSX_SUFFIXES


# --------------------------------------------------------------------------- #
# 数据模型：写入用的结构化描述
# --------------------------------------------------------------------------- #


class DocxBlock(BaseModel):
    """Word 文档中的一个内容块。"""

    kind: Literal["heading", "paragraph", "bullets", "numbered", "table", "quote", "page_break"] = (
        Field(description="块类型")
    )
    text: str | None = Field(default=None, description="heading/paragraph/quote 的文本")
    level: int | None = Field(default=None, ge=1, le=6, description="heading 层级，1-6")
    items: list[str] | None = Field(default=None, description="bullets/numbered 的条目")
    rows: list[list[str]] | None = Field(
        default=None, description="table 的行列数据，第一行作为表头"
    )


class SheetSpec(BaseModel):
    """Excel 中的一个工作表。"""

    name: str = Field(description="工作表名")
    rows: list[list[Any]] = Field(default_factory=list, description="二维数据，第一行为表头")
    header: bool = Field(default=True, description="是否把第一行视为表头并加粗")


class SlideSpec(BaseModel):
    """PPT 中的一页。"""

    title: str = Field(description="页标题")
    bullets: list[str] = Field(default_factory=list, description="要点列表")
    notes: str | None = Field(default=None, description="演讲者备注")


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #


def list_files(
    root: Path,
    *,
    pattern: str = "*",
    recursive: bool = True,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """列出工作区内的文件（不含目录）。"""
    globber = root.rglob if recursive else root.glob
    results: list[dict[str, Any]] = []
    for item in sorted(globber(pattern)):
        if not item.is_file():
            continue
        stat = item.stat()
        results.append(
            {
                "path": str(item.relative_to(root)).replace("\\", "/"),
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "suffix": item.suffix.lower(),
            }
        )
        if len(results) >= limit:
            break
    return results


def read_document(path: Path) -> str:
    """统一读取入口，返回 Markdown 文本。"""
    suffix = path.suffix.lower()
    if not path.exists():
        raise DocumentParseError(f"文件不存在：{path}")
    if suffix == DOCX_SUFFIX:
        return _read_docx(path)
    if suffix in XLSX_SUFFIXES:
        return _read_xlsx(path)
    if suffix == PPTX_SUFFIX:
        return _read_pptx(path)
    if suffix in TEXT_SUFFIXES:
        return _read_text(path)
    raise UnsupportedFormatError(
        f"不支持的格式：{suffix or '(无扩展名)'}",
        hint=f"当前支持：{', '.join(sorted(SUPPORTED_SUFFIXES))}",
    )


def _read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            text = path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
        if path.suffix.lower() == ".csv":
            return _csv_to_markdown(text)
        if path.suffix.lower() == ".json":
            return _json_to_markdown(text)
        return text
    raise DocumentParseError(
        f"无法解码文件：{path.name}",
        hint="请确认文件是 UTF-8 或 GBK 编码的文本。",
    )


def _csv_to_markdown(raw: str) -> str:
    rows = list(csv.reader(raw.splitlines()))
    if not rows:
        return ""
    return _rows_to_markdown_table(rows)


def _json_to_markdown(raw: str) -> str:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    return f"```json\n{json.dumps(data, ensure_ascii=False, indent=2)}\n```"


def _read_docx(path: Path) -> str:
    doc = Document(str(path))
    lines: list[str] = []
    body = doc.element.body
    para_map = {p._element: p for p in doc.paragraphs}
    table_map = {t._element: t for t in doc.tables}

    for child in body.iterchildren():
        if child in para_map:
            para = para_map[child]
            text = para.text.strip()
            if not text:
                continue
            style = (para.style.name or "").lower()
            if style.startswith("heading"):
                level = _heading_level(style)
                lines.append(f"{'#' * level} {text}")
            elif "list" in style:
                lines.append(f"- {text}")
            else:
                lines.append(text)
            lines.append("")
        elif child in table_map:
            table = table_map[child]
            rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
            lines.append(_rows_to_markdown_table(rows))
            lines.append("")
    return "\n".join(lines).strip()


def _heading_level(style_name: str) -> int:
    match = re.search(r"(\d+)", style_name)
    return int(match.group(1)) if match else 1


def _read_xlsx(path: Path) -> str:
    wb = load_workbook(str(path), data_only=True, read_only=True)
    chunks: list[str] = []
    try:
        for sheet in wb.worksheets:
            rows = [
                ["" if cell is None else str(cell) for cell in row]
                for row in sheet.iter_rows(values_only=True)
            ]
            rows = [row for row in rows if any(cell.strip() for cell in row)]
            chunks.append(f"## 工作表：{sheet.title}")
            chunks.append("")
            chunks.append(_rows_to_markdown_table(rows[:200]) if rows else "_（空工作表）_")
            if len(rows) > 200:
                chunks.append(f"\n> 仅显示前 200 行，共 {len(rows)} 行。")
            chunks.append("")
    finally:
        wb.close()
    return "\n".join(chunks).strip()


def _read_pptx(path: Path) -> str:
    prs = Presentation(str(path))
    chunks: list[str] = []
    for index, slide in enumerate(prs.slides, start=1):
        chunks.append(f"## 第 {index} 页")
        chunks.append("")
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    text = "".join(run.text for run in para.runs).strip()
                    if text:
                        chunks.append(f"- {text}")
            if getattr(shape, "has_table", False) and shape.has_table:
                rows = [[cell.text.strip() for cell in row.cells] for row in shape.table.rows]
                chunks.append("")
                chunks.append(_rows_to_markdown_table(rows))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            chunks.append("")
            chunks.append(f"> 备注：{slide.notes_slide.notes_text_frame.text.strip()}")
        chunks.append("")
    return "\n".join(chunks).strip()


def _rows_to_markdown_table(rows: Sequence[Sequence[Any]]) -> str:
    if not rows:
        return ""
    width = max(len(r) for r in rows)

    def _clean(cell: Any) -> str:
        return str(cell).replace("|", "\\|").replace("\n", " ")

    normalized = [[_clean(cell) for cell in row] for row in rows]
    normalized = [r + [""] * (width - len(r)) for r in normalized]
    header = normalized[0]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines.extend("| " + " | ".join(r) + " |" for r in normalized[1:])
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #


def create_docx(path: Path, title: str, blocks: Iterable[DocxBlock]) -> dict[str, Any]:
    """按结构化块创建 Word 文档。"""
    doc = Document()
    doc.core_properties.title = title

    heading = doc.add_heading(title, level=0)
    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER

    table_count = 0
    for block in blocks:
        if block.kind == "heading":
            doc.add_heading(block.text or "", level=min(block.level or 1, 4))
        elif block.kind == "paragraph":
            doc.add_paragraph(block.text or "")
        elif block.kind == "quote":
            para = doc.add_paragraph(block.text or "")
            if "Quote" in {style.name for style in doc.styles}:
                para.style = doc.styles["Quote"]
        elif block.kind == "bullets":
            for item in block.items or []:
                doc.add_paragraph(item, style="List Bullet")
        elif block.kind == "numbered":
            for item in block.items or []:
                doc.add_paragraph(item, style="List Number")
        elif block.kind == "page_break":
            doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        elif block.kind == "table" and block.rows:
            rows = block.rows
            table = doc.add_table(rows=len(rows), cols=max(len(r) for r in rows))
            table.style = "Light Grid Accent 1"
            for r_idx, row in enumerate(rows):
                for c_idx, value in enumerate(row):
                    cell = table.cell(r_idx, c_idx)
                    cell.text = str(value)
                    for para in cell.paragraphs:
                        for run in para.runs:
                            run.font.size = Pt(9)
                            if r_idx == 0:
                                run.font.bold = True
            table_count += 1
            doc.add_paragraph()

    _atomic_write(path, lambda tmp: doc.save(str(tmp)))
    return {
        "path": str(path),
        "title": title,
        "blocks": len(list(blocks)) if isinstance(blocks, list) else None,
        "tables": table_count,
    }


def create_xlsx(path: Path, sheets: Sequence[SheetSpec]) -> dict[str, Any]:
    """创建 Excel 工作簿。"""
    wb = Workbook()
    wb.remove(wb.active)

    for spec in sheets:
        ws = wb.create_sheet(title=safe_filename(spec.name)[:31] or "Sheet1")
        for row in spec.rows:
            ws.append(row)
        if spec.header and spec.rows:
            for cell in ws[1]:
                cell.font = Font(bold=True)
                cell.fill = PatternFill("solid", start_color="DDEBF7")
                cell.alignment = Alignment(horizontal="center", vertical="center")
            ws.freeze_panes = "A2"
        _autosize(ws)
        ws.auto_filter.ref = ws.dimensions

    if not wb.worksheets:
        wb.create_sheet(title="Sheet1")

    _atomic_write(path, lambda tmp: wb.save(str(tmp)))
    return {
        "path": str(path),
        "sheets": [ws.title for ws in wb.worksheets],
        "rows": [len(spec.rows) for spec in sheets],
    }


def _autosize(ws: Any, max_width: int = 48) -> None:
    for column_cells in ws.columns:
        length = max((len(str(c.value)) for c in column_cells if c.value is not None), default=0)
        letter = get_column_letter(column_cells[0].column)
        ws.column_dimensions[letter].width = min(max(10, length + 2), max_width)


def create_pptx(path: Path, title: str, slides: Sequence[SlideSpec]) -> dict[str, Any]:
    """创建演示文稿（16:9）。"""
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    # 封面
    cover = prs.slides.add_slide(prs.slide_layouts[0])
    cover.shapes.title.text = title
    if len(cover.placeholders) > 1:
        cover.placeholders[1].text = date.today().isoformat()

    total_bullets = 0
    for spec in slides:
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = spec.title
        body = slide.placeholders[1].text_frame
        body.clear()
        for index, bullet in enumerate(spec.bullets):
            para = body.paragraphs[0] if index == 0 else body.add_paragraph()
            para.text = bullet
            para.font.size = PptPt(18)
            total_bullets += 1
        if spec.notes:
            slide.notes_slide.notes_text_frame.text = spec.notes

    _atomic_write(path, lambda tmp: prs.save(str(tmp)))
    return {
        "path": str(path),
        "title": title,
        "slide_count": len(prs.slides),
        "bullets": total_bullets,
    }


def write_text(path: Path, content: str, *, overwrite: bool = True) -> dict[str, Any]:
    """写文本文件（md/txt/csv/json）。"""
    if path.exists() and not overwrite:
        raise DocumentParseError(
            f"文件已存在：{path.name}",
            hint="如需覆盖请把 overwrite 设为 true，或换一个文件名。",
        )
    _atomic_write(path, lambda tmp: tmp.write_text(content, encoding="utf-8"))
    return {
        "path": str(path),
        "chars": len(content),
        "lines": content.count("\n") + 1,
    }


def _atomic_write(target: Path, writer: Any) -> None:
    """先写同目录临时文件再原子替换，避免中断时留下损坏的半成品。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=target.suffix)
    tmp = Path(tmp_name)
    try:
        import os

        os.close(fd)
        writer(tmp)
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# 便捷函数
# --------------------------------------------------------------------------- #


def relative(root: Path, path: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


def resolve(root: Path, path: str, *, must_exist: bool = True) -> Path:
    return resolve_in_root(path, root, must_exist=must_exist)
