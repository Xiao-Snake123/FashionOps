# 服装电商多 Agent 运营中台

> 一句话：把「秋季女装上新」这件事，从选品到投放到复盘，交给 9 个 Agent 自动编排完成，
> 全程可追踪、可干预、可评测。

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
