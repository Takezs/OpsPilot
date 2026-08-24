# OpsPilot Agent 交接说明

## 工作位置

- 仓库：`E:\JavaProjects\OpsPilot`
- 分支：`plan/opspilot-core-mvp`
- 任务 5（权限感知 Hybrid Retrieval）最新功能提交：`1bce950`
- M1（任务 1–4）与 M2 / 任务 5、任务 6 已批准（任务 6 督导验收提交 `955fe22`、`1f0e71c`、`05754bd`）
- 任务 7（Run Journal 与 Transactional Outbox）已完成，等待督导复审
- 下一任务：任务 8，Tool Registry、Agent Loop 与演示服务

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
- Reranker、上下文预算与引用回答（任务 6）：`rerank_with_fallback` 超时降级到 RRF（`reranker_status=degraded`，含真实 HTTP 超时与墙钟取消）；Context Builder 稳定引用 ID `[DOC:<document_id>#<chunk_id>]` 与令牌预算；DeepSeek 结构化回答；fail-closed Citation Validator（任一无效引用整体 `insufficient_evidence`）与引用快照；Query Rewrite 在 Generation。
- Run Journal 与 Transactional Outbox（任务 7）：`agent_runs.next_seq` 单调计数器 + `append_event(session, run_id, event_type, payload)` 在调用者事务中 `UPDATE ... RETURNING` 原子递增并锁定 Run 行，写 `run_events` 与 `event_outbox` 且不自行 commit；`publish_pending_events` 用 `FOR UPDATE SKIP LOCKED` 按 `(run_id, seq)` 取未投递 row，只向 Redis 发布 `{run_id, seq}`（频道 `run_events:{run_id}`），成功标记 delivered，失败记录 `last_error` 待重试且不阻塞其他事件（poison row 隔离），重复投递安全；`sanitize_payload` 在写入边界按词元递归脱敏（强敏感名词 + `token`/`key` 语境规则——保留 `token_count`/`token_size` 与 `prompt_tokens`/`total_tokens`/`token_usage_count` 等用量元数据，继续脱敏 `access_token`/`refresh_token`/`session_token`/`id_token` 等 + 邮箱/手机号/Bearer 凭证）与限长（字符串截断 + 整体字节上限 fail-closed），并显式校验非字符串键、含孤立代理项（lone surrogate）的键与值、不支持的 value 类型、NaN/Infinity、循环引用与嵌套深度；迁移 `0010_runs_journal_outbox`。

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

预期 PostgreSQL、Redis 为 healthy，`pg_isready` 接受连接，Redis 返回 `PONG`，Alembic 为 `0010_runs_journal_outbox (head)`。

## 任务 7（已完成）范围与验收

目标：Run Journal 与 Transactional Outbox。完整步骤见[实现计划](superpowers/plans/2026-08-19-opspilot-v1-implementation.md)中“任务 7”章节。

- 并发追加 20 个 Event 后断言 `(run_id, seq)` 连续唯一；注入 Outbox insert 失败后断言业务状态与 Event 均回滚。
- 事务内 Journal API：`append_event(session, run_id, event_type, payload)` 在调用者事务中锁定 Run seq，写 `run_events` 与 `event_outbox`，不自行 commit。
- Outbox Publisher：`FOR UPDATE SKIP LOCKED` 获取未投递 row，只向 Redis 发布 `{run_id, seq}`，成功后标记 delivered；重复发布安全。
- 真实 PostgreSQL/Redis 集成测试与 `0010_runs_journal_outbox` 迁移均已通过；当前等待督导复审。

## 任务 8（下一任务）范围

目标：Tool Registry、Agent Loop 与演示服务。完整步骤见[实现计划](superpowers/plans/2026-08-19-opspilot-v1-implementation.md)中“任务 8”章节。任务 8 不实现审批、Operation fencing、SSE 或前端（属任务 9–12/13–15）。新增迁移必须基于当前 head `0010_runs_journal_outbox` 生成新 revision，不得复用固定迁移文件名（0004/0005/0006 已被占用）。

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
- 不顺手实现任务 8。
- 不声称通过未实际运行的命令。
- 不提交 `.env`、API Key、Authorization、PII、临时目录或本地文件。

## 需要维护的文档

任务完成后更新：

- `docs/development-progress.md`：里程碑、已完成内容、最新验证数字、下一任务。
- 实现计划中的任务状态行。
- 如设计发生经批准的变化，更新架构规格；不要静默偏离。
