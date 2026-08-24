# OpsPilot Agent 交接说明

## 工作位置

- 仓库：`E:\JavaProjects\OpsPilot`
- 分支：`plan/opspilot-core-mvp`
- 任务 5（权限感知 Hybrid Retrieval）最新功能提交：`1bce950`
- 复审修复（READY 过滤、CI、`effective_at`、文档同步）：见 review-fix 提交。
- M1（任务 1–4）与 M2 / 任务 5 已批准（任务 5 督导验收提交 `d5ba8f3`、`b490bad`）
- 下一任务：任务 6，Reranker、上下文预算与引用回答

只在 `plan/opspilot-core-mvp` 分支开发。主工作区存在用户文件，不得清理、覆盖或回退。

## 已有能力

- FastAPI 项目、配置、统一健康检查和质量门禁。
- PostgreSQL 16 + pgvector、Redis 7、Alembic、Docker Compose。
- JWT 登录、USER/REVIEWER/ADMIN、数据库恢复的 `KnowledgeScope`（KB 级权限）。
- 流式文件上传、共享 Volume、安全路径、去重、版本、`effective_at` 和可靠索引 Outbox。
- Markdown/TXT、PDF、DOCX 解析，中文感知结构切分，页码/章节引用信息。
- BGE-M3 Embedding 契约、pgvector HNSW、PostgreSQL FTS GIN。
- 可恢复、可并发收敛的索引 Worker。
- REVIEWER/ADMIN 人工重试、retry attempt lifecycle、ARQ 显式 Retry、lease reconciler。
- 带历史数据的 `0007 → 0008` 安全迁移，以及 `0009` Document `effective_at`。
- 权限感知 Dense/PostgreSQL FTS 检索与 RRF 融合（任务 5）：两路 SQL 在数据库候选层应用 `KnowledgeScope` 过滤、只检索 `Document.status == READY` 的 Chunk，相同分数按 `chunk_id` 稳定排序。

不要用历史数字当作新代码的验证结果，每次交接都必须重新运行并报告最新数字。

## 环境检查

```powershell
cd E:\JavaProjects\OpsPilot
docker version
docker compose version
docker compose up -d postgres redis
docker compose ps
docker compose exec -T postgres pg_isready -U opspilot -d opspilot
docker compose exec -T redis redis-cli ping

cd backend
..\.venv\Scripts\alembic.exe -c alembic.ini upgrade head
..\.venv\Scripts\alembic.exe -c alembic.ini current
```

预期 PostgreSQL、Redis 为 healthy，`pg_isready` 接受连接，Redis 返回 `PONG`，Alembic 为 `0009_document_effective_at (head)`。

## 任务 6（下一任务）范围

目标：Reranker、上下文预算与引用回答。完整步骤见[实现计划](superpowers/plans/2026-08-19-opspilot-v1-implementation.md)中“任务 6”章节。

- 实现 BGE Reranker；超时保持 RRF 顺序并记录降级（`reranker_status=degraded`）。
- 实现 Context Builder 与上下文预算，稳定引用 ID `[DOC:<document_id>#<chunk_id>]`，携带版本、章节、生效日期（`effective_at`）与页码。
- 实现 DeepSeek 结构化回答与 Citation Validator；无有效引用的事实回答转为 `insufficient_evidence`。
- 引用快照至少保存 `document_id + document_version + chunk_id + section_path + page`。
- 任务 6 不包含 Query Rewrite 调用 Agent Orchestrator 之外的接线、Run Journal、Tool Gateway 或前端。

注意：新增迁移必须基于当前 head `0009_document_effective_at` 生成新 revision（`alembic revision`），不得复用实现计划中旧的任务 7/9/16 固定迁移文件名（0004/0005/0006 已被现有迁移占用）。

## 任务 6 建议 TDD 顺序

1. 写 `tests/retrieval/test_context.py` 与 `tests/generation/test_citations.py` 失败测试（预算、降级、引用校验）。
2. 实现 Provider 契约与 Fake Provider，运行定向测试变绿。
3. 实现 Context Builder 与 Citation Validator。
4. 运行任务 6 定向测试，然后运行完整门禁。
5. 更新 `docs/development-progress.md` 并单独提交任务 6。

## 完整验证命令

```powershell
cd E:\JavaProjects\OpsPilot\backend
..\.venv\Scripts\ruff.exe format --check .
..\.venv\Scripts\ruff.exe check .
..\.venv\Scripts\mypy.exe --no-incremental src
..\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider `
  --basetemp=E:\JavaProjects\OpsPilot\.pytest-temp-agent
..\.venv\Scripts\alembic.exe -c alembic.ini current
```

Windows 默认临时目录可能出现 ACL 错误；使用仓库内唯一 `--basetemp`。不要把 ACL 错误误报为代码或数据库失败。

## 禁止事项

- 不直接在 main 分支开发。
- 不将 M1 已批准与任务 5 已完成代码重写成另一套架构。
- 不在 Python 层做权限后过滤；权限、READY 状态过滤必须保留在候选 SQL。
- 不用 SQLite/Fake 数据库宣称检索集成通过。
- 不把 PostgreSQL FTS 写成 BM25。
- 不顺手实现任务 7。
- 不声称通过未实际运行的命令。
- 不提交 `.env`、API Key、Authorization、PII、临时目录或本地文件。

## 需要维护的文档

任务完成后更新：

- `docs/development-progress.md`：里程碑、已完成内容、最新验证数字、下一任务。
- 实现计划中的任务状态行。
- 如设计发生经批准的变化，更新架构规格；不要静默偏离。
