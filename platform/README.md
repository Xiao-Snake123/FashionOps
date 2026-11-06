# 服装电商多 Agent 运营中台

> 一句话：运营说一句「帮我选 3 款秋季女装并出报表发给运营组」，
> 系统自己拆成有序步骤、逐步执行，涉及对外发信的步骤**停下来等人点确认**，
> 全程可追踪、可干预、可评测。

---

## 一、怎么把它跑起来

### 最快：一条命令（推荐）

```powershell
cd "c:\Users\Administrator\Desktop\Work agent\platform\deploy\scripts"
.\start.ps1
```

它会按顺序把整个项目拉起来，并且**每一步都幂等**（已经在跑的就跳过，
所以可以放心重复执行）：

1. 数据层：Redis + Qdrant（复用 `services.ps1`）
2. 校验数据库迁移版本（库里没有迁移记录就自动 `fashionops-db init`）
3. 后端 uvicorn（后台进程，日志在 `D:\develop\logs\backend.out.log`）
4. MCP 服务：把平台工具以 MCP/SSE 对外暴露（`:8100`）
5. DeerFlow sidecar（**可选，默认不起**，见下）
6. 前端 Vite（<http://localhost:3000>）

日志统一写在 `D:\develop\logs\`：`backend.out.log` / `mcp.out.log` /
`deerflow.out.log` / `frontend.out.log`。

| 服务 | 地址 | 说明 |
|---|---|---|
| 前端 | <http://localhost:3000> | **只能**用 `localhost`（走 IPv6 `::1`） |
| 后端 | <http://127.0.0.1:8010/docs> | 接口文档 |
| 健康检查 | <http://127.0.0.1:8010/health> | 三个依赖是否都通 |
| MCP/SSE | <http://127.0.0.1:8100/mcp/sse> | 外部 Agent 连这里用平台工具 |
| DeerFlow | <http://127.0.0.1:8011> | 可选编排后端，默认不起 |

> ⚠ 端口刻意避开 **8000 / 8001**：本机另一个股票系统把这两个当默认端口
> （FastAPI/WebUI 占 8000、Java Spring Boot 占 8001）。清单见
> [`docs/14-端口清单.md`](docs/14-端口清单.md)。

常用参数：

```powershell
.\start.ps1 -BackendPort 8020     # 8010 被别的程序占了时
.\start.ps1 -NoFrontend           # 只起后端（前端你自己开）
.\start.ps1 -NoMcp                # 不起 MCP 服务
.\start.ps1 -SkipData             # Redis/Qdrant 由外部管理（比如跑在 Docker 里）
.\start.ps1 -WithDeerFlow         # 连 DeerFlow sidecar（:8011）一起起
```

**停止**（只停应用层）：

```powershell
.\stop.ps1
```

要连 Redis / Qdrant 一起停：

```powershell
.\stop.ps1 -All
```

> ⚠ `stop.ps1` 是**按端口**定位进程的（没记 PID —— PID 文件在机器重启后就是
> 过期数据，还会误杀）。找到占用者后会校验进程名，名字不对就不动它。

> ⚠ 同一个坑在**启动侧**也存在，而且更隐蔽：只探端口会被别的程序骗过，
> 于是"服务起来了吗"的答案永远是"起来了"，脚本跳过启动，**什么都不做还不报错**。
> 本机实测撞到过两次：
>
> - `:8001` 常驻那个股票系统的 Java 后端（`dsa-server.jar`）；
> - `:8000` 被那个股票系统的 **python** 占着（`main.py --webui-only`）——
>   这种情况下连"进程名是不是 python"都救不了，**冒名者同样叫 python.exe**。
>
> 所以 `start.ps1` 对后端 / MCP / DeerFlow / 前端四处都查**命令行特征**
> （分别是 `fashionops.api.main` / `fashionops.mcp.server` / `app.gateway.app` / `vite`），
> 对不上就红字报「被 xxx (PID n) 占着，不是 XXX」并把它**的命令行打出来**，
> 同时**不再打印该服务的访问地址** —— 免得你去访问一个其实是别人的端口，
> 然后对着 404 查半天。
>
> **这也是本项目默认端口从 8000/8001 挪到 8010/8011 的原因** ——
> 跟那个股票系统的默认端口彻底错开，两边可以同时跑。

### 可选：DeerFlow 编排后端（默认不启用）

平台默认用**自研编排**（代码规划 + 显式 todo），**不依赖** DeerFlow。
DeerFlow 是可选的第二种编排后端：

```powershell
.\start.ps1 -WithDeerFlow
```

或者把 `platform/.env` 里的 `FASHIONOPS_ORCHESTRATOR_BACKEND` 改成 `deerflow`
（脚本会读到，自动跟着起）。

链路是**双向**的：平台 → DeerFlow（`:8011`）→ 平台 MCP（`:8100`）→ 平台工具。
外部 Agent 调写操作（如 `mail.send`）时走审批闸口，工具**不会被执行**，
只回 `needs_approval` —— 平台始终掌握"哪些动作需要人点确认"。

默认不起的两个理由：① 它有自己的 venv / `config.yaml` / `.env`，不是每台机器都有；
② 默认 `orchestrator_backend=legacy`，这时候起了也**没人调它**。
DeerFlow 不在 `platform/` 同级目录时，用 `-DeerFlowRoot` 指定位置。

### 首次运行前要做的两件事

一键脚本不管这两件（它们只需要做一次）：

```powershell
cd "c:\Users\Administrator\Desktop\Work agent\platform\deploy\scripts"
.\init-mysql.ps1          # 1) 建库 + 建专用账号 + 自动写入 .env
cd "c:\Users\Administrator\Desktop\Work agent\platform"
.venv\Scripts\fashionops-seed.exe   # 2) 灌演示数据（会自动补回演示账号）
```

### 想分步手动跑

> 下面每条命令都在本机（Windows 11 + PowerShell）实测过。
> 顺序很重要：**依赖服务 → 建库 → 建表灌数据 → 起后端 → 起前端**。

> ⚠ **下面所有命令都假设"当前目录是 `platform/`"，不是仓库根目录。**
> 仓库根目录下**没有** `deploy\` —— 在根目录执行 `cd deploy\scripts` 会报
> "找不到路径"。先 `cd platform` 再往下走（路径里有空格，引号别省）：
>
> ```powershell
> cd "c:\Users\Administrator\Desktop\Work agent\platform"
> ```

### 0. 前提

| 需要 | 版本 | 端口 |
|---|---|---|
| Python | 3.12（仓库自带 `.venv`） | — |
| Node.js | 18+（本机 24） | — |
| MySQL | 8.0 | 3306 |
| Redis | 8.x（限流 / 熔断 / 缓存） | 6379 |
| Qdrant | 1.x（知识库向量检索） | 6333 |

Redis 和 Qdrant 由仓库脚本管理：

```powershell
cd "c:\Users\Administrator\Desktop\Work agent\platform\deploy\scripts"
.\services.ps1 start      # 启 Redis + Qdrant
.\services.ps1 status     # 看状态（MySQL 也一起显示）
```

### 1. 初始化 MySQL（只需一次）

```powershell
cd "c:\Users\Administrator\Desktop\Work agent\platform\deploy\scripts"
.\init-mysql.ps1          # 建库 + 建专用账号 + 自动写入 .env
```

脚本会交互询问 MySQL root 密码（**只在本机终端使用**），然后生成应用账号密码写进 `.env`。

### 2. 装依赖 · 建表 · 灌假数据

```powershell
cd "c:\Users\Administrator\Desktop\Work agent\platform"

# 装依赖（二选一：装了 uv 用前者，没有就用后者）
# 仓库里已经有 .venv，通常不用重装；换了 Python 版本或重装环境时才需要
uv sync --all-extras
.venv\Scripts\python.exe -m pip install -e ".[dev]"

# 建表 / 升级到最新迁移版本（幂等，可反复跑）
.venv\Scripts\fashionops-db.exe init

# 灌假数据
.venv\Scripts\fashionops-seed.exe
```

`fashionops-seed` 会**清空并重建业务表**，同时按新架构生成：
任务的步骤（`task_todos`）、事件流、Agent 运行记录、停在人工闸口的任务与审批单、
以及三条对话演示数据（覆盖"直接答 / 建任务 / 停在闸口"）。

> ⚠ 它也会重建身份表，所以脚本内部会**自动补回演示账号**
> （`demo` / `ops_manager` / `operator`，密码都是 `FashionOps@2026`）——
> 这一步是必要的：早期版本灌完库登不进去（401），看起来像鉴权坏了。

### 3. 起后端

```powershell
cd "c:\Users\Administrator\Desktop\Work agent\platform"

# ⚠ 必须用 Start-Process 起：直接在前台跑的话，终端会话一结束进程就被带走，
#    表现是"刚才还好好的，一会儿接口全 404"
Start-Process -FilePath ".venv\Scripts\python.exe" `
  -ArgumentList "-m","uvicorn","fashionops.api.main:app","--host","127.0.0.1","--port","8010" `
  -WindowStyle Hidden

# 改代码调试时加 --reload（前台跑就行，反正要盯着日志）
.venv\Scripts\python.exe -m uvicorn fashionops.api.main:app --reload --port 8010
```

自检：**<http://127.0.0.1:8010/health>**（注意是根路径，不是 `/api/v1/health`）

```json
{"status": "degraded", "dependencies": [
  {"name": "database", "ok": true,  "detail": "MySQL 8"},
  {"name": "redis",    "ok": false, ...},
  {"name": "qdrant",   "ok": false, ...}]}
```

- `ok`：三个都通
- `degraded`：Redis / Qdrant 没起 —— **项目照样能跑**，只是限流熔断走降级、
  知识库检索用不了（见"排错"）

接口文档：<http://127.0.0.1:8010/docs>

### 4. 起前端

```powershell
cd frontend
npm install      # 首次
npm run dev      # http://localhost:3000
```

- 前端**只能用 `http://localhost:3000`**（走 IPv6 `::1`），`127.0.0.1` 连不上
- `/api` 与 `/health` 由 Vite 代理到 `http://127.0.0.1:8010`（默认值，见 `vite.config.ts`）；
  后端换了端口时用 `VITE_API_TARGET` 改：`$env:VITE_API_TARGET="http://127.0.0.1:8020"`
- 生产构建：`npm run build`（= `tsc --noEmit` + `vite build`）

### 5. 打开看效果

**<http://localhost:3000>** → 用演示账号登录：

| 用户名 | 密码 | 角色 |
|---|---|---|
| `demo` | `FashionOps@2026` | `admin`（全部权限） |
| `ops_manager` | `FashionOps@2026` | 运营主管（**能审批、能发信**） |
| `operator` | `FashionOps@2026` | 运营专员（**不能审批、不能发信**） |

> ⚠ 这是**本地演示账号**，明文写在 README 里，**绝不能用于任何公开部署**。
> 真实凭据（数据库密码、邮箱授权码、模型 API Key）只在 `platform/.env`，
> 而 `.env` 在 `.gitignore` 里；仓库还有 pre-commit 钩子扫硬编码密钥。

### 6. 可选：把知识库灌进 Qdrant

客服 RAG 的向量在 Qdrant 里，不在 MySQL。Qdrant 起来后：

```powershell
.venv\Scripts\fashionops-kb.exe index    # 建索引（幂等）
.venv\Scripts\fashionops-kb.exe stats    # 看索引状态
.venv\Scripts\fashionops-kb.exe search "连衣裙退货政策"   # 检查召回效果
```

### 7. 跑测试

```powershell
.venv\Scripts\python.exe -m pytest -q                 # 全量（约 3 分钟）
.venv\Scripts\python.exe -m pytest tests/test_loop.py -q --tb=line
.venv\Scripts\ruff.exe check src tests scripts        # 静态检查
```

> 单元测试用**临时 SQLite**（`tests/conftest.py` 的 `db` 夹具），不碰开发库；
> 只有 `tests/test_api_mysql.py` 故意打真实 MySQL —— 它专抓 SQLite 上不复现的问题。

### 8. 停服务

```powershell
cd deploy\scripts
.\services.ps1 stop
```

---

## 二、命令速查

| 想干什么 | 命令 |
|---|---|
| 建表 / 升级 | `.venv\Scripts\fashionops-db.exe init` |
| 看库里各表行数 | `.venv\Scripts\fashionops-db.exe info` |
| 生成新迁移 | `.venv\Scripts\fashionops-db.exe revision` |
| 灌假数据 | `.venv\Scripts\fashionops-seed.exe`（`--keep` 追加、`--seed 42` 换数据） |
| 起 Redis / Qdrant | `deploy\scripts\services.ps1 start` |
| 起后端 | `uvicorn fashionops.api.main:app --port 8010` |
| 起前端 | `npm run dev`（3000） |
| 起 MCP 服务 | `uvicorn fashionops.mcp.server:create_sse_app --factory --port 8100` |
| 起 DeerFlow | `deploy\scripts\start.ps1 -WithDeerFlow`（可选，`:8011`） |
| 建用户 / 改密 / 看权限 | `.venv\Scripts\fashionops-user.exe create\|passwd\|show` |
| 补角色预设权限 | `.venv\Scripts\fashionops-user.exe sync --apply-presets` |
| 知识库建索引 | `.venv\Scripts\fashionops-kb.exe index` |
| 批量处理工单 | `.venv\Scripts\fashionops-ticket.exe handle-pending` |
| 体检配置 | `.venv\Scripts\fashionops-config.exe doctor` |

---

## 三、看什么（页面导览）

| 页面 | 看什么 |
|---|---|
| **对话助手** | 说一句话直接问（"SKU00042 还有多少库存？"）—— 流式回答 + 工具调用过程；底部**分析模板**一键触发常见分析（库存 / 退货 / 广告 ROI） |
| 对话里说"给 x@y.com 发封邮件…" | **停下来弹一张确认卡片**，点了才真发（人工闸口） |
| 对话里说"帮我选 3 款秋季女装并出报表" | 转成**真实任务**，给一张能点进任务页的卡片 |
| **任务工作台** → 点任务 | **有序的步骤列表**（每步的状态 / 结论 / 用过的工具 / 耗时 / 产出），SSE 实时事件流；**关联单据**区直接跳到它动过的广告计划 / 邮件 / 商品 / 工单 |
| 任务停在闸口时 | 审批横幅显示"第几步 / 哪个工具 / 审批单号 + 人话摘要（收件人、主题、正文）" |
| **数据看板** | GMV / 退货率 / 广告 ROI / 库存告急 + Redis·Qdrant 连通状态；**类目退货率图带行业基准带**，一眼看出哪类在拖高整体 |
| **商品库** | SKU 与库存；冬款缺货**高亮预警**；可从商品一键发起"补货"任务（跨模块联动） |
| **广告投放** | 亏损计划标红；**就地批准 / 驳回 / 一键止损 / 改预算**（改预算属高危，走人工确认闸口；止损是唯一免审批的写操作） |
| **客服工单** | 情绪 / 金额风险标注；**派单 / 改派 / 人工进度**（阶段 + 备注），看板按未派单 / 待接单 / 各人工阶段分组 |
| **邮件闸门** | 被拦下的邮件及拦截原因；**改白名单 / 演练转真发 / 重发**（重发会重新过一遍闸门） |
| **账号管理** | 分级授权；**异常登录审计**（异地 / 新 IP / 爆破后成功），只看 IP 是否变化、不做归属地判断 |

---

## 四、它现在是怎么工作的

```
用户一句话
   ↓
agents/workplan.py   代码按关键词拆成**有序步骤**（选品 → 分析 → 通知）
   ↓                 命中 0~1 类意图 → 单步直接做；≥2 类才拆多步
agents/loop.py       一个 Agent 逐步执行：每步按 kind 裁剪工具目录
   ↓                 算数和判定走代码（scoring / analytics_rules），表达交给模型
   ↓                 要发邮件等不可撤销的动作 → 落审批单 → 停下来等人（不阻塞线程）
   ↓
agents/recorder.py   旁路记账：事件流（SSE）+ agent_runs + 任务主表
```

**刻意没有的东西**（都是删掉的旧架构）：模型出 DAG、图校验与环检测、
hub 路由、9 个专用 Agent 节点、LangGraph checkpoint。
理由与证据见 [`docs/12-AI协作交接.md`](docs/12-AI协作交接.md)。

> LangGraph 后来以 **DeerFlow sidecar** 的形式回来了，但只是**可选**的
> 第二种编排后端，默认不启用 —— 默认路径仍然走上面这套自研编排。

---

## 五、技术栈

| 层 | 选型 |
|---|---|
| 后端 | FastAPI + uvicorn + SQLAlchemy 2.0 + Pydantic v2 |
| 关系库 | MySQL 8.0（29 张表，Alembic 管理） |
| 缓存 / 限流 / 熔断 | Redis 8.x |
| 向量库 | Qdrant 1.x（客服 RAG） |
| 执行编排 | **自研**：一个 Agent + 显式 todo 列表（不再依赖 LangGraph） |
| 工具协议 | MCP（stdio + SSE 双传输）—— 对外暴露平台工具，也反向消费外部 MCP 服务 |
| 可选编排 | DeerFlow（LangGraph）sidecar，默认不启用 |
| 前端 | React + Vite + ECharts |
| 模型 | OpenAI 兼容接口（默认 `mock`，不填 Key 也能跑通全链路） |

外部系统（电商 / 广告 / ERP）走 **MockAdapter**，开发期零真实调用，靠配置切换。

---

## 六、配置

所有配置走环境变量，前缀 `FASHIONOPS_`，也可写在 `platform/.env`（清单见 `.env.example`）。

**关于模型**：`FASHIONOPS_MODEL_PROVIDER` 默认 `mock` —— 返回结构合法的假响应，
不填 API Key 也能把全链路跑通；接真实模型改成 `deepseek` / `qwen` / `zhipu`。

---

## 七、排错

| 现象 | 原因 / 怎么办 |
|---|---|
| `/health` 是 `degraded`，redis / qdrant 报 10061 | 依赖服务没起 → `deploy\scripts\services.ps1 start`。**不影响主流程**，只是限流降级、知识库检索不可用 |
| 后端起不来：`[Errno 10048]` 端口被占 | 换端口 `--port 8020`，并给前端设 `$env:VITE_API_TARGET` |
| 前端能开但一直 401 / 接口全 404 | 后端没在 8010（或进程被终端带走了）→ 用 `Start-Process` 重启后端 |
| 灌完库登不进去 | 老版本 bug，已修；现在 seed 会自动补回演示账号。真没有就跑 `fashionops-user create demo --role admin` |
| `pytest` 有一批 skip | 全是 Redis / Qdrant 没起导致的，不是代码问题 |
| 任务详情页"没有步骤记录" | 那是旧架构灌的任务（计划存在 `tasks.dag`）；重新跑 `fashionops-seed` 即可 |
| 外部 Agent 连不上 `:8100/mcp/sse` | MCP 服务没起 → 重跑 `.\start.ps1`，看 `D:\develop\logs\mcp.err.log`。注意它**刻意**不挂在主应用下（挂了会被 middleware 包成重复响应 → 502） |
| `start.ps1` 报"端口被 [xxx] 占用，不是平台后端" | 那个端口上是**别的 python 程序**（本机 `:8010` 若是被那个股票系统抢先占了就会这样）。此时脚本**不会**启动后端。停掉它，或换端口：`.\start.ps1 -BackendPort 8020`（前端会自动跟着改 `VITE_API_TARGET`）—— 这条是保护，不是故障 |

---

## 八、设计原则

1. **算数交给代码，表达交给模型**：打分、阈值判定、拆步骤全是可复现的纯函数；
   模型只负责理解和写结论。
2. **不可撤销的动作必须有闸口**：发信前落审批单 + 停下来等人；批准时执行的是
   **当时存下来的那份参数**，人和系统看的是同一份内容。
3. **假数据必须埋异常**：库存告急、ROI 暴跌、纠纷工单、失败运行 ——
   不然熔断和转人工这些分支永远演示不出来。
4. **外部依赖可插拔**：缓存、向量库、对象存储、外部 API 各留适配器，
   开发用本地实现，生产只换适配器类。

---

## 九、相关文档

| 文档 | 内容 |
|---|---|
| [`docs/12-AI协作交接.md`](docs/12-AI协作交接.md) | 为什么把 DAG 编排换成 todo 列表（含实测数据与决策记录） |
| [`docs/13-数据模型与库表设计.md`](docs/13-数据模型与库表设计.md) | 29 张表、外键关系、灌库方式与体检结论 |
| `../mcp-servers/` | 独立可交付的办公 MCP 工具服务 |
