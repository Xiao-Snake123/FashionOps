"""报表工具：把结构化数据渲染成 Excel 并登记为素材。

这个工具做四件事，缺一不可：

1. 渲染 xlsx（``reporting.build_workbook``）
2. 落到对象存储（走 ``StorageBackend`` 抽象，将来可换 MinIO）
3. **登记到 ``assets`` 表** —— 不登记的话，前端拿不到素材列表，
   "产出可见"这个目标就落空了
4. 返回可访问的 URL，让 Agent 能把它写进邮件正文

## 为什么要限制行数

报表是 Agent 产出的，行数由上游数据决定。如果上游某次查询返回了 50 万行，
生成 Excel 会吃掉大量内存，而且生成出来的表也没人看。
所以硬性截断并**在返回里显式告知被截断了** ——
静默截断比报错更糟，运营会以为看到的是全量数据。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from fashionops.db import session_scope
from fashionops.db.models import Asset
from fashionops.gateway.registry import Scope, ToolRegistry
from fashionops.reporting import Column, Sheet, build_workbook
from fashionops.storage import get_storage

#: 单个 sheet 最多渲染多少行。超出部分丢弃并上报
MAX_ROWS_PER_SHEET = 5000
#: 报表文件大小上限（字节）。超过就说明数据量不适合用 Excel 交付
MAX_REPORT_BYTES = 20 * 1024 * 1024


class ColumnSpec(BaseModel):
    key: str = Field(description="对应 rows 里的字段名")
    title: str = Field(description="表头显示的文字")
    number_format: str | None = Field(
        default=None,
        description='Excel 数字格式，如 "#,##0.00"（两位小数）/"0.0%"（百分比）。金额和比例一定要填',
    )
    align: Literal["left", "center", "right"] = "left"


class SheetSpec(BaseModel):
    name: str = Field(description="工作表名，不超过 31 字符")
    columns: list[ColumnSpec] = Field(min_length=1)
    rows: list[dict[str, Any]] = Field(default_factory=list)


class ExportExcelArgs(BaseModel):
    filename: str = Field(
        min_length=1, max_length=120, description="文件名（不含路径），如「选品分析.xlsx」"
    )
    title: str = Field(description="报表大标题")
    subtitle: str | None = Field(default=None, description="副标题，通常写数据口径与时间范围")
    sheets: list[SheetSpec] = Field(min_length=1, max_length=10)
    produced_by: str = Field(default="analytics", description="产出它的 Agent 名")
    task_id: int | None = None
    product_id: int | None = None


def _safe_filename(name: str) -> str:
    """把文件名收敛成安全形式。

    **不能直接用它拼路径**：``../../x.xlsx`` 会写到存储根外面。
    ``LocalStorage._resolve`` 还有一道防护，但入口处先过滤掉更清楚 ——
    安全校验分层做，任何一层单独失效都不至于出事。
    """
    cleaned = name.replace("\\", "/").split("/")[-1]
    for ch in '<>:"|?*':
        cleaned = cleaned.replace(ch, "_")
    cleaned = cleaned.strip(" .")
    if not cleaned.lower().endswith(".xlsx"):
        cleaned += ".xlsx"
    return cleaned or "report.xlsx"


def _storage_key(filename: str) -> str:
    """把人类可读的文件名收敛成 ASCII 安全的存储 key。

    **两件事必须分开**，这是踩过的坑：

    * ``assets.name`` 是给人看的，可以是「20260912-经营分析-T1.xlsx」
    * 存储 key 会进入 URL、HTTP 头、对象存储的桶规范，
      所以必须限在 ASCII 安全字符集内

    一开始直接把中文文件名当 key，结果存储层抛
    ``PathEscapeError`` —— 中文在 URL 里还要额外编码，
    不如在入口就换成 ASCII 形式。

    **还必须拼一个短随机串**：只靠日期 + 文件名不保证唯一，
    同一天两份「经营分析.xlsx」会互相覆盖 ——
    后写的把前一份默默冲掉，而 assets 表里还留着两条记录指向同一个文件。
    """
    stem = _safe_filename(filename)[:-5]  # 去掉 .xlsx
    ascii_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-") or "report"
    return f"generated/{date.today():%Y%m%d}/{ascii_stem[:60]}-{uuid4().hex[:8]}.xlsx"


def _register_asset(
    *,
    filename: str,
    key: str,
    size: int,
    mime: str,
    task_id: int | None,
    product_id: int | None,
    generator: str,
) -> str:
    """往 ``assets`` 表插一条记录，返回 asset_no。

    先插入拿到自增 id，再用 id 拼出 ``AS{日期}{序号}`` ——
    这样 asset_no 天然唯一，不用额外查一次"当前最大值"（那样有并发问题）。
    """
    with session_scope() as session:
        asset = Asset(
            asset_no="TEMP",
            task_id=task_id,
            product_id=product_id,
            asset_type="spreadsheet",
            name=filename,
            storage_backend="local",
            storage_path=key,
            mime_type=mime,
            size_bytes=size,
            source="generated",
            generator=generator,
            review_status="approved",
        )
        session.add(asset)
        session.flush()
        asset.asset_no = f"AS{date.today():%y%m%d}{asset.id:04d}"
        session.flush()
        return asset.asset_no


def export_excel_core(
    filename: str,
    title: str,
    subtitle: str | None = None,
    sheets: list[dict[str, Any]] | None = None,
    produced_by: str = "analytics",
    task_id: int | None = None,
    product_id: int | None = None,
) -> dict[str, Any]:
    """``report.export_excel`` 的实现体。

    单独抽成模块级函数而不是留在 ``register()`` 闭包里，
    是因为业务工具（``tools/business.py`` 的
    ``analytics.render_business_report``）要在自己的 handler 里
    **直接复用**这套"渲染 → 落盘 → 登记 assets"的管线 ——
    工具 handler 拿不到网关，只能走函数调用。
    """
    specs = [SheetSpec.model_validate(s) for s in (sheets or [])]

    truncated: list[dict[str, Any]] = []
    render_sheets: list[Sheet] = []
    for spec in specs:
        rows = spec.rows
        if len(rows) > MAX_ROWS_PER_SHEET:
            truncated.append(
                {
                    "sheet": spec.name,
                    "kept": MAX_ROWS_PER_SHEET,
                    "dropped": len(rows) - MAX_ROWS_PER_SHEET,
                }
            )
            rows = rows[:MAX_ROWS_PER_SHEET]
        render_sheets.append(
            Sheet(
                name=spec.name,
                columns=[
                    Column(
                        key=c.key,
                        title=c.title,
                        number_format=c.number_format,
                        align=c.align,
                    )
                    for c in spec.columns
                ],
                rows=list(rows),
            )
        )

    payload = build_workbook(render_sheets, title=title, subtitle=subtitle)
    if len(payload) > MAX_REPORT_BYTES:
        raise ValueError(
            f"报表体积 {len(payload) / 1024 / 1024:.1f}MB 超过 "
            f"{MAX_REPORT_BYTES // 1024 // 1024}MB 上限，请减少行数或拆分多份"
        )

    safe_name = _safe_filename(filename)
    key = _storage_key(safe_name)
    stored = get_storage().save(
        key,
        payload,
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    asset_no = _register_asset(
        filename=safe_name,
        key=stored.key,
        size=stored.size_bytes,
        mime=stored.mime_type or "application/octet-stream",
        task_id=task_id,
        product_id=product_id,
        generator=produced_by,
    )

    return {
        "asset_no": asset_no,
        "filename": safe_name,
        "storage_key": stored.key,
        "size_bytes": stored.size_bytes,
        "sha256": stored.sha256,
        "url": get_storage().url_for(stored.key),
        "sheet_names": [s.name for s in render_sheets],
        "row_counts": {s.name: len(s.rows) for s in render_sheets},
        # 截断了就一定要说 —— 静默截断会让人以为看的是全量数据
        "truncated": truncated,
    }


def register(registry: ToolRegistry) -> None:
    @registry.tool(
        name="report.export_excel",
        description=(
            "把结构化数据渲染成 Excel 报表，落盘并登记为素材。"
            "返回 asset_no 与访问 URL，可以写进邮件正文。"
        ),
        args_model=ExportExcelArgs,
        scopes=(Scope.ASSET_WRITE,),
        read_only=False,
        # 生成报表是"可重放"的操作：同样的入参重跑一次得到同样的文件，
        # 不会像上架那样产生副作用，所以标记为幂等是安全的
        idempotent=True,
        timeout_s=60.0,
        tags=("report",),
        rate_limit_per_minute=10,
    )
    def export_excel(
        filename: str,
        title: str,
        subtitle: str | None = None,
        sheets: list[dict[str, Any]] | None = None,
        produced_by: str = "analytics",
        task_id: int | None = None,
        product_id: int | None = None,
    ) -> dict[str, Any]:
        return export_excel_core(
            filename=filename,
            title=title,
            subtitle=subtitle,
            sheets=sheets,
            produced_by=produced_by,
            task_id=task_id,
            product_id=product_id,
        )
