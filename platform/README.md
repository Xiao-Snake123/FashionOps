# 服装电商多 Agent 运营中台

> 一句话：把「秋季女装上新」这件事，从选品到投放到复盘，交给 9 个 Agent 自动编排完成，
> 全程可追踪、可干预、可评测。

---

## 先看效果（演示账号）

**打开 <http://localhost:5173>，用这个账号登录：**

| 用户名 | 密码 | 角色 |
| --- | --- | --- |
| `demo` | `FashionOps@2026` | `admin`（全部权限） |

> ⚠ **这是本地演示账号，只用于跑起来的开发环境。**
> 它明文写在 README 里，所以**绝不能在任何公开部署上使用它** ——
> 真要部署，先用下面的命令把它删掉或改密。
>
> 同理，**真实凭据一律不进仓库**：数据库密码、邮箱授权码、模型 API Key
> 全部只在 `platform/.env` 里，而 `.env` 在 `.gitignore` 里。仓库里还有一道
> pre-commit 钩子（`no-hardcoded-secrets`）会在提交时扫 `.md` / `.py` / `.json`，
> 防止这类东西被误提交。

登录后建议按这个顺序看：

| 页面 | 看什么 |
| --- | --- |
| **对话助手** | 直接说一句话（"SKU00042 还有多少库存？"），看流式回答 + 工具调用过程 |
| 对话里说"给 x@y.com 发封邮件…" | 会**停下来弹一张确认卡片**，点了才真发 —— 写操作的人工闸口 |
| 对话里说"帮我选 3 款秋季女装并出报表" | 转成一个**真实任务**，给一张能点进任务页的卡片 |
| **任务工作台** → 点任务 | DAG 图、SSE 实时事件流、审批横幅、产物下载 |
| 客服工单 / 商品库 / 广告投放 / 邮件闸门 | 业务数据展示；邮件闸门页能看到被拦下的邮件及拦截原因 |

### 自己加账号

```powershell
# 建用户（密码留空会自动生成并打印一次）
.venv\Scripts\fashionops-user.exe create 张三 --role operator --full-name "张三"
# 改密码（会撤销该用户全部会话）
.venv\Scripts\fashionops-user.exe passwd demo
# 看某个用户"实际生效"的权限（角色改了但它不生效时先看这个）
.venv\Scripts\fashionops-user.exe show 张三
```

角色与权限的区别（`operator` 刻意**不能审批、不能发信**）见
`src/fashionops/security/permissions.py` 顶部的说明。

---

## 这是什么

一个**多 Agent 编排平台**（不是单体 Agent 应用），核心是三个东西：

1. **总控编排 Agent** —— 把一句自然语言需求拆成 DAG，路由给垂直 Agent，收敛结果，异常熔断，高危操作停下来等人确认。
2. **9 个垂直业务 Agent** —— 选品 / 图文生成 / 视频生成 / 商品上架 / 广告投放 / 智能客服 / 数据分析 / 邮件通知。
3. **工具网关层** —— 所有外部调用（电商 API、广告 API、ERP、SMTP）收口到一层，统一限流、重试、熔断、审计、凭证隔离。

## 技术栈

| 层 | 选型 |
| --- | --- |
| 后端 | FastAPI + uvicorn + SQLAlchemy 2.0 + Pydantic v2 |
| 关系库 | **MySQL 8.0**（本地实例） |
| 缓存 / 任务状态 | **Redis 8.x**（AOF 持久化，支撑任务中断恢复） |
| 向量库 | **Qdrant 1.x**（客服 RAG 混合检索） |
| Agent 编排 | LangGraph + langgraph-checkpoint-redis + langchain-core |
| 前端 | React + Vite + React Flow（DAG 可视化）+ ECharts |
| 可观测性 | OpenTelemetry + OpenLLMetry |
| 持久化执行 | DBOS |

外部系统（电商平台 / 广告平台 / ERP）全部走 **MockAdapter**，开发期零真实调用；
通过配置开关即可切到真实实现，不需要改业务代码。

## 目录结构

```
platform/
├─ src/fashionops/
│  ├─ config.py          # 全部配置（前缀 FASHIONOPS_）
│  ├─ db/                # 引擎、会话、14 张业务表模型
│  ├─ seed/              # 假数据生成（故意埋异常数据）
│  ├─ gateway/           # 工具网关：限流 / 重试 / 熔断 / 审计 / 凭证
│  ├─ adapters/          # 外部系统可插拔适配器（Mock / Real）
│  ├─ agents/            # 9 个 Agent + 总控编排
│  └─ api/               # FastAPI 路由 + SSE
├─ deploy/
│  ├─ scripts/           # 服务启停、MySQL 初始化
│  ├─ mysql/             # 建库建号 SQL
│  └─ redis/             # Redis 配置
└─ frontend/             # React + Vite 工作台
```

## 快速开始

### 1. 启动数据层服务

```powershell
cd deploy\scripts
.\services.ps1 start          # 启 Redis + Qdrant
.\services.ps1 status         # 看三个服务状态
```

### 2. 初始化 MySQL（只需跑一次）

```powershell
cd deploy\scripts
.\init-mysql.ps1              # 建库 + 建专用账号 + 自动写入 .env
```

脚本会交互式询问你的 MySQL root 密码（**密码只在本机终端内使用，不会被上传**），
然后自动生成一个应用专用账号密码并写进 `.env`。

### 3. 装依赖 + 建表 + 灌假数据

```powershell
uv sync --all-extras
uv run fashionops-db init
uv run fashionops-seed
```

### 4. 起后端

```powershell
uv run uvicorn fashionops.api.main:app --reload --port 8000
```

打开 <http://127.0.0.1:8000/docs> 看接口，<http://127.0.0.1:8000/health> 看依赖连通性。

### 5. 起前端

```powershell
cd frontend
pnpm install
pnpm dev
```

## 配置说明

所有配置走环境变量，前缀 `FASHIONOPS_`，也可以写在 `platform/.env`。
完整清单见 `.env.example`。

**关于模型**：`FASHIONOPS_MODEL_PROVIDER` 默认是 `mock` —— 不填任何 API Key
也能把全链路跑通（返回结构合法的假响应）。要接真实模型再改成 `deepseek` / `qwen` / `zhipu`。

## 设计原则

1. **外部依赖全部可插拔**：关系库、向量库、缓存、对象存储、外部 API 各留一个适配器接口，
   开发期用本地实现，生产只换适配器类，业务代码不动。
2. **假数据必须埋异常**：库存不足、ROI 暴跌、纠纷工单 —— 不然演示不出熔断和转人工的价值。
3. **高危操作必须有闸口**：上架、改预算这类动作走 LangGraph 的 `interrupt()`，等人确认。

## 相关文档

- `../docs/07-服装电商多Agent运营中台-项目规划.md` —— 完整项目规划、分期、技术点矩阵
- `../docs/08-数据层安装记录.md` —— 本机装了什么、装在哪、怎么启停、怎么卸
- `../mcp-servers/` —— 独立可交付的办公 MCP 工具服务（23 个工具）
