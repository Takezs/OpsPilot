# OpsPilot Agent 交接说明

## 工作位置

- 仓库：`E:\pyproject\OpsPilot`
- 隔离 worktree：`E:\pyproject\OpsPilot\.worktrees\opspilot-planning`
- 分支：`plan/opspilot-core-mvp`
- 当前文档交接前最新功能提交：`9701e3f`
- M1 状态：已批准
- 下一任务：任务 5，权限感知 Hybrid Retrieval

只在隔离 worktree 中开发。主工作区存在用户文件，不得清理、覆盖或回退。

## 已有能力

- FastAPI 项目、配置、统一健康检查和质量门禁。
- PostgreSQL 16 + pgvector、Redis 7、Alembic、Docker Compose。
- JWT 登录、USER/REVIEWER/ADMIN、数据库恢复的 `KnowledgeScope`。
- 流式文件上传、共享 Volume、安全路径、去重、版本和可靠索引 Outbox。
- Markdown/TXT、PDF、DOCX 解析，中文感知结构切分，页码/章节引用信息。
- BGE-M3 Embedding 契约、pgvector HNSW、PostgreSQL FTS GIN。
- 可恢复、可并发收敛的索引 Worker。
- REVIEWER/ADMIN 人工重试、retry attempt lifecycle、ARQ 显式 Retry、lease reconciler。
- 带历史数据的 `0007 → 0008` 安全迁移。

M1 最终完整基线为 63 个测试通过；不要把这个数字当作新代码的验证结果，每次交接都必须重新运行并报告最新数字。

## 环境检查

```powershell
cd E:\pyproject\OpsPilot\.worktrees\opspilot-planning
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

预期 PostgreSQL、Redis 为 healthy，`pg_isready` 接受连接，Redis 返回 `PONG`，Alembic 为 head。

## 任务 5 精确范围

创建：

- `backend/src/opspilot/retrieval/types.py`
- `backend/src/opspilot/retrieval/dense.py`
- `backend/src/opspilot/retrieval/fts.py`
- `backend/src/opspilot/retrieval/fusion.py`
- `backend/src/opspilot/retrieval/service.py`
- `backend/tests/retrieval/test_fusion.py`
- `backend/tests/integration/test_retrieval_scope.py`

必须实现：

1. `RetrievalCandidate(chunk_id, score, rank, source)` 统一候选类型。
2. Dense 检索与 PostgreSQL FTS 检索，各默认返回 30 条。
3. 两路 SQL 都在 JOIN knowledge base 时应用部门和 access level 过滤。
4. RRF 默认融合到 20 条，按 chunk 去重并保留各阶段排名。
5. 调试结果明确分为 Dense、FTS、RRF；不要把 FTS 称为 BM25。
6. 真实 PostgreSQL 集成测试证明无权限 Chunk 从候选 SQL 阶段就不可见。

任务 5 不包含 Reranker、LLM、Query Rewrite、引用回答、Run Journal 或前端。

## 任务 5 建议 TDD 顺序

1. 写 `test_rrf_merges_rankings`，确认 fusion 模块缺失或行为失败。
2. 实现最小 RRF 和候选类型，运行 fusion 单测变绿。
3. 写真实 PostgreSQL scope 测试，同时插入允许与禁止的 Chunk。
4. 实现 Dense SQL 的 scope 过滤并验证禁止 Chunk 不在结果中。
5. 实现 FTS SQL 的同等 scope 过滤。
6. 实现 service 调度、去重和调试阶段响应。
7. 运行任务 5 定向测试，然后运行完整门禁。
8. 更新 `docs/development-progress.md` 并提交一次任务 5 commit。

## 任务 5 验收命令

```powershell
cd E:\pyproject\OpsPilot\.worktrees\opspilot-planning\backend
..\.venv\Scripts\python.exe -m pytest `
  tests\retrieval\test_fusion.py `
  tests\integration\test_retrieval_scope.py -q -p no:cacheprovider `
  --basetemp=E:\pyproject\OpsPilot\.worktrees\opspilot-planning\.pytest-temp-task5

..\.venv\Scripts\ruff.exe format --check .
..\.venv\Scripts\ruff.exe check .
..\.venv\Scripts\mypy.exe --no-incremental src
..\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider `
  --basetemp=E:\pyproject\OpsPilot\.worktrees\opspilot-planning\.pytest-temp-task5-full
..\.venv\Scripts\alembic.exe -c alembic.ini current
```

## 禁止事项

- 不直接在 main 分支开发。
- 不将 M1 已批准代码重写成另一套架构。
- 不在 Python 层做权限后过滤。
- 不用 SQLite/Fake 数据库宣称检索集成通过。
- 不把 PostgreSQL FTS 写成 BM25。
- 不顺手实现任务 6。
- 不声称通过未实际运行的命令。

## 需要维护的文档

任务完成后更新：

- `docs/development-progress.md`：里程碑、已完成内容、最新验证数字、下一任务。
- 实现计划中的任务状态行。
- 如设计发生经批准的变化，更新架构规格；不要静默偏离。
