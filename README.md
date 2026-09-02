# OpsPilot

OpsPilot 是一个面向企业运营场景的可审计 AI Agent 项目。它将权限感知 RAG、引用回答、工具审批、可靠副作用执行、事件日志和可复现实验整合在一个模块化单体中。

## 当前状态

- M1（任务 1–4）已完成并通过复审。
- M2 / 任务 5（权限感知 Hybrid Retrieval）、任务 6（Reranker、上下文预算与引用回答）与任务 7（Run Journal 与 Transactional Outbox）已通过督导复审。
- M3 / 任务 8（Tool Registry、受限 Agent Loop 与演示服务）已通过督导复审。
- 当前阶段：Task18 发布候选（冻结test执行前）。
- 当前分支：`plan/opspilot-core-mvp`。
- 任务1–17已通过督导复审；冻结test仍未执行。
- 最新开发状态见[开发进度](docs/development-progress.md)。

## 技术栈

- Backend：Python 3.12、FastAPI、SQLAlchemy 2、Alembic、ARQ。
- Data：PostgreSQL 16、pgvector、PostgreSQL FTS、Redis 7。
- Models：DeepSeek OpenAI-compatible Chat、BGE-M3 Embedding、BGE Reranker。
- Frontend：Vue 3、TypeScript、Vite、Element Plus。

## 本地启动

```powershell
docker compose up -d postgres redis
cd backend
..\.venv\Scripts\alembic.exe -c alembic.ini upgrade head
..\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider `
  --basetemp=E:\JavaProjects\OpsPilot\.pytest-temp-local
```

完整候选拓扑通过 `docker compose build` 与 `docker compose up -d` 启动 API、Web、Worker、Outbox Publisher、Order、Payment、Email、PostgreSQL 与 Redis。生产模式必须显式注入 JWT、DeepSeek 与 BGE 密钥；镜像不包含 `.env`。

环境变量从 `.env` 读取；复制 `.env.example` 后填写本地值。禁止提交 `.env`、API Key 或其他密钥。

## 开发文档

- [开发进度](docs/development-progress.md)
- [Agent 交接说明](docs/agent-handoff.md)
- [继续开发提示词](docs/prompts/continue-development.md)
- [架构设计规格](docs/superpowers/specs/2026-08-19-opspilot-core-mvp-design.md)
- [完整实现计划](docs/superpowers/plans/2026-08-19-opspilot-v1-implementation.md)
- [架构](docs/architecture.md)
- [检索与引用](docs/retrieval.md)
- [可靠执行](docs/reliable-execution.md)
- [评测](docs/evaluation.md)
- [面试证据](docs/interview-guide.md)

参与开发前必须阅读根目录的 [AGENTS.md](AGENTS.md)。
