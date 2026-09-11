# LangGraph 系办公 Agent 候选与组合方案

> 调研时间：2026-09-11　　目标：找 LangGraph 技术栈、可二次开发的办公 Agent 项目

## 一、直接回答

**有，就是 DeerFlow 2.0（`bytedance/deer-flow`）。** 它不是"顺便用了 LangGraph"，而是把 LangGraph 当运行时内核。

### DeerFlow 确实是 LangGraph 的证据

| 证据 | 位置 |
| --- | --- |
| 官方定位 | README："Built on **LangGraph** and LangChain, it ships with everything an agent needs" |
| 图定义入口 | 仓库根有 `backend/langgraph.json`（LangGraph CLI 的标准配置） |
| 持久化 | LangGraph checkpointer + LangGraph Store 共用同一数据库后端（sqlite / postgres） |
| 运行时 API | 网关对外暴露 **LangGraph 兼容路径** `/api/langgraph/*`，并支持 `values` / `messages-tuple` / `updates` / `debug` / `tasks` / `checkpoints` / `custom` 等 stream 模式 |
| 调试 | 支持 `cd backend && uv run langgraph dev --allow-blocking` 直接打开 **LangGraph Studio** 可视化管理图 |
| 扩展机制 | 支持注册零参数 **`AgentMiddleware`** 类（`extensions.middlewares`），在 lead agent 与 subagent 管线中插入自定义逻辑 |
| 子图 | 子 Agent 跑在"独立的 delegated graph"里，上下文与父图隔离 |

### DeerFlow 关键参数

| 项 | 内容 |
| --- | --- |
| Stars | **82.3k**（438 贡献者，最近提交在 1 小时内） |
| 语言 | **Python 82.5% + TypeScript 13.7%** |
| 许可证 | **MIT**（可自由改名、改 UI、商用） |
| 硬件 | 本地开发 4 vCPU / 8GB；服务器 8 vCPU / 16GB |
| 后端入口 | `backend/packages/harness/deerflow/` |
| 文档 | `backend/docs/CONFIGURATION.md`、`backend/docs/API.md`、`backend/docs/MCP_SERVER.md`、`backend/docs/TUI.md` |

---

## 二、LangGraph 系候选清单（已逐个核实）

| 项目 | Stars | 语言 | 许可证 | 状态 | 结论 |
| --- | --- | --- | --- | --- | --- |
| **bytedance/deer-flow** | 82.3k | Python 82.5% + TS 13.7% | MIT | ✅ 极活跃 | **首选，二次开发基座** |
| langchain-ai/agent-inbox | 1.1k | TS 98.5% | MIT | ✅ 活跃（3 小时前） | **加分组件：人工审批 UI** |
| langchain-ai/agent-chat-ui | 3.1k | TS 95.3% | MIT | ✅ 活跃（2 天前） | **可选：自建前端底座** |
| langchain-ai/open_deep_research | 12.7k | Python 69.6% | MIT | ⚠️ **2026-08-21 已归档** | 不能当基座，可当**学习参考** |
| langchain-ai/open-agent-platform | 1.9k | TS 93.7% | MIT | ⚠️ **2026-02-25 已归档/废弃** | 官方已建议改用 LangSmith Agent Builder |
| langmanus/langmanus | — | — | — | ❌ 仓库已不存在 | 已并入 DeerFlow |

> 提醒：`open_deep_research` 和 `open-agent-platform` 这两个"看起来最像办公产品"的官方项目**都已经归档只读**。如果只搜到 README 就动手，会白花时间。

### 归档项目仍然有用的地方

**`langchain-ai/open_deep_research`** —— 代码干净、结构清晰，非常适合**学 LangGraph**：
- `src/` 目录下一套完整的 Plan-and-Execute 与 Supervisor-Researcher 两种实现（`legacy/graph.py` 与 `legacy/multi_agent.py`）
- 完整的**评测流水线**：`tests/run_evaluate.py` + `tests/extract_langsmith_data.py`，对接 Deep Research Bench，README 里有真实分数表（GPT-5 得 0.4943）
- 你可以把它的**评测方法论搬到自己项目上**——这正是我上次说的"简历上最硬的部分"

**`langchain-ai/agent-inbox`** —— 用 LangGraph 的 `interrupt()` 函数做人机协同：
- 支持 accept / edit / respond / ignore 四种人工响应动作
- 与 LangGraph 部署直连，读取 interrupt 并回写 HumanResponse
- 接到 DeerFlow 上就能实现「Agent 要发邮件 / 删文件 / 付款 → 弹审批卡片 → 人工确认」

**`langchain-ai/agent-chat-ui`** —— 任意 LangGraph server 的通用 Next.js 前端：
- 支持 **Artifacts 侧边栏渲染**（正好做"生成 Word/Excel/PPT 后侧栏预览"）
- 可通过 `NEXT_PUBLIC_API_URL` + `NEXT_PUBLIC_ASSISTANT_ID` 直接指向你的图
- 如果你不想啃 DeerFlow 的 Next.js 前端，可以用它当前端底座

---

## 三、推荐组合方案

```
┌─────────────────────────────────────────────────────┐
│  你的办公 Agent 产品（改名后的自有品牌）                │
├─────────────────────────────────────────────────────┤
│  前端层                                              │
│   方案 A：DeerFlow 自带 Next.js 前端（改造量小）        │
│   方案 B：Agent Chat UI（TypeScript，MIT，更轻）       │
│   + Agent Inbox（人工审批收件箱，HITL）                │
├─────────────────────────────────────────────────────┤
│  Agent 运行时：DeerFlow（LangGraph 内核）              │
│   • lead agent + 动态派生 subagent（上下文隔离）        │
│   • SKILL.md 技能渐进加载                              │
│   • 长期记忆（DeerMem / mem0 / honcho）                │
│   • 沙箱：Docker / K8s / E2B + 出网白名单              │
│   • MCP server 接入                                    │
│   • SSE 流式网关 + Redis stream bridge                 │
│   • 可插拔 RBAC（角色 × tools/models/skills/sandbox）  │
├─────────────────────────────────────────────────────┤
│  工具层（自研 / 集成）                                  │
│   • browser-use（网页自动化，Python，MIT）              │
│   • 办公文档：python-docx / python-pptx / openpyxl     │
│   • 会议转写：faster-whisper / FunASR                  │
│   • 数据分析：代码执行沙箱 + PandasAI                    │
├─────────────────────────────────────────────────────┤
│  模型层：DeepSeek / 通义 / 智谱 / Kimi + 自研路由       │
└─────────────────────────────────────────────────────┘
```

---

## 四、LangGraph 层面的二次开发切入点

这些是**能直接写进简历、面试能讲 20 分钟**的改动：

| # | 改动 | 具体做什么 | 面试能讲什么 |
| --- | --- | --- | --- |
| 1 | **注册新 Graph** | 在 `backend/langgraph.json` 里注册一个"办公助手"图，独立于 lead agent | StateGraph 设计、节点与条件边 |
| 2 | **写 AgentMiddleware** | 通过 `extensions.middlewares` 注入自定义中间件，做工具调用治理（限写文件、拦敏感参数、记审计） | 中间件洋葱模型、工具调用拦截 |
| 3 | **加 HITL 节点** | 用 `interrupt()` 在"发邮件/删文件/付款"前插入人工确认节点，前端接 Agent Inbox | human-in-the-loop 实现、状态持久化与恢复 |
| 4 | **自研长期记忆 Store** | 换/加一个基于 LangGraph Store 的记忆后端，做事实分类 + 淘汰策略 | 记忆架构、检索与写入门控 |
| 5 | **子 Agent 编排优化** | 调 `max_concurrent_subagents` 与任务拆分策略，测延迟 vs token 成本的权衡 | 多智能体调度 tradeoff（有数据） |
| 6 | **Checkpointer 升级** | 从 sqlite 换 postgres，加多 worker 租约与孤儿恢复 | 分布式状态一致性 |
| 7 | **接 LangGraph Studio** | 配置好 `langgraph dev`，用它做调试演示 | 可演示的开发工作流（面试加分） |
| 8 | **建评测集** | 参考 open_deep_research 的评测脚本，自建 30 条办公任务评测 | **成功率 / 步数 / 延迟 / 成本 四组数字** |

### 优先级建议（如果只有 4 周）

```
第 1 周：跑通 DeerFlow，读 backend/packages/harness/，画出架构图
第 2 周：写 2 个办公 Skill + 1 个 AgentMiddleware（出第一个可见成果）
第 3 周：接 Agent Inbox 做 HITL；做模型路由
第 4 周：建评测集，跑出前后对比数据；改品牌 UI；写文档
```

---

## 五、下一步

- [ ] 确认用 DeerFlow 2.0 作为 LangGraph 基座
- [ ] 拉代码 + Docker 跑通（8GB 内存起）
- [ ] 读 `backend/langgraph.json` 和 `backend/packages/harness/deerflow/`，画出图结构
- [ ] 定义你要注册的第一个自定义 Graph / Middleware
