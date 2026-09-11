"""办公文档读写测试：往返一致性、格式覆盖、异常路径。"""

from __future__ import annotations

from pathlib import Path

import pytest

from office_mcp.common.errors import DocumentParseError, UnsupportedFormatError
from office_mcp.docs import office
from office_mcp.docs.office import DocxBlock, SheetSpec, SlideSpec


class TestWord:
    def test_create_and_read_back(self, tmp_path: Path) -> None:
        target = tmp_path / "r.docx"
        office.create_docx(
            target,
            "季度总结",
            [
                DocxBlock(kind="heading", text="一、成果", level=1),
                DocxBlock(kind="paragraph", text="完成核心模块开发。"),
                DocxBlock(kind="bullets", items=["路径沙箱", "审计日志"]),
                DocxBlock(kind="numbered", items=["第一步", "第二步"]),
                DocxBlock(
                    kind="table",
                    rows=[["指标", "值"], ["成功率", "84%"], ["步数", "7.8"]],
                ),
                DocxBlock(kind="page_break"),
                DocxBlock(kind="heading", text="二、计划", level=1),
            ],
        )
        assert target.exists()
        text = office.read_document(target)

        assert "# 一、成果" in text  # 标题转为 Markdown 标题
        assert "完成核心模块开发。" in text
        assert "- 路径沙箱" in text
        assert "| 指标 | 值 |" in text
        assert "| 成功率 | 84% |" in text
        assert "# 二、计划" in text  # 分页符之后的标题也读到了

    def test_title_written(self, tmp_path: Path) -> None:
        target = tmp_path / "t.docx"
        office.create_docx(target, "文档标题", [])
        assert "文档标题" in office.read_document(target)

    def test_empty_blocks_ok(self, tmp_path: Path) -> None:
        target = tmp_path / "empty.docx"
        info = office.create_docx(target, "空文档", [])
        assert info["tables"] == 0


class TestExcel:
    def test_create_and_read_back(self, tmp_path: Path) -> None:
        target = tmp_path / "b.xlsx"
        office.create_xlsx(
            target,
            [
                SheetSpec(
                    name="核心指标",
                    rows=[["日期", "任务数"], ["2026-09-01", 120], ["2026-09-02", 145]],
                ),
                SheetSpec(name="明细", rows=[["a", "b"], ["1", "2"]]),
            ],
        )
        assert target.exists()
        text = office.read_document(target)
        assert "## 工作表：核心指标" in text
        assert "## 工作表：明细" in text
        assert "| 日期 | 任务数 |" in text
        assert "| 2026-09-01 | 120 |" in text

    def test_sheet_name_length_capped(self, tmp_path: Path) -> None:
        """Excel 工作表名上限 31 字符，超长必须被截断否则文件损坏。"""
        target = tmp_path / "long.xlsx"
        office.create_xlsx(target, [SheetSpec(name="x" * 80, rows=[["a"]])])
        assert target.exists()
        assert "## 工作表：" in office.read_document(target)

    def test_autofilter_and_freeze(self, tmp_path: Path) -> None:
        from openpyxl import load_workbook

        target = tmp_path / "style.xlsx"
        office.create_xlsx(target, [SheetSpec(name="s", rows=[["h1", "h2"], [1, 2]])])
        wb = load_workbook(target)
        ws = wb["s"]
        assert ws.freeze_panes == "A2"
        assert ws.auto_filter.ref is not None
        assert ws["A1"].font.bold is True
        wb.close()

    def test_numeric_cells_preserved(self, tmp_path: Path) -> None:
        """数值不能被写成字符串，否则 Excel 无法计算。"""
        from openpyxl import load_workbook

        target = tmp_path / "num.xlsx"
        office.create_xlsx(target, [SheetSpec(name="s", rows=[["v"], [0.84], [120]])])
        wb = load_workbook(target)
        assert isinstance(wb["s"]["A2"].value, float)
        assert isinstance(wb["s"]["A3"].value, int)
        wb.close()


class TestPowerPoint:
    def test_create_and_read_back(self, tmp_path: Path) -> None:
        target = tmp_path / "p.pptx"
        office.create_pptx(
            target,
            "项目汇报",
            [
                SlideSpec(title="背景", bullets=["痛点一", "痛点二"], notes="讲得慢一点"),
                SlideSpec(title="方案", bullets=["MCP", "LangGraph"]),
            ],
        )
        text = office.read_document(target)
        assert "项目汇报" in text  # 封面
        assert "背景" in text
        assert "- 痛点一" in text
        assert "备注：讲得慢一点" in text
        assert "方案" in text

    def test_slide_count_includes_cover(self, tmp_path: Path) -> None:
        target = tmp_path / "c.pptx"
        info = office.create_pptx(target, "T", [SlideSpec(title="a"), SlideSpec(title="b")])
        assert info["slide_count"] == 3  # 封面 + 2 页


class TestTextFormats:
    @pytest.mark.parametrize(
        ("name", "content", "expect"),
        [
            ("a.md", "# 标题\n正文", "# 标题"),
            ("a.txt", "纯文本", "纯文本"),
            ("a.csv", "name,qty\n苹果,3\n", "| name | qty |"),
            ("a.json", '{"k": "v"}', '"k": "v"'),
        ],
    )
    def test_read_text_formats(self, tmp_path: Path, name: str, content: str, expect: str) -> None:
        target = tmp_path / name
        target.write_text(content, encoding="utf-8")
        assert expect in office.read_document(target)

    def test_gbk_fallback(self, tmp_path: Path) -> None:
        target = tmp_path / "gbk.txt"
        target.write_bytes("中文内容".encode("gbk"))
        assert "中文内容" in office.read_document(target)

    def test_write_text_round_trip(self, tmp_path: Path) -> None:
        target = tmp_path / "w.md"
        # "# 标题\n\n内容\n" 共 3 个换行 → 4 行，9 个字符
        info = office.write_text(target, "# 标题\n\n内容\n")
        assert info["lines"] == 4
        assert info["chars"] == 9
        assert office.read_document(target).startswith("# 标题")

    def test_write_without_overwrite_raises(self, tmp_path: Path) -> None:
        target = tmp_path / "e.txt"
        office.write_text(target, "first")
        with pytest.raises(DocumentParseError) as exc:
            office.write_text(target, "second", overwrite=False)
        assert target.read_text(encoding="utf-8") == "first"
        assert exc.value.hint


class TestErrorsAndLimits:
    def test_unsupported_format(self, tmp_path: Path) -> None:
        target = tmp_path / "x.xyz"
        target.write_text("x", encoding="utf-8")
        with pytest.raises(UnsupportedFormatError) as exc:
            office.read_document(target)
        assert "支持" in (exc.value.hint or "")

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(DocumentParseError):
            office.read_document(tmp_path / "nope.docx")

    def test_corrupt_docx(self, tmp_path: Path) -> None:
        target = tmp_path / "bad.docx"
        target.write_bytes(b"this is not a real docx")
        with pytest.raises(Exception):  # noqa: B017 - 底层库异常类型不稳定
            office.read_document(target)

    def test_list_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("a", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.docx").write_bytes(b"x")

        assert len(office.list_files(tmp_path)) == 2
        assert len(office.list_files(tmp_path, pattern="*.txt")) == 1
        assert len(office.list_files(tmp_path, recursive=False)) == 1
        assert len(office.list_files(tmp_path, limit=1)) == 1

    def test_list_files_paths_use_forward_slash(self, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.txt").write_text("b", encoding="utf-8")
        paths = [f["path"] for f in office.list_files(tmp_path)]
        assert paths == ["sub/b.txt"]

    def test_atomic_write_leaves_no_temp_files(self, tmp_path: Path) -> None:
        office.write_text(tmp_path / "t.md", "content")
        leftovers = [p.name for p in tmp_path.iterdir() if p.name != "t.md"]
        assert leftovers == []
