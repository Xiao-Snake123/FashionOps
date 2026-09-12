"""对象存储抽象。

## 为什么要抽这一层

开发期素材就落在本地磁盘上，生产要换 MinIO / S3 / OSS。
如果业务代码里到处是 ``open(path, "wb")``，换存储就是全项目改一遍。

所以只暴露四个动作：``save`` / ``read`` / ``url_for`` / ``exists``。
本地实现按 ``storage_dir`` 落盘，将来加一个 ``MinioStorage`` 实现同一个协议即可。

## 路径沙箱

``save`` 的 key 是调用方给的（比如 ``generated/20260912/report.xlsx``），
必须做路径穿越防护：``../../etc/passwd`` 这种 key 不能让它在存储根之外写文件。

做法和 ``mcp-servers/common/paths.py`` 一致：先 ``resolve()`` 把 ``..`` 和软链接
都展开，再按**路径段**比较，而不是字符串前缀比较 ——
后者会把 ``/data-evil`` 误判成 ``/data`` 的子路径。
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from fashionops.config import get_settings

logger = logging.getLogger("fashionops.storage")

#: key 里只允许这些字符。中文、空格、点号一律禁掉 ——
#: 它们在不同文件系统上的表现不一致，而且容易和路径分隔符混淆
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


class PathEscapeError(ValueError):
    """key 试图跳出存储根目录。"""


@dataclass(frozen=True)
class StoredObject:
    """一次写入的结果。"""

    key: str
    backend: str
    size_bytes: int
    sha256: str
    mime_type: str | None = None

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "backend": self.backend,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "mime_type": self.mime_type,
        }


class StorageBackend(Protocol):
    backend_name: str

    def save(self, key: str, data: bytes, *, mime_type: str | None = None) -> StoredObject: ...
    def read(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def url_for(self, key: str) -> str: ...


class LocalStorage:
    """本地文件系统实现。

    存储根之外一律拒绝写入。注意这里**不做软链接逃逸防护的额外处理** ——
    ``resolve()`` 已经把软链接展开了，所以只要在 resolve 之后再比较就够。
    """

    backend_name = "local"

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or get_settings().local_storage_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def _resolve(self, key: str) -> Path:
        """把 key 解析成绝对路径，并确认它没跑出存储根。"""
        if not key or not _SAFE_KEY_RE.match(key):
            raise PathEscapeError(f"非法的存储 key：{key!r}（只允许字母数字和 . _ - /）")

        candidate = (self.root / key).resolve()
        # 按路径段比较，不能用 str.startswith ——
        # 那样 /data-evil 会被当成 /data 的子路径
        if candidate != self.root and self.root not in candidate.parents:
            raise PathEscapeError(f"key 跳出了存储根：{key!r} -> {candidate}")
        return candidate

    # ------------------------------------------------------------------
    def save(self, key: str, data: bytes, *, mime_type: str | None = None) -> StoredObject:
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)

        # 先写临时文件再原子替换：直接覆盖原文件的话，
        # 如果写到一半进程挂了，会留下一个半截文件 —— 前端下载时才发现损坏。
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)

        digest = hashlib.sha256(data).hexdigest()
        return StoredObject(
            key=key,
            backend=self.backend_name,
            size_bytes=len(data),
            sha256=digest,
            mime_type=mime_type or mimetypes.guess_type(key)[0],
        )

    def read(self, key: str) -> bytes:
        return self._resolve(key).read_bytes()

    def exists(self, key: str) -> bool:
        try:
            return self._resolve(key).is_file()
        except PathEscapeError:
            return False

    def url_for(self, key: str) -> str:
        """给前端用的访问地址。

        本地实现返回一个后端静态路由（阶段 2 接上），不是 ``file://`` ——
        浏览器不允许网页直接读本地文件，返回 file:// 等于给了个不能用的地址。
        """
        return f"/api/v1/assets/raw/{key}"

    def absolute_path(self, key: str) -> Path:
        """本地实现特有：拿到真实路径（比如给 Excel 库直接写文件用）。"""
        return self._resolve(key)


_storage: StorageBackend | None = None


def get_storage(backend: str | None = None) -> StorageBackend:
    """按配置返回存储后端。目前只有本地实现。"""
    global _storage
    if _storage is not None and backend is None:
        return _storage

    settings = get_settings()
    name = backend or settings.storage_backend
    if name != "local":
        # 静默回退比报错好：存储配置写错不该让整个应用起不来。
        # 但必须告警 —— 否则会以为在往 MinIO 写，实际一直在写本地磁盘。
        logger.warning("storage_backend=%s 还没实现，已回退到本地文件系统", name)
    _storage = LocalStorage()
    return _storage


def reset_storage() -> None:
    """清掉单例。测试里换配置后要调用。"""
    global _storage
    _storage = None
