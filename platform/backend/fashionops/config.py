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

# backend/fashionops/config.py → parents[2] 就是 platform/
_PLATFORM_ROOT = Path(__file__).resolve().parents[2]

#: 所有环境变量的统一前缀。
#: 抽成常量是因为 ``config_doctor`` 要靠它反推"合法的环境变量名有哪些"——
#: 两边各写一份字面量的话，改了前缀就会出现"体检工具自己过时了"的静默失效。
ENV_PREFIX = "FASHIONOPS_"


def _split_csv(raw: str | None) -> tuple[str, ...]:
    """把逗号（或空白）分隔的配置拆成元组，顺手去掉空项。

    白名单这类配置用字符串而不是列表，是为了在 ``.env`` 里好写：
    写成 JSON 数组的话要处理引号和转义，改一次错一次。
    """
    if not raw:
        return ()
    parts = re.split(r"[,\s]+", raw.strip())
    return tuple(p for p in parts if p)


# 侧翼效应，但是必须的：只要有用到配置的地方，本地服务就得绕过系统代理。
# 背景见 fashionops/net.py —— 不这么做的话，连本地 Qdrant 都会返回 502。
ensure_localhost_bypasses_proxy()


class Settings(BaseSettings):
    """进程级配置。"""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        # 绝对路径放在前面，保证在任意工作目录启动（uvicorn / pytest / uv run）
        # 都能读到同一份 platform/.env，不会因为 cwd 不同而读到别的配置
        env_file=(_PLATFORM_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        # 未知环境变量**不报错、直接丢弃**。
        #
        # 这是刻意的：改成 forbid 的话，一个多余的变量就会让应用起不来，
        # 而生产上"起不来"比"配置没生效"严重得多。
        # 代价是拼错变量名**没有任何反馈** —— 所以配了 `config_doctor`
        # 在启动日志和 CLI 里把这类问题报出来。
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

    # ---- 日志 ----
    log_level: str = Field(
        default="INFO",
        description="DEBUG / INFO / WARNING / ERROR。开发期想看清链路就改 DEBUG",
    )
    log_format: str = Field(
        default="",
        description=(
            "``console``（人读，带颜色）或 ``json``（机器读，接 ELK/Loki）。"
            "留空则按 env 自动选：prod 用 json，其余用 console"
        ),
    )

    # ---- 数据库 ----
    # 不配 db_host 就落到 SQLite 文件：跑单测、或先看界面时不需要 MySQL 在跑。
    db_host: str | None = Field(default=None, description="MySQL 地址。留空则自动用 SQLite 文件")
    db_port: int = 3306
    db_user: str = "fashionops"
    db_password: str | None = Field(default=None, exclude=True, repr=False, description="由 init-mysql.ps1 写入 .env")
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

    # ---- Embedding（文本向量化，客服 RAG 用）----
    # 默认**复用 chat 的 provider 和 Key**：智谱等厂商同一个 Key 同时管
    # chat 和 embedding，再让用户配一份是多余的心智负担。
    # 需要混用（chat 用智谱、embedding 用本地 BGE）时才填下面这几个。
    embedding_provider: str = Field(default="", description="留空则跟随 ``model_provider``；可填 zhipu/qwen/local/mock")
    embedding_model: str = Field(
        default="embedding-2",
        description="智谱 embedding-2 是 1024 维（和 embedding_dim 对应）；OpenAI 用 text-embedding-3-small",
    )
    embedding_api_key: str | None = Field(
        default=None, exclude=True, repr=False, description="留空则跟随 ``model_api_key``"
    )
    embedding_base_url: str = Field(
        default="", description="留空则按 provider 推导；本地部署的 embedding 服务必须显式填"
    )
    embedding_timeout: float = Field(default=30.0, description="单次向量化请求超时秒数")

    # ---- 检索增强（RAG 召回）----
    # 混合检索：稠密（语义向量）+ 稀疏（词法 BM25 风格）通过 Qdrant 的
    # ``FusionQuery(RRF)`` 在库内融合，专门补"精确词 / 编号 / 罕见术语"这类
    # 纯语义向量召回不力的盲区。关掉则退回纯稠密向量检索（与改造前一致）。
    retrieval_hybrid: bool = Field(
        default=True,
        description="是否启用混合检索（稠密 + 稀疏 RRF 融合）。False 则退回纯稠密向量检索",
    )
    kb_sparse_vector_name: str = Field(
        default="bm25",
        description="KB 集合里稀疏向量的名字，需与建索引时一致（见 vectors.base.KB_SPARSE_VECTOR_NAME）",
    )

    # ---- 对象存储 ----
    storage_backend: str = Field(
        default="local",
        description="对象存储后端：``local``（本地文件系统）/ ``minio``（生产）",
    )
    storage_dir: Path | None = Field(default=None, description="本地存储根目录，留空则用 ``data_dir/storage``")

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
    redis_key_prefix: str = Field(default="fashionops", description="键前缀，避免和同机上其他项目抢 key")
    redis_socket_timeout: float = Field(default=5.0, description="Redis 读写超时秒数")

    # ---- 业务缓存（db2）----
    # db2 在 cache.py 里被规划成"业务缓存：列表页、聚合结果"，但长期以来
    # 没有任何读接口用它（只有网关幂等存储误用了它，见 gateway/core.py）。
    # 这两个配置就是把它真正用起来。
    business_cache_enabled: bool = Field(
        default=True,
        description=(
            "业务缓存（Redis db2）总开关。关掉后所有只读聚合接口直连库。"
            "⚠ 测试里必须关：测试用临时 SQLite 库，而 Redis 是共享实例，"
            "开着会让不同用例读到彼此的陈旧结果（conftest.py 里有 autouse 夹具关它）。"
        ),
    )
    business_cache_ttl: int = Field(
        default=30,
        ge=1,
        le=3600,
        description="业务缓存默认 TTL（秒）。纯易失数据，回源查库即可，不需要持久化。",
    )

    # ---- 网络 ----
    no_proxy_extra: str | None = Field(
        default=None,
        description=(
            "额外需要绕过系统代理的地址（逗号分隔）。回环地址已自动加入，这里填内网域名。背景见 fashionops/net.py"
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
    model_name_fast: str = Field(default="deepseek-chat", description="轻量模型（用于模型路由，降本）")
    model_temperature: float = 0.3
    model_timeout: float = Field(
        default=120.0,
        description=(
            "单次模型请求的读超时（秒）。**别调太小**：工具把候选款回灌之后"
            "再让模型吐结构化 JSON，那一步实测能跑满 60 秒以上"
            "（真实事故见 llm/openai_compat.py 的重试说明）。"
        ),
    )
    model_max_retries: int = Field(
        default=2,
        description=(
            "对**判定为可重试**的错误（超时、连接失败、429、5xx）额外重试几次，"
            "指数退避 + 抖动。401/400 这类【重试也没用】的错误一次就失败，"
            "不会傻乎乎重试到把配额耗光。"
        ),
    )

    # ---- 外部系统（全部 Mock 占位）----
    ecom_api_mode: str = Field(default="mock", description="电商平台：``mock`` / ``real``（抖音小店 / 淘宝开放平台）")
    ad_api_mode: str = Field(default="mock", description="广告平台：``mock`` / ``real``（巨量引擎）")
    erp_api_mode: str = Field(default="mock", description="ERP：``mock`` / ``real``")

    # ---- 邮件（通知 Agent）----
    # 给 Agent 装上一双能发邮件的手，等于给它一个对外发声的通道。
    # 邮件发出去了就收不回来，所以这里的每一个默认值都选了"更安全的那个"：
    # 总开关默认关、dry_run 默认开、白名单默认空（空 = 禁发）。
    # 漏配的后果必须是"发不出去"，而不是"能发给任何人"。
    mail_enabled: bool = Field(default=False, description="邮件发送总开关。false 时 mail.send 一律拒绝")
    mail_dry_run: bool = Field(default=True, description="只记录不投递。开发期默认开，要真发必须显式关掉")
    mail_from: str = Field(default="agent@fashionops.local", description="发件人地址")
    mail_send_allowlist: str = Field(
        default="",
        description=(
            "收件人白名单，逗号分隔。支持三种写法：``*``（全部）/ "
            "``@corp.com``（整个域）/ ``a@b.com``（精确）。**留空 = 禁止发送任何邮件**"
        ),
    )
    mail_send_denylist: str = Field(default="", description="收件人黑名单，逗号分隔。优先级高于白名单")
    mail_max_recipients: int = Field(default=10, description="单封邮件收件人上限（防群发）")
    mail_rate_limit_per_minute: int = Field(default=6, description="每收件人每分钟最多收几封。防模型陷入循环反复发")
    mail_max_attachment_bytes: int = Field(default=20 * 1024 * 1024, description="单个附件大小上限（字节）")
    mail_attachment_allowlist: str = Field(
        default=".xlsx,.pdf,.csv,.png,.jpg,.jpeg",
        description="允许外发的附件扩展名，逗号分隔。留空 = 不限制（不建议）",
    )

    # ---- SMTP ----
    # 只用于真实投递。开发期 mail_dry_run=true 时完全用不到这些。
    smtp_host: str = Field(default="", description="SMTP 服务器。留空则只能 dry_run")
    smtp_port: int = Field(default=465, description="465=SSL 直连，587=STARTTLS")
    smtp_user: str = Field(default="", description="SMTP 登录名，一般是发件邮箱全称")
    smtp_password: str | None = Field(
        default=None,
        exclude=True,
        repr=False,
        description="SMTP 授权码。**填邮箱服务商生成的授权码，不是登录密码**",
    )
    smtp_use_ssl: bool = Field(default=True, description="true 用 SMTP_SSL（465），false 用 STARTTLS")
    smtp_timeout: float = Field(default=20.0, description="SMTP 连接/发送超时秒数")

    # ---- 认证 / 会话 ----
    # 默认值的选取原则和邮件闸门一样：**漏配的后果必须是"进不来"，不能是"谁都能进"**。
    # 所以 auth_required 默认 True —— 反过来的话，一次忘记配置就等于全站裸奔。
    auth_required: bool = Field(
        default=True,
        description=(
            "是否强制鉴权。true 时所有 /api/v1 接口都要求登录（/health 与 /docs 除外）。"
            "本地纯演示可以关掉，但**生产必须为 true**"
        ),
    )
    auth_allow_anonymous_scopes: str = Field(
        default="",
        description="允许匿名访问的只读路径前缀，逗号分隔。留空表示除白名单外一律要登录",
    )
    jwt_secret: str = Field(
        default="",
        description=(
            "JWT 签名密钥。**留空时启动会自动生成一个随机值并打 warning** ——"
            " 这样服务能起来（不会因为忘了配就起不来），但重启后所有 token 失效，"
            '所以生产必须显式配。生成：python -c "import secrets;print(secrets.token_urlsafe(48))"'
        ),
    )
    jwt_algorithm: str = Field(default="HS256", description="HS256（对称）够用；上 RS256 要配公私钥")
    access_token_minutes: int = Field(
        default=30, description="访问令牌有效期（分钟）。短一些是对的：它无状态、撤不回来"
    )
    refresh_token_days: int = Field(default=14, description="刷新令牌有效期（天）。它入库可撤销，所以可以长一些")

    # ---- 登录安全 ----
    password_min_length: int = Field(default=10, description="密码最短长度")
    password_require_classes: int = Field(
        default=2,
        description="至少要包含几类字符（小写/大写/数字/符号）。2 类兼顾安全和可用性",
    )
    login_max_failures: int = Field(default=5, description="连续登录失败几次后锁定账号。防在线暴力破解")
    login_lock_minutes: int = Field(default=15, description="锁定时长（分钟）")
    login_rate_limit_per_minute: int = Field(
        default=20,
        description=(
            "同一 IP + 用户名每分钟最多尝试几次登录。**和账号锁定是互补的两层**："
            "锁定防「盯着一个账号爆」，限流防「拿着一批账号扫」（碰库时每个账号只试一次，"
            "永远触发不了锁定）"
        ),
    )

    # ---- 审批职责分离 ----
    approval_require_separate_user: bool = Field(
        default=False,
        description=(
            "批准时是否禁止「自己批自己提交的任务」。**默认关**：小团队里提交人往往就是"
            "唯一有审批权的人，硬拦会让流程走不动。有合规要求（财务/对外发信）时应打开"
        ),
    )

    # ---- 审批有效期 ----
    approval_ttl_minutes: int = Field(
        default=120,
        description=(
            "确认单的有效期（分钟），超过后自动作废、不能再点。**默认 120**："
            "审批的全部意义是「这一次、这个内容，人同意了吗」，隔了两小时数据已经变了，"
            "那时点下的「同意」答的不是同一个问题。设 0 表示永不过期"
        ),
    )

    # ---- 对话上下文预算（见 chat/memory.py）----
    chat_context_messages: int = Field(
        default=40,
        description=(
            "喂给模型的对话**原文**条数上限（最近 N 条）。超出的部分会被压缩成摘要，"
            "不是直接丢掉。**要长记忆请调摘要策略，别调大这个数** —— "
            "它让每轮请求的 token 成本线性上涨，而摘要不会"
        ),
    )
    chat_summary_refresh_every: int = Field(
        default=10,
        description=(
            "攒够多少条「新掉出窗口」的消息才重做一次摘要。这是成本与新鲜度的平衡："
            "设 1 = 每轮都刷（最新，但每轮多一次模型调用）；设很大 = 省钱但摘要会旧"
        ),
    )
    chat_old_tool_result_chars: int = Field(
        default=1200,
        description=(
            "回放旧工具结果时的截断长度（字符）。工具返回的大段 JSON 每轮都要重发一次，"
            "而其中多数已经不再被需要。设 0 = 不截断"
        ),
    )

    # ---- 长期记忆（见 chat/longterm.py）----
    chat_memory_enabled: bool = Field(
        default=True,
        description=(
            "是否让对话 Agent 跨会话记住用户偏好（`remember` 工具 + 每轮注入）。"
            "关掉之后 `remember` 调用会被拒绝，已存的记忆也不再注入 —— "
            "给「不想让助手记住任何东西」的场景留的开关"
        ),
    )
    chat_memory_inject_limit: int = Field(
        default=20,
        description=(
            "每轮最多注入多少条长期记忆（按最近更新倒序）。它不是存储上限，"
            "只是注入上限 —— 记忆每条都是一句话，但这个数字直接进了 system prompt，"
            "调大要相应接受每轮的固定 token 成本"
        ),
    )

    # ---- 初始管理员 ----
    # 首次启动没有用户就没法登录，所以要在建表后创建第一个管理员。
    # 密码留空则**不创建**，改由 `fashionops-user create-admin` 显式创建 ——
    # 自动生成一个随机密码打印在日志里，比不创建更危险（日志会被采集走）。
    bootstrap_admin_username: str = Field(default="", description="首次初始化时创建的管理员用户名")
    bootstrap_admin_password: str | None = Field(
        default=None, exclude=True, repr=False, description="上面那个管理员的初始密码"
    )

    # ---- 钉钉 / 企业微信扫码登录 ----
    # ⚠ 当前**只有接口和配置位，没有真实调用**（见 security/oauth.py 的说明）。
    # 留成"能配但不用"而不是删掉：真接入时只需要填这四个值 + 一个 provider 实现，
    # 不用再改数据模型和前端流程。
    dingtalk_client_id: str = Field(default="", description="钉钉应用 AppKey")
    dingtalk_client_secret: str | None = Field(default=None, exclude=True, repr=False, description="钉钉应用 AppSecret")
    dingtalk_corp_id: str = Field(default="", description="钉钉企业 CorpId")
    wecom_corp_id: str = Field(default="", description="企业微信 CorpId")
    wecom_agent_id: str = Field(default="", description="企业微信应用 AgentId")
    wecom_secret: str | None = Field(default=None, exclude=True, repr=False, description="企业微信应用 Secret")
    oauth_redirect_base: str = Field(
        default="http://localhost:3000/oauth/callback",
        description="扫码登录后前端回调地址。**必须在第三方后台登记过**，否则会被拒",
    )

    # ---- 工具网关 ----
    gateway_rate_limit_per_minute: int = Field(default=120, description="工具调用每分钟上限（按工具名分别计数）")
    gateway_max_retries: int = Field(default=2, description="外部调用的最大重试次数")
    gateway_circuit_breaker_threshold: int = Field(default=5, description="连续失败多少次后熔断该工具")
    gateway_circuit_breaker_cooldown: int = Field(default=60, description="熔断冷却秒数")

    # ---- 可观测性 ----
    otel_enabled: bool = Field(default=False, description="是否开启 OpenTelemetry 链路追踪")
    otel_endpoint: str | None = None
    otel_service_name: str = "fashionops"

    # ---- 外部 MCP 服务器（office 等，见 integrations/office.py）----
    # 默认全关：开了才拉起子进程接入。两种方式二选一（见 office.py 说明）：
    #   1) office_mcp_servers_json 直接给 JSON 数组；
    #   2) office_mcp_cwd 指向 mcp-servers 目录，按约定拉起三个 office 模块。
    office_mcp_enabled: bool = Field(default=False, description="是否把外部 MCP 服务器（office 等）接入工具网关")
    office_mcp_servers_json: str | None = Field(
        default=None,
        description="JSON 数组，描述要接入的 MCP 服务器（command/args/cwd/env/scopes 等）",
    )
    office_mcp_cwd: str | None = Field(
        default=None,
        description="mcp-servers 目录；设了就按约定拉起 office-docs/email/calendar 三个服务器",
    )
    office_mcp_python: str = Field(
        default="python",
        description="拉起 office 服务器用的解释器（通常指向 mcp-servers 的 venv 里的 python）",
    )
    office_mcp_workspace: str | None = Field(default=None, description="传给 office 服务器的 OFFICE_MCP_WORKSPACE_ROOT")

    # ---- DeerFlow（LangGraph 编排层，sidecar @ :8011）----
    # 对话侧编排引擎的可切换项：`legacy` 走平台自研的 run_tool_loop；
    # `deerflow` 走 DeerFlow 的 LangGraph 图（作为 sidecar 进程跑在 :8011）。
    # 用配置开关切换，便于灰度与一键回滚（见 chat/agent.py）。
    orchestrator_backend: str = Field(
        default="legacy",
        description="对话编排引擎：``legacy``（平台自研）/ ``deerflow``（DeerFlow LangGraph sidecar）",
    )
    # DeerFlow Gateway 的 LangGraph 兼容 API 基址（不含末尾斜杠）。
    #
    # 路径是 `/api` 而**不是** `/api/langgraph`：DeerFlow 把线程与 run 路由挂在
    # `/api/threads` 下（见 backend/app/gateway/routers/thread_runs.py 的
    # `APIRouter(prefix="/api/threads")`），本适配层的请求是
    # `{base}/threads` 和 `{base}/threads/{id}/runs/stream`。
    # 多写一段 `/langgraph` 会 404，而 404 在流式接口上表现为"连上了但什么都没吐"，
    # 很难一眼看出是路径配错。DeerFlow README 里的 `langgraph_url` 指的是同一个东西。
    deerflow_base_url: str = Field(
        # ⚠ 默认 8011 而非 8001：本机另一个股票系统的 Java 后端常驻 8001。
        default="http://127.0.0.1:8011/api",
        description="DeerFlow sidecar 的 LangGraph API 基址（SSE 流与 thread/run 管理）",
    )
    # sidecar 间 server-to-server 调用用的内部令牌（对应 DeerFlow 的
    # DEER_FLOW_INTERNAL_AUTH_TOKEN）。不走 PAT 的 allowlist，也不带 CSRF。
    deerflow_internal_token: str | None = Field(
        default=None, exclude=True, repr=False, description="DeerFlow 内部鉴权令牌（X-DeerFlow-Internal-Token）"
    )
    # 以哪个平台用户身份在 DeerFlow 侧归属这次编排（用于 DeerFlow 的 owner 维度）。
    deerflow_owner_user_id: str = Field(
        default="deerflow", description="DeerFlow 侧归属用户标识（X-DeerFlow-Owner-User-Id）"
    )

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

    # ---- 邮件派生属性 ----
    @property
    def mail_allowlist(self) -> tuple[str, ...]:
        return _split_csv(self.mail_send_allowlist)

    @property
    def mail_denylist(self) -> tuple[str, ...]:
        return _split_csv(self.mail_send_denylist)

    @property
    def mail_attachment_exts(self) -> tuple[str, ...]:
        return tuple(
            e.lower() if e.startswith(".") else f".{e.lower()}" for e in _split_csv(self.mail_attachment_allowlist)
        )

    @property
    def mail_can_really_send(self) -> bool:
        """能不能真的投递出去（三个条件都满足才行）。"""
        return self.mail_enabled and not self.mail_dry_run and bool(self.smtp_host)

    def mail_policy_summary(self) -> dict[str, object]:
        """给 ``/health`` 和模型看的策略摘要 —— 不含任何密码。"""
        return {
            "enabled": self.mail_enabled,
            "dry_run": self.mail_dry_run,
            "from": self.mail_from,
            "allowlist": list(self.mail_allowlist),
            "denylist": list(self.mail_denylist),
            "max_recipients": self.mail_max_recipients,
            "rate_limit_per_minute": self.mail_rate_limit_per_minute,
            "max_attachment_bytes": self.mail_max_attachment_bytes,
            "attachment_exts": list(self.mail_attachment_exts),
            "smtp_host": self.smtp_host or "（未配置，只能 dry_run）",
            "smtp_configured": self.mail_can_really_send,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例，避免重复解析环境变量。"""
    settings = Settings()
    # .env 里额外指定了要绕过代理的地址时，在这里并入 NO_PROXY
    ensure_localhost_bypasses_proxy(settings.no_proxy_extra)
    return settings
