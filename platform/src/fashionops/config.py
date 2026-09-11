"""平台配置。

全部走环境变量（前缀 ``FASHIONOPS_``），也可以写在 ``.env``。

数据层是**外部可选**的：

* 配了 ``FASHIONOPS_DB_HOST`` → 用 MySQL
* 没配 → 落到 SQLite 文件（跑单测用，零外部依赖）

向量库与缓存的选型都能通过环境变量切换，业务代码只依赖适配器接口。
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from fashionops.net import ensure_localhost_bypasses_proxy

# src/fashionops/config.py → parents[2] 就是 platform/
_PLATFORM_ROOT = Path(__file__).resolve().parents[2]

# 侧翼效应，但是必须的：只要有用到配置的地方，本地服务就得绕过系统代理。
# 背景见 fashionops/net.py —— 不这么做的话，连本地 Qdrant 都会返回 502。
ensure_localhost_bypasses_proxy()


class Settings(BaseSettings):
    """进程级配置。"""

    model_config = SettingsConfigDict(
        env_prefix="FASHIONOPS_",
        # 绝对路径放在前面，保证在任意工作目录启动（uvicorn / pytest / uv run）
        # 都能读到同一份 platform/.env，不会因为 cwd 不同而读到别的配置
        env_file=(_PLATFORM_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 基础 ----
    app_name: str = "服装电商多 Agent 运营中台"
    env: str = Field(default="dev", description="环境标识：dev / staging / prod")
    data_dir: Path = Field(
        default=Path("./data"),
        description="本地数据根目录（SQLite 文件、素材、报表、日志都放这里）",
    )
    timezone: str = "Asia/Shanghai"

    # ---- 数据库 ----
    # 不配 db_host 就落到 SQLite 文件：跑单测、或先看界面时不需要 MySQL 在跑。
    db_host: str | None = Field(
        default=None, description="MySQL 地址。留空则自动用 SQLite 文件"
    )
    db_port: int = 3306
    db_user: str = "fashionops"
    db_password: str | None = Field(
        default=None, exclude=True, repr=False, description="由 init-mysql.ps1 写入 .env"
    )
    db_name: str = "fashionops"
    database_url: str | None = Field(
        default=None,
        description="完整连接串，可覆盖上面的分项配置（切 PostgreSQL 时用这个）",
    )
    db_echo: bool = Field(default=False, description="是否打印 SQL（调试用）")
    db_pool_size: int = Field(default=10, description="连接池大小，SQLite 下忽略")
    db_pool_recycle: int = Field(
        default=1800,
        description=(
            "连接回收秒数。MySQL 默认 wait_timeout 是 8 小时，"
            "这里设短一些，避免清晨第一批请求拿到已被服务端单向关闭的连接"
        ),
    )

    # ---- 向量库 ----
    vector_backend: str = Field(
        default="qdrant",
        description="向量后端：``qdrant``（本地实例）/ ``chroma``（嵌入式，备选）",
    )
    qdrant_url: str = Field(default="http://127.0.0.1:6333")
    qdrant_api_key: str | None = Field(
        default=None, exclude=True, repr=False, description="Qdrant Cloud 才需要，本地实例留空"
    )
    qdrant_collection_kb: str = Field(
        default="kb_articles", description="知识库集合名（客服 RAG 的政策/尺码/售后条款）"
    )
    qdrant_collection_tickets: str = Field(
        default="ticket_history",
        description="历史工单集合名（用相似历史工单做 few-shot 和人工话术参考）",
    )
    qdrant_timeout: float = Field(default=10.0, description="Qdrant 请求超时秒数")
    vector_dir: Path | None = Field(
        default=None, description="Chroma 备选后端的持久化目录，留空则用 ``data_dir/chroma``"
    )
    embedding_dim: int = Field(default=1024, description="向量维度，需与 embedding 模型一致")

    # ---- 对象存储 ----
    storage_backend: str = Field(
        default="local",
        description="对象存储后端：``local``（本地文件系统）/ ``minio``（生产）",
    )
    storage_dir: Path | None = Field(
        default=None, description="本地存储根目录，留空则用 ``data_dir/storage``"
    )

    # ---- 缓存 / 会话状态 ----
    cache_backend: str = Field(
        default="redis",
        description=(
            "缓存后端：``redis``（任务图 checkpoint + 网关限流计数）/ "
            "``sqlite``（无 Redis 时降级）/ ``memory``（单测用）"
        ),
    )
    redis_url: str = Field(
        default="redis://127.0.0.1:6379/0",
        description="db0=任务状态，db1=限流计数，db2=业务缓存（见 deploy/redis/redis.conf 注释）",
    )
    redis_key_prefix: str = Field(
        default="fashionops", description="键前缀，避免和同机上其他项目抢 key"
    )
    redis_socket_timeout: float = Field(default=5.0, description="Redis 读写超时秒数")

    # ---- 网络 ----
    no_proxy_extra: str | None = Field(
        default=None,
        description=(
            "额外需要绕过系统代理的地址（逗号分隔）。回环地址已自动加入，"
            "这里填内网域名。背景见 fashionops/net.py"
        ),
    )

    # ---- 模型 ----
    model_provider: str = Field(
        default="mock",
        description=(
            "模型提供方：``mock``（本地假响应，零 API Key 可跑通全流程）/ "
            "``deepseek`` / ``qwen`` / ``zhipu`` / ``openai``"
        ),
    )
    model_api_key: str | None = Field(default=None, exclude=True, repr=False)
    model_base_url: str | None = None
    model_name: str = Field(default="deepseek-chat", description="主力模型")
    model_name_fast: str = Field(
        default="deepseek-chat", description="轻量模型（用于模型路由，降本）"
    )
    model_temperature: float = 0.3

    # ---- 外部系统（全部 Mock 占位）----
    ecom_api_mode: str = Field(
        default="mock", description="电商平台：``mock`` / ``real``（抖音小店 / 淘宝开放平台）"
    )
    ad_api_mode: str = Field(
        default="mock", description="广告平台：``mock`` / ``real``（巨量引擎）"
    )
    erp_api_mode: str = Field(default="mock", description="ERP：``mock`` / ``real``")

    # ---- 工具网关 ----
    gateway_rate_limit_per_minute: int = Field(
        default=120, description="工具调用每分钟上限（按工具名分别计数）"
    )
    gateway_max_retries: int = Field(default=2, description="外部调用的最大重试次数")
    gateway_circuit_breaker_threshold: int = Field(
        default=5, description="连续失败多少次后熔断该工具"
    )
    gateway_circuit_breaker_cooldown: int = Field(default=60, description="熔断冷却秒数")

    # ---- 可观测性 ----
    otel_enabled: bool = Field(default=False, description="是否开启 OpenTelemetry 链路追踪")
    otel_endpoint: str | None = None
    otel_service_name: str = "fashionops"

    # ---- 校验器 ----

    @field_validator("data_dir")
    @classmethod
    def _ensure_data_dir(cls, value: Path) -> Path:
        resolved = value.expanduser().resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved

    # ---- 派生属性 ----

    @property
    def sqlalchemy_url(self) -> str:
        """实际使用的连接串。

        优先级：``database_url`` > MySQL 分项配置 > SQLite 文件。

        拆成分项配置的好处是密码单独一个变量，不用把密码拼进连接串字符串里 ——
        否则密码里出现一个 ``@`` 或 ``/`` 就会把连接串解析错。
        """
        if self.database_url:
            return self.database_url
        if self.db_host:
            # 密码可能含 @ : / ? 等 URL 保留字符，必须 percent-encode
            password = quote_plus(self.db_password or "")
            return (
                f"mysql+pymysql://{self.db_user}:{password}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"
            )
        return f"sqlite:///{(self.data_dir / 'fashionops.db').as_posix()}"

    @property
    def is_sqlite(self) -> bool:
        return self.sqlalchemy_url.startswith("sqlite")

    @property
    def is_mysql(self) -> bool:
        return self.sqlalchemy_url.startswith("mysql")

    @property
    def chroma_dir(self) -> Path:
        """Chroma 备选后端的数据目录。"""
        return (self.vector_dir or (self.data_dir / "chroma")).resolve()

    @property
    def redis_url_masked(self) -> str:
        """去掉密码的 Redis 地址，可以安全地写日志或回给 ``/health``。"""
        return re.sub(r"://[^@/]*@", "://***@", self.redis_url)

    def dependency_summary(self) -> dict[str, str]:
        """``/health`` 用的依赖摘要 —— 只暴露地址，不暴露任何密码。"""
        database = "sqlite" if self.is_sqlite else self.sqlalchemy_url.rsplit("@", 1)[-1]
        return {
            "database": database,
            "vector_backend": self.vector_backend,
            "qdrant": self.qdrant_url,
            "cache_backend": self.cache_backend,
            "redis": self.redis_url_masked,
            # 把 NO_PROXY 一并暴露出去：“本地服务却报 502” 这类问题看这一眼就清楚了
            "no_proxy": os.environ.get("NO_PROXY", ""),
        }

    @property
    def local_storage_dir(self) -> Path:
        return (self.storage_dir or (self.data_dir / "storage")).resolve()

    @property
    def checkpoint_db(self) -> Path:
        return self.data_dir / "checkpoints.db"

    @property
    def model_configured(self) -> bool:
        """是否配置了真实模型。``mock`` 模式下无需 Key，零成本跑通全流程。"""
        return self.model_provider != "mock" and bool(self.model_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例，避免重复解析环境变量。"""
    settings = Settings()
    # .env 里额外指定了要绕过代理的地址时，在这里并入 NO_PROXY
    ensure_localhost_bypasses_proxy(settings.no_proxy_extra)
    return settings
