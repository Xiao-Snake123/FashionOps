# office-mcp

办公自动化 MCP 服务器集合。Agent 通过 [Model Context Protocol](https://modelcontextprotocol.io/)
调用这些服务器完成真实的办公操作。

## 服务器一览

| 服务器 | 说明 | 状态 |
| --- | --- | --- |
| `office-docs` | Word / Excel / PowerPoint / Markdown 读写 | ✅ 可用 |
| `office-email` | IMAP 收信 + SMTP 发信（带 dry-run） | 🚧 开发中 |
| `office-calendar` | iCalendar / CalDAV 日程读写 | 🚧 开发中 |

## 设计原则

1. **凭证永不进入模型上下文**
   密钥只从环境变量读取，不出现在任何工具签名或返回值里。工具需要凭证时经
   `SecretStore` 取用 `SecretLease`（审计日志只记录凭证**名字**）。

2. **路径沙箱**
   所有文件操作先经 `resolve_in_root()` 解析符号链接与 `..`，再做路径段前缀比较，
   把访问限制在 `OFFICE_MCP_WORKSPACE_ROOT` 之内。`/data-evil` 不会被误判为 `/data` 的子路径。

3. **全量审计**
   每次工具调用写一行 JSONL：工具名、参数（递归脱敏）、耗时、状态、`trace_id`。
   审计失败不阻断业务（best-effort）。

4. **原子写入**
   文档先写同目录临时文件再 `replace()`，中断不会留下损坏的半成品。

5. **可降级**
   外部依赖不可用时以 `dry_run` 返回计划动作，而不是直接抛异常。

## 安装

```bash
cd mcp-servers
uv venv --python 3.12
uv pip install -e ".[dev]"
```

## 配置

全部通过环境变量（前缀 `OFFICE_MCP_`），也可写进 `.env`：

```dotenv
# 工作区根目录（所有文件操作的沙箱边界）
OFFICE_MCP_WORKSPACE_ROOT=./workspace

# 审计日志（JSONL，留空则关闭）
OFFICE_MCP_AUDIT_LOG=./workspace/audit.jsonl

# 只演练不落盘/不发送
OFFICE_MCP_DRY_RUN=0

# 单文件读取上限
OFFICE_MCP_MAX_READ_BYTES=8388608

# ---- 邮件（可选）----
OFFICE_MCP_SMTP_HOST=smtp.example.com
OFFICE_MCP_SMTP_PORT=465
OFFICE_MCP_IMAP_HOST=imap.example.com
OFFICE_MCP_MAIL_ADDRESS=me@example.com
OFFICE_MCP_MAIL_PASSWORD=***

# ---- 日历（可选）----
OFFICE_MCP_CALDAV_URL=https://caldav.example.com
OFFICE_MCP_CALDAV_USERNAME=me
OFFICE_MCP_CALDAV_PASSWORD=***
OFFICE_MCP_TIMEZONE=Asia/Shanghai
```

## 运行

```bash
# stdio（供 MCP 客户端拉起，默认）
python -m office_mcp.docs.server

# HTTP，便于本地调试
$env:OFFICE_MCP_TRANSPORT="http"; python -m office_mcp.docs.server
```

接入 Claude Desktop / Cursor / DeerFlow 的 MCP 配置示例：

```json
{
  "mcpServers": {
    "office-docs": {
      "command": "python",
      "args": ["-m", "office_mcp.docs.server"],
      "cwd": "D:/path/to/mcp-servers",
      "env": {
        "OFFICE_MCP_WORKSPACE_ROOT": "D:/office-data",
        "OFFICE_MCP_AUDIT_LOG": "D:/office-data/audit.jsonl"
      }
    }
  }
}
```

## 工具清单（office-docs）

| 工具 | 作用 |
| --- | --- |
| `list_files` | 列出工作区文件（支持通配符、递归、条数上限） |
| `read_document` | 读 docx/xlsx/pptx/md/txt/csv/json → Markdown |
| `create_word_document` | 按结构化块生成 Word（标题/段落/要点/表格/分页） |
| `create_excel_workbook` | 生成多工作表 Excel（自动表头样式、冻结首行、自动列宽） |
| `create_presentation` | 生成 16:9 PPT（含演讲者备注） |
| `write_text_file` | 写 md/txt/csv/json |
| `get_workspace_info` | 返回工作区与能力信息（不含凭证） |

工具返回值统一为 `{"ok": true, ...}` 或
`{"ok": false, "code": "...", "error": "...", "hint": "..."}`，
其中 `hint` 是给模型的修复建议，能显著降低无效重试。

## 测试

```bash
uv run pytest -q
```

## 目录结构

```
mcp-servers/
├── pyproject.toml
├── README.md
├── src/office_mcp/
│   ├── common/           # 配置 / 路径沙箱 / 凭证 / 审计 / 异常
│   ├── docs/             # 文档处理服务器
│   ├── email/            # 邮件服务器
│   └── calendar/         # 日历服务器
└── tests/
```
