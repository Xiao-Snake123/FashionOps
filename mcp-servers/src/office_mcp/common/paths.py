"""路径沙箱。

模型可能生成任意路径（包括 ``../../etc/passwd`` 这类），因此所有文件操作
必须先经 :func:`resolve_in_root` 归一化并校验仍在允许的根目录内。

实现要点：
- 先 ``resolve()`` 解析符号链接与 ``..``，再做前缀比较（只比较路径段，避免
  ``/data-evil`` 被误判为 ``/data`` 的子路径）。
- 对不存在的目标文件，先解析其父目录再拼接文件名。
"""

from __future__ import annotations

import re
from pathlib import Path

from office_mcp.common.errors import FileNotInSandboxError, PathViolationError

_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def resolve_in_root(path: str | Path, root: str | Path, *, must_exist: bool = False) -> Path:
    """把 ``path`` 解析为绝对路径，并确保它位于 ``root`` 之内。

    Args:
        path: 用户/模型给出的路径，可以是相对路径或绝对路径。
        root: 允许访问的根目录。
        must_exist: 为真时要求目标文件必须已存在。

    Raises:
        PathViolationError: 路径越界，或 ``must_exist`` 但文件不存在。
    """
    root_resolved = Path(root).expanduser().resolve()

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root_resolved / candidate

    # 对已存在的路径直接 resolve；对不存在的路径只 resolve 父目录，
    # 避免因为文件不存在而无法完成越界校验。
    if candidate.exists():
        target = candidate.resolve()
    else:
        target = candidate.parent.resolve() / candidate.name

    if not _is_relative_to(target, root_resolved):
        raise PathViolationError(
            f"路径越界：{path} 不在允许的根目录内。",
            hint=f"只能访问 {root_resolved} 目录及其子目录下的文件。",
        )

    if must_exist and not target.exists():
        raise FileNotInSandboxError(
            f"文件不存在：{path}",
            hint=f"请先用 list_files 查看 {root_resolved} 下有哪些文件，再选择正确的路径。",
        )

    return target


def _is_relative_to(child: Path, parent: Path) -> bool:
    """``Path.is_relative_to`` 的显式实现，便于兼容与测试。"""
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def safe_filename(name: str, *, default_ext: str = "") -> str:
    """把任意字符串清洗成安全文件名（去掉路径分隔符与保留字符）。

    用于"由标题生成文件名"这类场景，避免模型给出的标题里带 ``/`` 或 ``..``。
    """
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", name).strip().strip(".")
    cleaned = re.sub(r"\.{2,}", "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = "untitled"
    if default_ext and not cleaned.lower().endswith(default_ext.lower()):
        cleaned = f"{cleaned}{default_ext}"
    # Windows 保留名保护
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if cleaned.split(".")[0].upper() in reserved:
        cleaned = f"_{cleaned}"
    return cleaned[:120]
