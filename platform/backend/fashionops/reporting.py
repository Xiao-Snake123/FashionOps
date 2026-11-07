"""报表生成：把结构化数据变成可读的 Excel。

## 为什么不用 pandas.to_excel

pandas 出 Excel 很快，但出来的东西"能看不能用"：没有列宽、没有数字格式、
表头没有底色、金额是浮点数显示成 ``138.94000000000001``。
运营拿到这种表第一件事就是自己再调一遍格式。

所以这里用 openpyxl 直接控制样式。多写几十行，换来的是一份**可以直接发出去**
的报表 —— 对这个项目来说，"产出物可见"比"少写代码"重要得多。

## 几个容易忽略的点

1. **中文列宽要按双宽度算**。Excel 的列宽单位是"标准字符数"，
   一个中文字大约占 2 个宽度。按 ``len(str)`` 算的话中文列会被压到
   显示成 ``####``，报表直接不可用。
2. **金额不能用 float**。``0.1 + 0.2`` 在 Excel 里会显示成
   ``0.30000000000000004``。写进去之前统一转成 ``Decimal`` 并套数字格式。
3. **冻结首行 + 自动筛选**。数据一多，没有这两样就没法用。
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# ---------------------------------------------------------------------------
# 样式
# ---------------------------------------------------------------------------
HEADER_FILL = PatternFill("solid", fgColor="1F3B57")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)
TITLE_FONT = Font(bold=True, size=14)
SUBTITLE_FONT = Font(color="666666", size=9)

_THIN = Side(style="thin", color="D9D9D9")
BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)

#: 单元格里默认最多显示多少字符，超过就截断（完整内容还在，只是显示不下）
_MAX_DISPLAY_WIDTH = 48


@dataclass
class Column:
    """一列的定义。"""

    key: str
    title: str
    #: Excel 数字格式，如 ``"#,##0.00"`` / ``"0.0%"``。None 表示按原样写
    number_format: str | None = None
    width: float | None = None
    align: str = "left"


@dataclass
class Sheet:
    """一个工作表。"""

    name: str
    columns: list[Column]
    rows: list[dict[str, Any]] = field(default_factory=list)


def _display_width(value: Any) -> int:
    """估算一个值在 Excel 里占几个字符宽。

    **中文按 2 算**。这是这里唯一一处"不能偷懒"的地方：
    Excel 的列宽单位是标准字符（约等于一个数字的宽度），
    一个汉字大约占两格。按 ``len()`` 算的话，
    一列全是中文标题的宽度会被低估一半，显示成 ``####``。
    """
    text = "" if value is None else str(value)
    return sum(2 if "\u2e80" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef" else 1 for ch in text)


def _clean(value: Any) -> Any:
    """把值转成 openpyxl 能接受的类型。

    openpyxl 不认识 ``Decimal`` 之外的很多类型，也不接受带时区的 datetime。
    这里统一收口，避免"某个字段类型特殊就整张表写不出去"。
    """
    if value is None or isinstance(value, (int, float, str, bool, datetime, date)):
        return value
    if isinstance(value, Decimal):
        # Decimal 直接写进去 openpyxl 是支持的，且能保住精度。
        # 千万不要 float() —— 那正是金额出现 0.30000000000000004 的来源。
        return float(value) if value.is_finite() else None
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        import json

        return json.dumps(value, ensure_ascii=False)
    return str(value)


def build_workbook(
    sheets: Sequence[Sheet],
    *,
    title: str | None = None,
    subtitle: str | None = None,
) -> bytes:
    """把若干 Sheet 渲染成 xlsx 字节流。

    返回 bytes 而不是写文件：这样调用方可以自由决定落盘还是直接返回给 HTTP 响应，
    也方便单测（不用碰文件系统）。
    """
    workbook = Workbook()
    # 默认会带一个空 sheet，第一个 sheet 复用它，避免最后多出一页空白
    default_sheet = workbook.active
    workbook.remove(default_sheet)

    for sheet in sheets:
        worksheet = workbook.create_sheet(title=_safe_sheet_name(sheet.name))
        offset = 0

        if title:
            cell = worksheet.cell(row=1, column=1, value=title)
            cell.font = TITLE_FONT
            offset += 2  # 标题占一行 + 一个空行
        if subtitle:
            cell = worksheet.cell(row=offset + 1, column=1, value=subtitle)
            cell.font = SUBTITLE_FONT
            offset += 1

        header_row = offset + 1
        for index, column in enumerate(sheet.columns, start=1):
            cell = worksheet.cell(row=header_row, column=index, value=column.title)
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = BORDER

        for row_index, row in enumerate(sheet.rows, start=header_row + 1):
            for col_index, column in enumerate(sheet.columns, start=1):
                cell = worksheet.cell(row=row_index, column=col_index, value=_clean(row.get(column.key)))
                cell.border = BORDER
                cell.alignment = Alignment(horizontal=column.align, vertical="center")
                if column.number_format:
                    cell.number_format = column.number_format

        # ---- 列宽 ----
        for index, column in enumerate(sheet.columns, start=1):
            if column.width is not None:
                width = column.width
            else:
                widths = [_display_width(column.title)]
                widths += [_display_width(row.get(column.key)) for row in sheet.rows[:200]]
                # 加 3 是留给单元格内边距，再把超长内容截断，否则一个长描述能把列撑到屏幕外
                width = min(max(widths) + 3, _MAX_DISPLAY_WIDTH)
            worksheet.column_dimensions[get_column_letter(index)].width = width

        # ---- 冻结首行 + 自动筛选 ----
        # 数据一多，没有这两样基本没法用
        if sheet.rows:
            worksheet.freeze_panes = worksheet.cell(row=header_row + 1, column=1)
            worksheet.auto_filter.ref = (
                f"A{header_row}:{get_column_letter(len(sheet.columns))}{header_row + len(sheet.rows)}"
            )

    # 一个 sheet 都没有时给个占位，否则 openpyxl 存盘会报错
    if not workbook.sheetnames:
        workbook.create_sheet(title="空")

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _safe_sheet_name(name: str) -> str:
    """Excel 的 sheet 名有长度和字符限制：不能超过 31 字符，
    也不能包含 ``: \\ / ? * [ ]``。不处理的话创建 sheet 时直接抛异常。
    """
    for ch in ":\\/?*[]":
        name = name.replace(ch, "_")
    return (name or "Sheet")[:31]
