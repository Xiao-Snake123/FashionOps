# Work Agent —— 企业办公 Agent 平台

一个能**真正干活**的办公 Agent：读取企业文档、生成 Word/Excel/PPT、收发邮件、管理日程，
并通过 MCP 协议接入企业既有系统。

> 目标场景：内网私有化部署 · 多用户 · 权限管理 · 国产大模型优先

---

## 技术栈

| 层 | 选型 | 说明 |
| --- | --- | --- |
| Agent 运行时 | **DeerFlow 2.0**（LangGraph + LangChain） | 字节开源，MIT，多智能体编排 / 沙箱 / 记忆 / RBAC |
| 工具协议 | **MCP**（Model Context Protocol） | 通过 FastMCP 实现，工具可被任意 MCP 客户端复用 |
| 工具服务 | **Python 3.11+ / FastMCP 4** | 本仓库主体 |
| 前端 | React / Next.js | 待开发 |
| 模型 | DeepSeek / 通义 / 智谱 / Kimi | OpenAI 兼容接口 |
| 可观测性 | OpenTelemetry（规划中） | 全链路追踪 |

## 仓库结构

```
Work agent/
├── mcp-servers/          # ★ 自研：办公能力 MCP 服务器（本仓库核心产出）
│   ├── src/office_mcp/
│   │   ├── common/       #   配置 / 路径沙箱 / 凭证隔离 / 审计
│   │   ├── docs/         #   文档处理（已可用）
│   │   ├── email/        #   邮件（开发中）
│   │   └── calendar/     #   日历（开发中）
│   ├── scripts/demo.py   #   端到端演示脚本
│   └── tests/            #   74 个测试用例
├── agent-core/           # 待开发：自研 Agent 层（模型路由 / 评测）
├── web/                  # 待开发：办公工作台前端
├── vendor/deer-flow/     # 第三方基座（gitignore，需手动克隆）
└── docs/                 # 选型调研与开发文档
```

## 快速开始

### 1. 克隆第三方基座

```bash
git clone --depth 1 https://github.com/bytedance/deer-flow.git vendor/deer-flow
```

### 2. 运行 MCP 工具服务

```bash
cd mcp-servers
uv venv --python 3.12
uv pip install -e ".[dev]"

# 端到端演示（会真实生成 Word / Excel / PPT）
uv run python scripts/demo.py

# 跑测试
uv run pytest -q
```

### 3. 作为 MCP 服务器接入 Agent

```json
{
  "mcpServers": {
    "office-docs": {
      "command": "python",
      "args": ["-m", "office_mcp.docs.server"],
      "cwd": "D:/path/to/mcp-servers",
      "env": { "OFFICE_MCP_WORKSPACE_ROOT": "D:/office-data" }
    }
  }
}
```

详见 [`mcp-servers/README.md`](mcp-servers/README.md)。

---

## 工程上值得说的几个点

### 1. 路径沙箱：防止模型越权读写

模型可能生成 `../../etc/passwd` 这类路径。所有文件操作先经 `resolve_in_root()`：

- 先 `resolve()` 解析符号链接与 `..`，**再做路径段前缀比较**
  —— 保证 `/data-evil` 不会被误判为 `/data` 的子目录
- 对不存在的目标，先解析父目录再拼文件名，避免"文件不存在就跳过校验"
- 区分 `path_violation`（越权）与 `file_not_found`（缺失）两种错误码，
  模型才知道该换路径还是该放弃

### 2. 凭证隔离：密钥永不进入模型上下文

- 密钥只从环境变量读取，**不出现在任何工具的入参签名里**
- 工具需要凭证时经 `SecretStore` 取用 `SecretLease`，`repr()` 强制输出 `***`
- 审计日志只记录凭证**名字**
- 测试用例 `test_no_credential_params_exposed` 会扫描所有工具的 JSON Schema，
  出现 `password` / `token` / `api_key` 等字段即失败

### 3. 错误即提示：让模型能自我修复

工具返回值统一为：

```json
{"ok": false, "code": "file_not_found",
 "error": "文件不存在：outputs/周报.docx",
 "hint": "请先用 list_files 查看工作区下有哪些文件，再选择正确的路径。"}
```

`hint` 是给模型的**下一步动作建议**，能显著降低无效重试次数。

### 4. 全量审计

每次工具调用写一行 JSONL：工具名、参数（递归脱敏）、耗时、状态、`trace_id`。
业务异常与内部异常都记为 `error` 并带 `error_code`，便于监控面板正确统计成功率。
审计写入失败**不阻断业务**（best-effort）。

### 5. 原子写入

文档先写同目录临时文件再 `replace()`，进程中断不会留下损坏的半成品文件。

---

## 当前进度

| 模块 | 状态 | 测试 |
| --- | --- | --- |
| `common` 横切层（配置/沙箱/凭证/审计） | ✅ 完成 | 覆盖 |
| `docs` 文档处理 MCP 服务器 | ✅ 完成，7 个工具 | 覆盖 |
| `email` 邮件 MCP 服务器 | 🚧 待开发 | — |
| `calendar` 日历 MCP 服务器 | 🚧 待开发 | — |
| 接入 DeerFlow（LangGraph 层） | 🚧 待开发 | — |
| OpenTelemetry 全链路追踪 | 🚧 待开发 | — |
| τ³-bench 评测流水线 | 🚧 待开发 | — |
| 办公 Skill（周报/纪要/邮件起草） | 🚧 待开发 | — |

**质量现状**：`ruff check` 全部通过 · `ruff format` 已统一 · `pytest` 74 passed / 1 skipped

---

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [`docs/01-选型调研-第一轮.md`](docs/01-选型调研-第一轮.md) | 平台型项目横向对比 |
| [`docs/02-候选项目清单-按梯队.md`](docs/02-候选项目清单-按梯队.md) | 按梯队整理的可二开项目 |
| [`docs/03-简历项目候选-二次开发.md`](docs/03-简历项目候选-二次开发.md) | 面向 Agent 岗的选型标准 |
| [`docs/04-LangGraph系候选与组合方案.md`](docs/04-LangGraph系候选与组合方案.md) | LangGraph 生态核实 |
| [`docs/05-2026热门技术栈与项目.md`](docs/05-2026热门技术栈与项目.md) | MCP / A2A / Graphiti / OTel 等技术点 |
| [`docs/06-开发日志.md`](docs/06-开发日志.md) | 开发记录与踩坑 |

## 许可证

- 本仓库自有代码：MIT
- `vendor/deer-flow`：MIT（字节跳动），未修改，通过独立仓库获取
