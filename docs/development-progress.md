# OpsPilot 开发进度

> 最后更新：2026-08-24  
> 当前分支：`plan/opspilot-core-mvp`  
> 当前阶段：M2 / 任务 5 已完成并批准

## 总体进度

| 里程碑 | 任务 | 状态 | 当前结果 |
|---|---:|---|---|
| M1 基础与知识入库 | 1–4 | 已完成并批准 | 登录、权限上传、可靠异步入库、Chunk、Vector、PostgreSQL FTS |
| M2 可解释 RAG | 5–7 | 进行中 | 任务 5 权限感知 Hybrid Retrieval 已完成并批准 |
| M3 可靠 Agent | 8–12 | 待开发 | Tool、审批、Operation、核对、SSE |
| M4 产品界面 | 13–15 | 待开发 | 五个主页面、引用抽屉、退款 E2E |
| M5 v1.0 必做评测 | 16–17 | 待开发 | 数据集、实验 Runner、指标与看板 |
| M6 发布 | 18 | 待开发 | 可观测性、隐私、部署和发布验收 |

## 已完成内容

### 任务 1：仓库骨架与质量门禁

- 建立 Python 3.12、FastAPI、pytest、Ruff、Mypy 项目骨架。
- 提供 `/health` 健康检查、环境配置、统一质量命令与 CI 基础。
- 提交：`1f00946 chore: initialize opspilot quality gates (task 1)`。

### 任务 2：PostgreSQL、pgvector 与 Redis

- 使用真实 PostgreSQL 16 + pgvector 和 Redis 7，不使用 SQLite/Fake 替代集成验收。
- 建立 SQLAlchemy async、Alembic、`vector`/`pgcrypto` 扩展和 Docker Compose 健康检查。
- 提交：`7d050c5 feat: add postgres and redis infrastructure (task 2)`。

### 任务 3：认证、知识权限与文件上传

- 实现 USER、REVIEWER、ADMIN 身份与 JWT，Token 从数据库恢复部门和最高访问级别。
- 实现 `KnowledgeScope` 数据库层权限边界和跨部门拒绝。
- 实现共享 Volume 文件存储、路径 containment、流式上传、大小限制、SHA-256 去重与文档版本。
- Document 与索引 Outbox 同事务写入，ARQ 任务只携带 `document_id` 和受控 retry attempt。
- 上传使用短读事务；写入提交前发生查询失败、提交失败或取消时补偿删除本次文件。
- 提交：`1411e37`、`8a16ab6`、`22c50b6`、`5c2bebc`。

### 任务 4：解析、切分、Embedding 与可靠索引

- 支持 Markdown/TXT、PDF、DOCX；DOCX 按 XML body 顺序保留段落和表格位置。
- 保存章节、页码和 Chunk 位置；中文 Token 估算、段落合并、受控 overlap 和超大表格预算均有测试。
- 接入 BGE-M3 Provider 契约与确定性测试 Provider。
- 建立 pgvector HNSW、PostgreSQL FTS GIN 索引，并使用真实 `EXPLAIN` 验证索引路径。
- Worker 通过 `FileStorage.open` 读取文件，重复和并发任务最终只产生一份 Chunk。
- 中间态崩溃可从不可变原文件重建；取消会自动释放 PostgreSQL transaction advisory lock。
- 提交：`8f7b0e4` 及后续 M1 加固提交。

### M1 可靠性加固

- 人工重试仅允许 REVIEWER/ADMIN 调用，必须绑定持久化、审计化的 retry intent。
- retry attempt 生命周期为 `QUEUED → RUNNING → SUCCEEDED | FAILED`，带 `started_at`、`completed_at` 和 lease。
- 普通可重试异常显式抛出 ARQ `Retry`；最终失败结束旧 attempt，允许创建唯一的 attempt+1。
- 过期 attempt 由恢复器在统一 document lock 下收敛，不无限重放旧任务。
- 伪造的 `retry_attempt` 在 UPLOADED、PARSING、CHUNKING、INDEXING 下均不得修改文档或 Chunk。
- `0007 → 0008` 会确定性回填历史 retry 状态，不留下 `QUEUED + NULL lease` 的永久阻塞记录。
- 提交：`382f8ed fix: close retry attempt lifecycle`、`9701e3f fix: backfill retry lifecycle migration`。

### 任务 5：权限感知 Hybrid Retrieval

- 定义统一候选 `RetrievalCandidate(chunk_id, score, rank, source)` 与调试阶段 `RetrievalResult(dense, fts, rrf)`。
- 实现 Dense 检索（pgvector 余弦距离，HNSW 索引）与 PostgreSQL FTS（`simple` 配置；术语不称为 BM25），各默认返回 30 条。
- 两路检索 SQL 都在 JOIN knowledge base 时应用 `KnowledgeScope` 的部门与 access level 过滤，越权 Chunk 在数据库候选层即被排除，不在 Python 中后过滤。
- 实现 Reciprocal Rank Fusion（k=60）按 chunk 去重融合到 20 条，调试响应保留 Dense、FTS、RRF 各阶段排名。
- 真实 PostgreSQL 集成测试证明受限 scope 在 Dense/FTS/RRF 候选层排除越权 Chunk，提升 scope 可检索机密 Chunk。
- 提交：`1bce950 feat: add permission aware hybrid retrieval (task 5)`。
- 复审：通过（2026-08-24），约束核验无违规（FTS 术语、SQL 层权限过滤、真实 PostgreSQL、无越界实现）。

## 当前验证基线

任务 5 完成并复审时的真实结果：

- 完整测试：`69 passed in 19.55s`（M1 基线 63 + fusion 4 + scope 集成 2）。
- Ruff format：63 个文件格式正确。
- Ruff check：全部通过。
- Mypy `--no-incremental`：38 个源文件无问题。
- Alembic：`0008_document_retry_lifecycle (head)`。
- PostgreSQL：healthy，`pg_isready` 为 accepting connections。
- Redis：healthy，`redis-cli ping` 返回 `PONG`。

## 下一步：M2 / 任务 6

目标是实现 Reranker、上下文预算与引用回答：

1. 实现 BGE Reranker；超时保持 RRF 顺序并记录降级。
2. 实现 Context Builder 与上下文预算，稳定引用 ID `[DOC:<id>#<chunk_id>]`，携带版本、章节、生效日期与页码。
3. 实现 DeepSeek 结构化回答与 Citation Validator；无有效引用的事实回答转为证据不足。
4. 通过定向测试、完整 pytest、Ruff、Mypy 后提交任务 6 检查点。

## 后续开发计划

- 任务 6：BGE Reranker、上下文预算、DeepSeek 引用回答与 Citation Validator。
- 任务 7：Run Journal、连续 seq 与 Transactional Outbox。
- 任务 8–12：Tool Registry、Agent、审批、Operation fencing、OUTCOME_UNKNOWN 核对和可靠 SSE。
- 任务 13–15：Vue 管理端、五个主页面、引用详情抽屉和核心退款 E2E。
- 任务 16–17：v1.0/简历验收前必须完成 Evaluation 数据集、异步 Runner、故障矩阵和量化报告。
- 任务 18：可观测性、脱敏、容器部署、文档与 v1.0 发布。

完整的逐任务步骤与验收命令见[实现计划](superpowers/plans/2026-08-19-opspilot-v1-implementation.md)。

## 已知技术债

- `attempt=0` 的历史 Outbox 在 0008 迁移时会按 Document 状态回填并设置 lease，但当前 retry reconciler 只处理人工 attempt（`attempt > 0`）。它不影响人工重试或 M2；后续需选择统一管理初始入库 lifecycle，或在模型和文档中明确 status/lease 只服务人工 attempt。
- 当前本地 pytest 临时目录可能因 Windows ACL 产生访问警告；使用 worktree 内独立 `--basetemp` 可稳定运行，不影响测试结果。

## 开发约束

- 所有功能遵循 Red → Green → Refactor → Commit。
- PostgreSQL/pgvector、Redis/ARQ 相关能力必须使用真实服务验证。
- 不把文件 bytes、API Key、Authorization 或完整 PII 写入 Redis、Journal 或日志。
- 不在 M1/M2 中用 SQLite 或 Fake 数据库替代集成验收。
- `.env` 和 API Key 不提交到 Git。
