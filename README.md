# OpsPilot

OpsPilot 是一个面向企业运营场景的可审计 AI Agent 项目。它将权限感知 RAG、引用回答、工具审批、可靠副作用执行、事件日志和可复现实验整合在一个模块化单体中。

## 当前状态

- M1（任务 1–4）已完成并通过复审。
- M2 / 任务 5（权限感知 Hybrid Retrieval）已通过督导复审。
- 当前分支：`plan/opspilot-core-mvp`。
- 下一步：M2 / 任务 6，Reranker、上下文预算与引用回答。
- 最新开发状态见[开发进度](docs/development-progress.md)。

## 技术栈

- Backend：Python 3.12、FastAPI、SQLAlchemy 2、Alembic、ARQ。
- Data：PostgreSQL 16、pgvector、PostgreSQL FTS、Redis 7。
- Models：DeepSeek OpenAI-compatible Chat、BGE-M3 Embedding、BGE Reranker。
- Frontend（后续里程碑）：Vue 3、TypeScript、Vite、Element Plus。

## 本地启动

```powershell
docker compose up -d postgres redis
cd backend
..\.venv\Scripts\alembic.exe -c alembic.ini upgrade head
..\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider `
  --basetemp=E:\pyproject\OpsPilot\.worktrees\opspilot-planning\.pytest-temp-local
```

环境变量从 `.env` 读取；复制 `.env.example` 后填写本地值。禁止提交 `.env`、API Key 或其他密钥。

## 开发文档

- [开发进度](docs/development-progress.md)
- [Agent 交接说明](docs/agent-handoff.md)
- [继续开发提示词](docs/prompts/continue-development.md)
- [架构设计规格](docs/superpowers/specs/2026-08-19-opspilot-core-mvp-design.md)
- [完整实现计划](docs/superpowers/plans/2026-08-19-opspilot-v1-implementation.md)

参与开发前必须阅读根目录的 [AGENTS.md](AGENTS.md)。
