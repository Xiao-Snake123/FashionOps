"""路径沙箱测试。

这是安全边界，必须覆盖各种绕过手法：
绝对路径、``..`` 穿越、符号链接、前缀混淆（``/data-evil`` vs ``/data``）、
以及"由标题生成文件名"时的字符注入。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from office_mcp.common.errors import FileNotInSandboxError, PathViolationError
from office_mcp.common.paths import resolve_in_root, safe_filename


@pytest.fixture()
def sandbox(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "sub").mkdir(parents=True)
    (root / "ok.txt").write_text("hello", encoding="utf-8")
    (root / "sub" / "nested.txt").write_text("nested", encoding="utf-8")
    return root


class TestResolveInRoot:
    def test_relative_path_inside(self, sandbox: Path) -> None:
        assert resolve_in_root("ok.txt", sandbox).name == "ok.txt"

    def test_nested_relative_path(self, sandbox: Path) -> None:
        assert resolve_in_root("sub/nested.txt", sandbox).exists()

    def test_absolute_path_inside(self, sandbox: Path) -> None:
        assert resolve_in_root(sandbox / "ok.txt", sandbox).exists()

    def test_dot_segments_stay_inside(self, sandbox: Path) -> None:
        assert resolve_in_root("./sub/../ok.txt", sandbox).name == "ok.txt"

    @pytest.mark.parametrize(
        "evil",
        [
            "../outside.txt",
            "../../outside.txt",
            "sub/../../outside.txt",
            "..\\..\\outside.txt",
        ],
    )
    def test_parent_traversal_blocked(self, sandbox: Path, evil: str) -> None:
        with pytest.raises(PathViolationError) as exc:
            resolve_in_root(evil, sandbox)
        assert exc.value.code == "path_violation"
        assert exc.value.hint  # 必须给模型可执行的修复建议

    def test_absolute_escape_blocked(self, sandbox: Path) -> None:
        outside = sandbox.parent / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        with pytest.raises(PathViolationError):
            resolve_in_root(outside, sandbox)

    def test_sibling_prefix_not_confused(self, tmp_path: Path) -> None:
        """``workspace-evil`` 不能被当成 ``workspace`` 的子目录。"""
        root = tmp_path / "workspace"
        root.mkdir()
        sibling = tmp_path / "workspace-evil"
        sibling.mkdir()
        (sibling / "x.txt").write_text("x", encoding="utf-8")

        with pytest.raises(PathViolationError):
            resolve_in_root(sibling / "x.txt", root)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows 专属：目录联接/符号链接")
    def test_symlink_escape_blocked(self, tmp_path: Path, sandbox: Path) -> None:
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        (outside_dir / "secret.txt").write_text("secret", encoding="utf-8")
        link = sandbox / "link"
        try:
            link.symlink_to(outside_dir, target_is_directory=True)
        except OSError:
            pytest.skip("当前环境不允许创建符号链接")

        with pytest.raises(PathViolationError):
            resolve_in_root("link/secret.txt", sandbox)

    def test_must_exist_raises_file_not_found(self, sandbox: Path) -> None:
        with pytest.raises(FileNotInSandboxError) as exc:
            resolve_in_root("nope.txt", sandbox, must_exist=True)
        # 与越权区分：模型需要知道这是"文件不存在"而不是"你没权限"
        assert exc.value.code == "file_not_found"

    def test_missing_file_without_must_exist_is_allowed(self, sandbox: Path) -> None:
        target = resolve_in_root("new/new.txt", sandbox)
        assert target.name == "new.txt"


class TestSafeFilename:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("normal", "normal"),
            ("has space", "has space"),
            ("a/b/c", "a_b_c"),
            ("a\\b", "a_b"),
            ("..", "untitled"),
            # `..` 与 `/` 都被就地替换，结果里不再含任何路径分隔符或点号
            ("../../etc/passwd", "___etc_passwd"),
            ("bad:name*?", "bad_name__"),
        ],
    )
    def test_sanitize(self, raw: str, expected: str) -> None:
        assert safe_filename(raw) == expected

    def test_empty_becomes_untitled(self) -> None:
        assert safe_filename("   ") == "untitled"

    def test_default_ext_appended(self) -> None:
        assert safe_filename("report", default_ext=".docx") == "report.docx"

    def test_existing_ext_not_duplicated(self) -> None:
        assert safe_filename("report.docx", default_ext=".docx") == "report.docx"

    def test_windows_reserved_name_escaped(self) -> None:
        assert safe_filename("CON") == "_CON"
        assert safe_filename("com1.docx") == "_com1.docx"

    def test_length_capped(self) -> None:
        assert len(safe_filename("x" * 500)) <= 120
