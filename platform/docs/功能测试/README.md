# 功能测试（截图 + 输出 + 报告）

把项目所有业务功能跑了一遍的归档。每个功能都有 **功能描述 / 使用教程 / 测试数据 / 输出 / 截图** 五部分。

## 入口文档
- [功能测试报告.md](功能测试报告.md) —— 14 项业务功能逐项说明，含测试中发现并修复的 Bug。

## 截图（`screenshots/`）
| 文件 | 对应功能 |
|------|----------|
| `01-login.png` | 登录认证 |
| `02-dashboard.png` | 数据看板 |
| `03-chat.png` | 对话助手 |
| `03b-chat-query.png` | 对话-库存查询（触发 ERP 工具） |
| `03c-chat-rag.png` | 对话-退货政策（触发知识库 RAG） |
| `04-tasks.png` | 任务工作台 |
| `09-taskdetail-approval.png` | 任务详情-审批横幅 |
| `09b-task-run.png` | 任务运行中（SSE 实时事件） |
| `09c-task-after.png` | 审批后状态 |
| `10-taskdetail-success.png` | 任务成功（步骤 + 产物） |
| `05-emails.png` | 邮件闸门 |
| `06-tickets.png` | 客服工单 |
| `07-campaigns.png` | 广告投放 |
| `08-products.png` | 商品库 |
| `11-accounts.png` | 账号管理（RBAC） |
| `12-apidocs.png` | API 文档（OpenAPI 规范） |

## 命令行输出（`outputs/`）
- `kb-index.txt` / `kb-stats.txt` / `kb-search.txt` —— 知识库建索引 / 状态 / 混合检索
- `config-check.txt` —— 配置自检（102 项，无问题）
- `db-info.txt` —— 数据库状态（29 表，10277 行）
- `auth-login.json` / `auth-me.json` / `auth-refresh.json` —— 认证登录 / 用户信息 / 刷新令牌
- `summary.json` —— 看板汇总接口

## 测试环境
后端 `http://127.0.0.1:8000`，前端 `http://localhost:3000`；MySQL / Redis / Qdrant 均在线。
演示账号：`demo` / `FashionOps@2026`。

## 重要提示
测试发现 Phase 3（提交 `9d6523d`）引入一个真实回归：`qdrant_store.search_hybrid` 的 `FusionQuery` 调用在 `qdrant-client==1.19.0` 下报错，已修复（`prefetch` 提到 `query_points` 顶层）并重建索引。详见报告开头「⚠️ 测试中发现并修复的问题」。
