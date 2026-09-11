"""配置：全部通过环境变量注入，工具签名中不出现任何密钥。

环境变量统一使用 ``OFFICE_MCP_`` 前缀，例如::

    OFFICE_MCP_WORKSPACE_ROOT=D:/office-data
    OFFICE_MCP_AUDIT_LOG=D:/office-data/audit.jsonl
    OFFICE_MCP_DRY_RUN=1
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """进程级配置。"""

    model_config = SettingsConfigDict(
        env_prefix="OFFICE_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 沙箱 ----
    workspace_root: Path = Field(
        default=Path("./workspace"),
        description="允许读写的根目录。所有文件工具都被限制在此目录内。",
    )

    # ---- 审计 ----
    audit_log: Path | None = Field(
        default=None,
        description="审计日志文件路径（JSONL）。为空则关闭审计。",
    )
    audit_redact_keys: tuple[str, ...] = Field(
        default=("password", "token", "secret", "api_key", "authorization", "cookie"),
        description="审计日志中需要脱敏的参数名（小写匹配）。",
    )

    # ---- 运行模式 ----
    dry_run: bool = Field(
        default=False,
        description="为真时不产生任何外部副作用（不发邮件、不写日历），只返回将要执行的动作。",
    )
    max_read_bytes: int = Field(default=8 * 1024 * 1024, description="单文件读取上限（字节）。")

    # ---- 邮件 ----
    smtp_host: str | None = None
    smtp_port: int = 465
    smtp_use_ssl: bool = True
    imap_host: str | None = None
    imap_port: int = 993
    imap_use_ssl: bool = True
    mail_address: str | None = None
    mail_password: str | None = Field(default=None, exclude=True, repr=False)

    # ---- 日历 ----
    caldav_url: str | None = None
    caldav_username: str | None = None
    caldav_password: str | None = Field(default=None, exclude=True, repr=False)
    timezone: str = "Asia/Shanghai"

    @field_validator("workspace_root")
    @classmethod
    def _expand_workspace(cls, value: Path) -> Path:
        resolved = value.expanduser().resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved

    @field_validator("audit_log")
    @classmethod
    def _expand_audit(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        resolved = value.expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    @property
    def mail_configured(self) -> bool:
        return bool(self.smtp_host and self.mail_address and self.mail_password)

    @property
    def calendar_configured(self) -> bool:
        return bool(self.caldav_url)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例，避免重复解析环境变量。"""
    return Settings()
