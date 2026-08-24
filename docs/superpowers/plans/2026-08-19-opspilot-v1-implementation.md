# OpsPilot v1 实现计划

> 当前开发状态与最近验证证据见[开发进度](../../development-progress.md)。

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 以纵向切片交付可上传、可检索、可引用问答、可审批退款且能从不确定结果中安全恢复的 OpsPilot，并在 v1.0 前完成可复现 Evaluation 与发布验收。

**架构：** FastAPI 模块化单体保存业务事实，ARQ Worker 执行异步入库和可靠副作用，PostgreSQL+pgvector 同时承载业务、向量、FTS、Run Journal 与 Transactional Outbox，Redis 只承载队列和实时通知。Vue 3 管理端通过 REST/SSE 展示五个主页面；所有外部业务调用必须经过 Tool Gateway，副作用请求可能到达 Provider 时必须先核对而非盲目重试。

**技术栈：** Python 3.12、FastAPI、Pydantic 2、SQLAlchemy 2、Alembic、PostgreSQL 16、pgvector、Redis 7、ARQ、DeepSeek V4 Flash、BGE-M3、BGE Reranker、Vue 3、TypeScript、Vite、Element Plus、Pinia、Vitest、Playwright、pytest、Testcontainers、OpenTelemetry、Docker Compose。

---

## 里程碑与执行纪律

| 里程碑 | 任务 | 可验收结果 |
|---|---:|---|
| M1 基础与知识入库 | 1–4 | 登录、上传文档、异步解析并写入 Chunk/Vector/FTS |
| M2 可解释 RAG | 5–7 | 权限感知 Hybrid Retrieval、Reranker、引用问答、Run Journal |
| M3 可靠 Agent | 8–12 | 工具决策、审批、Operation、Outbox、Lease fencing、核对与 SSE |
| M4 产品界面 | 13–15 | 五个主页面、引用抽屉和核心退款 E2E |
| M5 v1.0 必做评测 | 16–17 | 版本化数据集、实验 Runner、指标、故障矩阵、报告与看板 |
| M6 发布 | 18 | 可观测性、隐私、一键部署、文档和完整验收 |

每个任务严格执行 Red → Green → Refactor → Commit。未安装 Docker 时只执行纯单元步骤；任务 2 开始前必须安装 Docker Desktop，并以真实 PostgreSQL/pgvector 与 Redis 完成集成验证，禁止用 SQLite 替代。API Key 只写入未跟踪的 `.env`。

新增数据库变更必须基于当前 Alembic head 执行 `alembic revision` 生成新 revision 文件，并保持 `backend/alembic/versions/` 内的顺序编号一致；禁止使用本文档历史步骤中残留的 `0004/0005/0006` 等固定迁移文件名——这些编号已被现有迁移（`0004_user_knowledge_scope`、`0005_chunk_page`、`0006_document_index_outbox`）占用。

## 文件结构

- `backend/src/opspilot/api/`：路由组装、统一响应与错误映射。
- `backend/src/opspilot/auth/`：固定角色、JWT、KnowledgeScope。
- `backend/src/opspilot/knowledge/`：文件存储、文档生命周期、解析、切分、Embedding 和索引。
- `backend/src/opspilot/retrieval/`：Dense、PostgreSQL FTS、RRF、Reranker 与 Context Builder；禁止调用 LLM。
- `backend/src/opspilot/generation/`：OpenAI-compatible Chat、Query Rewrite、结构化回答和引用校验。
- `backend/src/opspilot/runs/`：Run、Append-only Journal、Transactional Outbox、SSE。
- `backend/src/opspilot/tools/`：ToolDefinition、Registry、Adapter 与演示工具契约。
- `backend/src/opspilot/execution/`：策略、审批、Operation、Claim/Lease、fencing、执行与核对。
- `backend/src/opspilot/evaluation/`：公开接口驱动的实验 Runner 和确定性指标。
- `frontend/src/views/`：五个主页面；`frontend/src/components/`：引用抽屉、工具卡片、时间线。
- `demo-services/`：合成订单、支付和邮件服务，支持确定性故障注入。
- `evaluation/`：版本化数据集、实验配置和不可手改的报告产物。

### 任务 1：仓库骨架与质量门禁

**状态：✅ 已完成。**

**文件：**
- 创建：`backend/pyproject.toml`
- 创建：`backend/src/opspilot/main.py`
- 创建：`backend/src/opspilot/config.py`
- 创建：`backend/src/opspilot/api/errors.py`
- 创建：`backend/tests/test_health.py`
- 创建：`Makefile`
- 创建：`.env.example`
- 创建：`.github/workflows/ci.yml`

- [ ] **步骤 1：创建 Python 项目配置并安装开发依赖**

在 `pyproject.toml` 配置 Python 3.12、FastAPI、Pydantic Settings、SQLAlchemy async、Alembic、asyncpg、pgvector、Redis、ARQ、httpx、PyMuPDF、python-docx、PyJWT、pwdlib、OpenTelemetry，以及 pytest、pytest-asyncio、ruff、mypy、coverage、testcontainers。创建 `.venv` 并以 editable 模式安装开发依赖；本步骤只建立工具链，不实现应用行为。

- [ ] **步骤 2：编写失败的健康检查测试**

```python
from fastapi.testclient import TestClient
from opspilot.main import app

def test_health() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **步骤 3：运行并确认应用尚不存在**

运行：`cd backend && python -m pytest tests/test_health.py -q`
预期：FAIL，`ModuleNotFoundError: opspilot`。

- [ ] **步骤 4：创建最小应用**

`main.py` 创建 `FastAPI(title="OpsPilot", version="0.1.0")` 和异步 `/health`。

- [ ] **步骤 5：增加统一错误对象与配置**

```python
class ApiError(BaseModel):
    code: str
    message: str
    details: dict[str, object] | None = None

class ErrorResponse(BaseModel):
    error: ApiError
    request_id: str
```

配置必须从环境读取 `DATABASE_URL`、`REDIS_URL`、`DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL` 和模型名；`.env.example` 只放空值。

- [ ] **步骤 6：建立质量命令并验证**

`make check` 依次运行 `ruff check backend`、`ruff format --check backend`、`mypy backend/src` 和 `pytest --cov=opspilot`。运行 `make check`，预期全部通过。

- [ ] **步骤 7：提交**

```bash
git add backend Makefile .env.example .github/workflows/ci.yml
git commit -m "chore: initialize opspilot quality gates"
```

### 任务 2：PostgreSQL、Redis 与核心迁移

**状态：✅ 已完成。**

**文件：**
- 创建：`docker-compose.yml`
- 创建：`backend/src/opspilot/db.py`
- 创建：`backend/src/opspilot/models/base.py`
- 创建：`backend/alembic.ini`
- 创建：`backend/alembic/env.py`
- 创建：`backend/alembic/versions/0001_core_extensions.py`
- 创建：`backend/tests/integration/test_database.py`

- [ ] **步骤 1：安装 Docker Desktop并验证**

运行：`docker version && docker compose version`。预期两个命令退出码为 0；否则停止本任务，不得用 SQLite 继续。

- [ ] **步骤 2：编写失败的扩展测试**

```python
@pytest.mark.integration
async def test_required_extensions(session: AsyncSession) -> None:
    rows = await session.scalars(text("select extname from pg_extension"))
    assert {"vector", "pgcrypto"} <= set(rows)
```

- [ ] **步骤 3：创建 Compose 与迁移**

使用包含 pgvector 的 PostgreSQL 16 镜像和 Redis 7；两者配置健康检查。迁移执行 `CREATE EXTENSION IF NOT EXISTS vector` 与 `pgcrypto`。`db.py` 暴露 async engine、session factory 和 request-scoped session。

- [ ] **步骤 4：运行真实集成验证**

运行：`docker compose up -d postgres redis && cd backend && alembic upgrade head && pytest tests/integration/test_database.py -q`。预期 PASS。

- [ ] **步骤 5：提交**

```bash
git add docker-compose.yml backend/src/opspilot/db.py backend/src/opspilot/models backend/alembic.ini backend/alembic backend/tests/integration/test_database.py
git commit -m "feat: add postgres and redis infrastructure"
```

### 任务 3：认证、知识权限与共享文件存储

**状态：✅ 已完成，并完成并发上传、可靠 Outbox 与人工重试加固。**

**文件：**
- 创建：`backend/src/opspilot/auth/models.py`、`schemas.py`、`service.py`、`router.py`
- 创建：`backend/src/opspilot/knowledge/models.py`、`schemas.py`、`storage.py`、`repository.py`、`router.py`
- 创建：`backend/alembic/versions/0002_auth_knowledge.py`
- 创建：`backend/tests/auth/test_auth.py`
- 创建：`backend/tests/knowledge/test_upload.py`

- [ ] **步骤 1：编写认证与权限失败测试**

覆盖成功登录、错误密码 401、Token 恢复 USER/REVIEWER/ADMIN，以及 `KnowledgeScope(departments, max_access_level)`。

- [ ] **步骤 2：编写上传失败测试**

覆盖 `.md` 成功、非法扩展名 422、20MB 上限、SHA-256 去重、同标题不同版本、UUID 文件名和跨部门 403；断言队列参数只有 `document_id`，不含 bytes。

- [ ] **步骤 3：实现最小认证与模型**

使用 pwdlib 哈希密码、PyJWT 短期 Token、固定角色枚举；创建 users、knowledge_bases、documents 表及唯一约束。

- [ ] **步骤 4：实现 FileStorage 边界**

```python
class FileStorage(Protocol):
    async def save(self, stream: AsyncIterator[bytes], suffix: str) -> str: ...
    async def open(self, storage_path: str) -> AsyncContextManager[BinaryIO]: ...
```

实现 `VolumeFileStorage`，服务端生成 UUID 文件名，路径必须解析在配置的 storage root 内。

- [ ] **步骤 5：验证并提交**

运行：`cd backend && pytest tests/auth tests/knowledge/test_upload.py -q`。预期 PASS。

```bash
git add backend/src/opspilot/auth backend/src/opspilot/knowledge backend/alembic/versions/0002_auth_knowledge.py backend/tests
git commit -m "feat: add secure knowledge document upload"
```

### 任务 4：解析、结构切分与 BGE-M3 索引

**状态：✅ 已完成，M1 最终复审已批准。**

**文件：**
- 创建：`backend/src/opspilot/knowledge/parsers/base.py`、`pdf.py`、`docx.py`、`markdown.py`
- 创建：`backend/src/opspilot/knowledge/chunking.py`、`embedding.py`、`indexer.py`、`tasks.py`
- 创建：`backend/src/opspilot/worker.py`
- 创建：`backend/alembic/versions/0003_chunks.py`
- 创建：`backend/tests/knowledge/test_chunking.py`、`test_indexer.py`
- 创建：`backend/tests/fixtures/documents/`

- [ ] **步骤 1：编写结构切分测试**

```python
def test_heading_and_body_stay_in_same_chunk() -> None:
    chunks = chunk_blocks(FIXTURE_BLOCKS, max_tokens=650)
    target = next(c for c in chunks if "七天无理由" in c.content)
    assert target.section_path == ["3 退款", "3.2 七天无理由"]
    assert target.token_count <= 650
```

另断言表格不按行拆散、相邻 Chunk 仅 50 tokens 重叠。

- [ ] **步骤 2：编写 Fake Embedding 批处理测试**

定义 `EmbeddingProvider.embed(texts: list[str]) -> list[list[float]]`，断言批次大小、顺序、`model+content_sha256` 缓存键和重复任务不产生重复 Chunk。

- [ ] **步骤 3：实现解析、切分与 Provider**

统一 `ParsedBlock(kind, text, level, page)`；实现文本型 PDF、DOCX、Markdown/TXT 解析，BGE-M3 本地 Provider 和确定性 Fake Provider。

- [ ] **步骤 4：实现 Worker 入库任务**

任务只接收 `document_id`，从 PG 读取 `storage_path`，按状态 PARSING→CHUNKING→INDEXING→READY 更新；失败写安全原因。创建 vector 索引和 `tsvector` 生成/触发逻辑。

- [ ] **步骤 5：验证并提交**

运行：`cd backend && pytest tests/knowledge/test_chunking.py tests/knowledge/test_indexer.py -q`，再运行真实迁移与索引集成测试。

```bash
git add backend/src/opspilot/knowledge backend/src/opspilot/worker.py backend/alembic/versions/0003_chunks.py backend/tests/knowledge backend/tests/fixtures
git commit -m "feat: index structured documents with bge m3"
```

### 任务 5：权限感知 Hybrid Retrieval

**状态：✅ 已完成，复审通过。**

**文件：**
- 创建：`backend/src/opspilot/retrieval/types.py`、`dense.py`、`fts.py`、`fusion.py`、`service.py`
- 创建：`backend/tests/retrieval/test_fusion.py`
- 创建：`backend/tests/integration/test_retrieval_scope.py`

- [ ] **步骤 1：编写 RRF 与权限测试**

```python
def test_rrf_merges_rankings() -> None:
    result = reciprocal_rank_fusion([["a", "b"], ["b", "c"]], k=60)
    assert result[0].chunk_id == "b"
```

集成测试插入 `access_level=3` 风控 Chunk，USER scope 查询结果必须在数据库候选层排除该 Chunk。

- [ ] **步骤 2：实现统一候选类型和两路检索**

`RetrievalCandidate(chunk_id, score, rank, source)`；Dense 与 PostgreSQL FTS 查询均必须接收 `KnowledgeScope` 并构造 SQL 过滤条件。

- [ ] **步骤 3：实现 RRF、去重与调试响应**

每路默认 30 条，融合 20 条；调试响应分别保存 Dense、FTS 和 RRF 排名，不把 PostgreSQL FTS 称为 BM25。

- [ ] **步骤 4：验证并提交**

运行：`cd backend && pytest tests/retrieval/test_fusion.py tests/integration/test_retrieval_scope.py -q`。预期 PASS。

```bash
git add backend/src/opspilot/retrieval backend/tests/retrieval backend/tests/integration/test_retrieval_scope.py
git commit -m "feat: add permission aware hybrid retrieval"
```

### 任务 6：Reranker、上下文预算与引用回答

**文件：**
- 创建：`backend/src/opspilot/retrieval/reranker.py`、`context_builder.py`
- 创建：`backend/src/opspilot/generation/provider.py`、`rewrite.py`、`prompts.py`、`citations.py`、`service.py`
- 创建：`backend/tests/retrieval/test_context.py`
- 创建：`backend/tests/generation/test_citations.py`

- [ ] **步骤 1：编写预算、降级与引用测试**

断言 Reranker 超时保持 RRF 顺序并设置 `degraded`；上下文不超预算且引用 ID 顺序稳定；未检索、过期或不存在的引用使事实回答转为 `insufficient_evidence=True`。

- [ ] **步骤 2：实现 Provider 契约**

```python
class GroundedAnswer(BaseModel):
    answer: str
    citations: list[str]
    insufficient_evidence: bool
    follow_up_question: str | None = None
```

实现 DeepSeek V4 Flash OpenAI-compatible Provider、BGE Reranker 和 Fake Provider。Query Rewrite 放在 Generation，由 Agent Orchestrator 按需调用，不放入 Retrieval。

- [ ] **步骤 3：实现 Context Builder 与 Citation Validator**

上下文标识为 `[DOC:<document_id>#<chunk_id>]`，携带版本、章节、生效日期和页码；保存消息引用快照。

- [ ] **步骤 4：验证并提交**

运行：`cd backend && pytest tests/retrieval/test_context.py tests/generation/test_citations.py -q`。预期 PASS。

```bash
git add backend/src/opspilot/retrieval backend/src/opspilot/generation backend/tests/retrieval backend/tests/generation
git commit -m "feat: answer with reranked verified citations"
```

### 任务 7：Run Journal 与 Transactional Outbox

**文件：**
- 创建：`backend/src/opspilot/runs/models.py`、`journal.py`、`outbox.py`
- 创建：`backend/alembic/versions/` 下基于当前 head 生成的新 revision（禁止使用固定名称 `0004_runs_outbox.py`）
- 创建：`backend/tests/runs/test_journal.py`
- 创建：`backend/tests/integration/test_outbox.py`

- [ ] **步骤 1：编写并发 seq 与原子性测试**

并发追加 20 个 Event 后断言 `(run_id, seq)` 连续唯一；注入 Outbox insert 失败后断言业务状态与 Event 均回滚。

- [ ] **步骤 2：实现事务内 Journal API**

`append_event(session, run_id, event_type, payload)` 在调用者事务中锁定 Run seq，写 `run_events` 与 `event_outbox`，不得自行 commit。

- [ ] **步骤 3：实现 Outbox Publisher**

Publisher 使用 `FOR UPDATE SKIP LOCKED` 获取未投递 row，只向 Redis 发布 `{run_id, seq}`，成功后标记 delivered；重复发布必须安全。

- [ ] **步骤 4：验证并提交**

运行：`cd backend && pytest tests/runs tests/integration/test_outbox.py -q`。预期 PASS。

```bash
git add backend/src/opspilot/runs backend/alembic/versions backend/tests/runs backend/tests/integration/test_outbox.py
git commit -m "feat: persist run events with transactional outbox"
```

### 任务 8：Tool Registry、Agent Loop 与演示服务

**文件：**
- 创建：`backend/src/opspilot/tools/types.py`、`registry.py`
- 创建：`backend/src/opspilot/tools/adapters/python.py`、`mcp.py`
- 创建：`backend/src/opspilot/agent/state.py`、`prompts.py`、`runner.py`
- 创建：`demo-services/order_service/app.py`、`demo-services/payment_service/app.py`、`demo-services/email_service/app.py`
- 创建：`backend/tests/tools/test_registry.py`
- 创建：`backend/tests/agent/test_runner.py`

- [ ] **步骤 1：编写 ToolDefinition 与 Agent 场景测试**

`ToolDefinition` 必须包含 `effect: read_only|side_effect`、幂等能力和 `supports_reconciliation`。覆盖纯问答、缺订单号追问、正确工具、最大 8 轮/6 次工具终止。

- [ ] **步骤 2：实现 Registry 与受限 Agent**

只暴露 `search_knowledge`、`get_order`、`check_refund_eligibility`、`refund_order`、`get_refund_status`、`send_email`；所有参数通过 Pydantic Schema。

- [ ] **步骤 3：实现合成服务故障模式**

Payment Service 支持 `success`、`timeout_before_effect`、`timeout_after_effect` 和 `unknown_5xx_after_effect`，按服务端业务幂等键保存唯一退款与 Provider reference ID。

- [ ] **步骤 4：验证并提交**

运行：`cd backend && pytest tests/tools tests/agent -q`。预期 PASS。

```bash
git add backend/src/opspilot/tools backend/src/opspilot/agent demo-services backend/tests/tools backend/tests/agent
git commit -m "feat: orchestrate bounded registered tools"
```

### 任务 9：Policy、审批绑定与 Operation 持久化

**文件：**
- 创建：`backend/src/opspilot/execution/models.py`、`policy.py`、`idempotency.py`
- 创建：`backend/src/opspilot/approvals/models.py`、`schemas.py`、`service.py`、`router.py`
- 创建：`backend/alembic/versions/` 下基于当前 head 生成的新 revision（禁止使用固定名称 `0005_operations_approvals.py`）
- 创建：`backend/tests/execution/test_policy.py`
- 创建：`backend/tests/approvals/test_approval.py`

- [ ] **步骤 1：编写金额策略与审批篡改测试**

断言 `<=100 allow`、`100<amount<=1000 require_approval`、`>1000 deny`；审批绑定 `operation_id+arguments_hash+operation_version`，参数或版本变化返回 409。

- [ ] **步骤 2：编写并发重复审批测试**

两个 Reviewer 同时批准，只允许一个事务成功；第二个获得“已处理”事实状态，不重复投递。

- [ ] **步骤 3：实现服务端规范化与幂等键**

退款参数按 Schema 规范化，服务端派生 `refund:{order_id}`；Operation 和键在审批前持久化，唯一冲突返回已有 Operation。

- [ ] **步骤 4：实现审批过期与人工复核审计**

MANUAL_REVIEW 保持终态；管理员只能创建 `manual_review_resolutions`。人工重试必须创建新 Operation 并重新审批。

- [ ] **步骤 5：验证并提交**

运行：`cd backend && pytest tests/execution/test_policy.py tests/approvals -q`。预期 PASS。

```bash
git add backend/src/opspilot/execution backend/src/opspilot/approvals backend/alembic/versions backend/tests/execution backend/tests/approvals
git commit -m "feat: bind durable approvals to immutable operations"
```

### 任务 10：Claim/Lease、fencing 与状态事务

**文件：**
- 创建：`backend/src/opspilot/execution/claim.py`、`state_machine.py`、`executor.py`
- 创建：`backend/tests/execution/test_claim.py`
- 创建：`backend/tests/integration/test_operation_transactions.py`

- [ ] **步骤 1：编写 Claim 规则测试**

READY/RETRYING 可认领；其他状态不可直接认领。Claim 增加 version、生成 claim token、设置 owner/expiry。旧 token 提交结果影响行数为 0。

- [ ] **步骤 2：编写过期 EXECUTING 接管测试**

副作用 Operation 在 EXECUTING 租约过期时，新 Worker 只能原子转为 OUTCOME_UNKNOWN/RECONCILING，不能调用 `refund_order`。

- [ ] **步骤 3：实现条件状态更新**

所有更新使用 `WHERE id=:id AND version=:expected AND claim_token=:token`；状态更新、Run Event 和 Outbox row 同一事务。

- [ ] **步骤 4：实现续租与失权处理**

长任务按租期三分之一续租；续租失败立即停止数据库写入。外部响应迟到时只允许使用当前 token 提交。

- [ ] **步骤 5：验证并提交**

运行：`cd backend && pytest tests/execution/test_claim.py tests/integration/test_operation_transactions.py -q`。预期 PASS。

```bash
git add backend/src/opspilot/execution backend/tests/execution backend/tests/integration/test_operation_transactions.py
git commit -m "feat: fence leased tool operation execution"
```

### 任务 11：副作用分类、重试与 Reconciliation

**文件：**
- 创建：`backend/src/opspilot/execution/errors.py`、`retry.py`、`reconciliation.py`
- 创建：`backend/tests/execution/test_reliability.py`

- [ ] **步骤 1：编写错误分类表测试**

只读工具对连接错误、429、明确可重试 5xx 退避；副作用工具仅在明确未送达/未执行时 RETRYING。读取超时、发送后断线、未知 5xx 必须 OUTCOME_UNKNOWN。

- [ ] **步骤 2：编写核对测试**

timeout-after-effect 通过 Provider reference ID 或幂等键查到退款后进入 SUCCEEDED；确认不存在才 RETRYING；无法确认进入 MANUAL_REVIEW。

- [ ] **步骤 3：编写租约过期重复副作用故障测试**

旧 Worker 调用已生效后租约过期，新 Worker 接管先核对并进入 SUCCEEDED；Payment Service 退款记录严格为 1。

- [ ] **步骤 4：实现分类、退避和核对器**

指数退避带抖动并有最大次数；副作用 Operation 的重试入口必须要求“Provider 明确未执行”证据。

- [ ] **步骤 5：验证并提交**

运行：`cd backend && pytest tests/execution/test_reliability.py -q`。预期 PASS。

```bash
git add backend/src/opspilot/execution backend/tests/execution/test_reliability.py
git commit -m "feat: reconcile uncertain side effect outcomes"
```

### 任务 12：SSE 不丢事件算法

**文件：**
- 创建：`backend/src/opspilot/runs/router.py`、`sse.py`
- 创建：`backend/tests/runs/test_sse.py`
- 创建：`backend/tests/integration/test_sse_recovery.py`

- [ ] **步骤 1：编写历史补发与 Redis 丢通知测试**

SSE 先订阅 Redis，再按 Last-Event-ID 从 PG 补历史；补拉期间通知进入 buffer。永久丢弃一个 Redis 通知后，周期 PG watermark 仍补齐该 seq。

- [ ] **步骤 2：实现 SSE 服务**

通知只含 `{run_id, seq}`，每次按 seq 从 PG 取事实 Event；服务端维护连续发送水位与通知 buffer，按 seq 去重。

- [ ] **步骤 3：验证并提交**

运行：`cd backend && pytest tests/runs/test_sse.py tests/integration/test_sse_recovery.py -q`。预期 PASS。

```bash
git add backend/src/opspilot/runs backend/tests/runs backend/tests/integration/test_sse_recovery.py
git commit -m "feat: stream durable run events without gaps"
```

### 任务 13：Vue 应用壳、登录与 API 客户端

**文件：**
- 创建：`frontend/package.json`
- 创建：`frontend/src/main.ts`
- 创建：`frontend/src/App.vue`
- 创建：`frontend/src/router/index.ts`
- 创建：`frontend/src/stores/auth.ts`
- 创建：`frontend/src/api/client.ts`、`types.ts`
- 创建：`frontend/src/layout/AppLayout.vue`
- 创建：`frontend/src/views/LoginView.vue`
- 创建：`frontend/tests/login.spec.ts`

- [ ] **步骤 1：编写失败的登录测试**

覆盖错误密码提示、成功跳转、刷新恢复、401 清理会话和 USER 不显示审批操作。

- [ ] **步骤 2：初始化 Vue 3 + TypeScript + Vite**

加入 Element Plus、Pinia、Vue Router、Axios、Vitest 和 Playwright；生成类型化 API 错误映射。

- [ ] **步骤 3：实现布局、拦截器和路由守卫**

五个主页面路由为 `/workspace`、`/knowledge`、`/retrieval`、`/approvals`、`/runs/:id`。

- [ ] **步骤 4：验证并提交**

运行：`cd frontend && npm run test && npm run test:e2e -- tests/login.spec.ts`。预期 PASS。

```bash
git add frontend
git commit -m "feat: add authenticated opspilot web shell"
```

### 任务 14：知识库、检索调试器与引用抽屉

**文件：**
- 创建：`frontend/src/views/KnowledgeBaseView.vue`、`RetrievalDebuggerView.vue`
- 创建：`frontend/src/components/DocumentStatusTable.vue`、`RetrievalStageColumn.vue`、`CitationDrawer.vue`
- 创建：`frontend/tests/knowledge-retrieval.spec.ts`

- [ ] **步骤 1：编写上传与检索调试 E2E**

断言上传状态从 UPLOADED 到 READY/FAILED，调试器分别显示 Dense、PostgreSQL FTS、RRF、Reranker 的 rank/score。

- [ ] **步骤 2：编写引用版本定位测试**

点击引用打开 Drawer，断言展示生成时 `document_version`、`chunk_id`、章节、页码和高亮原文；引用抽屉不是独立路由。

- [ ] **步骤 3：实现页面与组件**

严格使用后端状态枚举；轮询只用于文档入库，Run 实时状态使用 SSE。

- [ ] **步骤 4：验证并提交**

运行：`cd frontend && npm run test && npm run test:e2e -- tests/knowledge-retrieval.spec.ts`。预期 PASS。

```bash
git add frontend/src/views frontend/src/components frontend/tests/knowledge-retrieval.spec.ts
git commit -m "feat: visualize ingestion retrieval and citations"
```

### 任务 15：工作台、审批、时间线与核心 E2E

**文件：**
- 创建：`frontend/src/views/AgentWorkspaceView.vue`、`ApprovalCenterView.vue`、`RunDetailView.vue`
- 创建：`frontend/src/components/ToolCallCard.vue`、`RunTimeline.vue`
- 创建：`frontend/src/composables/useRunEvents.ts`
- 创建：`frontend/tests/refund-flow.spec.ts`
- 创建：`backend/tests/e2e/test_refund_flow.py`

- [ ] **步骤 1：编写 SSE reducer 单元测试**

`useRunEvents` 维护 `expected_seq` 和 buffer：重复不渲染、乱序暂存、缺口 HTTP 补拉、连续后消费 buffer。输入 `[1,3,2,3,5,4]` 最终渲染 `[1,2,3,4,5]` 各一次。

- [ ] **步骤 2：编写审批交互测试**

USER 无按钮；REVIEWER/ADMIN 点击后立即禁用；409 显示已处理/过期/参数变化。批准只显示“允许执行”，不显示“退款成功”。

- [ ] **步骤 3：实现可靠状态展示**

明确展示 EXECUTING、OUTCOME_UNKNOWN、RECONCILING、MANUAL_REVIEW；断线显示重连，不把 Run 标为失败。

- [ ] **步骤 4：编写完整退款 E2E**

ORD-002 → 引用 → 待审批 → 批准 → timeout-after-effect → RECONCILING → SUCCEEDED；断线、重复、乱序后客户端 seq 集合与 PG 完全一致，Payment Service 退款记录为 1。

- [ ] **步骤 5：验证并提交**

运行：`make test-e2e`。预期所有核心流程 PASS。

```bash
git add frontend backend/tests/e2e
git commit -m "feat: deliver auditable approval refund workflow"
```

### 任务 16：v1.0 评测数据集与确定性指标

**文件：**
- 创建：`evaluation/datasets/dev.jsonl`、`test.jsonl`、`agent_tasks.jsonl`、`attacks.jsonl`
- 创建：`backend/src/opspilot/evaluation/models.py`、`schemas.py`、`retrieval_metrics.py`、`answer_metrics.py`、`agent_metrics.py`、`reliability_metrics.py`
- 创建：`backend/alembic/versions/` 下基于当前 head 生成的新 revision（禁止使用固定名称 `0006_evaluation.py`）
- 创建：`backend/tests/evaluation/test_schema.py`、`test_metrics.py`

- [ ] **步骤 1：编写数据集 Schema 测试**

用例必须包含版本、相关 Chunk、必要/禁止事实、预期/禁止工具、追问、审批和最终业务状态；冻结 test 集前记录 SHA-256。

- [ ] **步骤 2：编写手算指标测试**

固定小排名验证 Recall@5、Precision@5、MRR、nDCG@5；固定工具集合验证 Precision/Recall/F1；断言未经审批执行率与重复副作用率。

- [ ] **步骤 3：实现模型与指标**

确定性验证引用 ID、订单号、金额、工具名/参数、审批状态和最终数据库事实。LLM Judge 只能补充 Correctness/Faithfulness，失败不能阻止确定性指标。

- [ ] **步骤 4：生成并复核 200+ 合成用例**

dev 60、test 140，攻击集独立；最终 test 集在参数冻结前禁止运行。提交生成脚本和复核记录，不能手改报告数字。

- [ ] **步骤 5：验证并提交**

运行：`cd backend && pytest tests/evaluation/test_schema.py tests/evaluation/test_metrics.py -q`。预期 PASS。

```bash
git add evaluation/datasets backend/src/opspilot/evaluation backend/alembic/versions backend/tests/evaluation
git commit -m "test: add versioned opspilot evaluation corpus"
```

### 任务 17：异步实验 Runner、故障矩阵与评测看板

**文件：**
- 创建：`backend/src/opspilot/evaluation/runner.py`、`tasks.py`、`router.py`、`report.py`
- 创建：`evaluation/experiments/vector-only.yaml`、`hybrid.yaml`、`hybrid-rerank.yaml`
- 创建：`frontend/src/views/EvaluationDashboardView.vue`
- 创建：`frontend/src/components/MetricComparisonChart.vue`
- 创建：`backend/tests/evaluation/test_runner.py`
- 创建：`frontend/tests/evaluation.spec.ts`

- [ ] **步骤 1：编写配置快照与重复实验测试**

Runner 固定数据集 SHA、模型、Embedding、Reranker、top-k、Prompt、随机参数和并发；Agent 用例至少 3 次，报告均值、标准差和失败样本。

- [ ] **步骤 2：实现公开 API 驱动的异步 Runner**

Evaluation 只能调用公开服务接口；任务通过 ARQ 执行，结果写 evaluation_runs/cases。

- [ ] **步骤 3：实现故障注入矩阵**

覆盖执行前、外部副作用后本地提交前、结果落库后故障；每点至少 20 次，输出恢复率、重复副作用、丢失操作和恢复耗时。

- [ ] **步骤 4：实现 JSON/CSV/HTML 报告和看板**

展示 Recall@5、MRR、nDCG@5、引用正确率、任务成功率、P95、未经审批执行率和重复副作用，并包含实验配置与失败案例。

- [ ] **步骤 5：验证并提交**

运行：`make eval-smoke && cd frontend && npm run test:e2e -- tests/evaluation.spec.ts`。预期 PASS。

```bash
git add backend/src/opspilot/evaluation backend/tests/evaluation evaluation/experiments frontend/src/views/EvaluationDashboardView.vue frontend/src/components/MetricComparisonChart.vue frontend/tests/evaluation.spec.ts
git commit -m "feat: publish reproducible opspilot evaluations"
```

### 任务 18：可观测性、部署、文档与 v1.0 验收

**文件：**
- 创建：`backend/src/opspilot/observability/tracing.py`、`redaction.py`
- 创建：`backend/tests/observability/test_redaction.py`
- 创建：`deploy/Dockerfile.backend`、`Dockerfile.frontend`、`nginx.conf`
- 创建：`docs/architecture.md`、`retrieval.md`、`reliable-execution.md`、`evaluation.md`、`interview-guide.md`
- 创建：`README.md`
- 修改：`docker-compose.yml`

- [ ] **步骤 1：编写隐私与预算测试**

断言邮箱、手机号、API Key、Authorization 和完整 Prompt 不进入 Attempt、Run Event、日志或 Trace；每 Run 模型次数、工具次数、Token 和总时长超限后安全终止。

- [ ] **步骤 2：实现 Trace 与脱敏**

`agent.run` 下包含 retrieval、rerank、llm、tool.policy、tool.execute、tool.reconcile；统一携带 run_id，只记录输入摘要和结构化决定。

- [ ] **步骤 3：完成非 root 容器与健康检查**

Compose 启动 Web、API、Worker、Outbox Publisher、PostgreSQL、Redis、Order、Payment、Email；共享 Volume 只授予 API/Worker 必要权限。

- [ ] **步骤 4：编写架构与面试证据文档**

解释 PostgreSQL+pgvector、RRF/Reranker、数据库权限过滤、Prompt 外策略、业务幂等键、Transactional Outbox、Lease fencing 局限和 OUTCOME_UNKNOWN 核对。简历数字只引用 `evaluation/reports/<run_id>/results.json`。

- [ ] **步骤 5：执行完整发布验证**

```bash
docker compose build
docker compose up -d
make migrate
make seed
make check
make test-integration
make test-e2e
make eval-smoke
```

预期全部退出码为 0；退款 E2E 只有一条退款；SSE seq 与 PG 一致；报告成功生成。

- [ ] **步骤 6：冻结参数并运行最终测试集**

记录配置 SHA 后只运行一次冻结的 `test.jsonl`，保存原始结果和失败案例，不删除失败或手改指标。

- [ ] **步骤 7：提交发布版本**

```bash
git add backend frontend deploy docker-compose.yml docs README.md evaluation/reports
git commit -m "release: complete opspilot v1 evaluation build"
git tag v1.0.0
```
