# OpsPilot 开发进度

> 最后更新：2026-08-26
> 当前分支：`plan/opspilot-core-mvp`  
> 当前阶段：M2–M3 / 任务 1–11 已通过督导复审；任务 12 可靠 SSE 已实现，等待督导复审

## 总体进度

| 里程碑 | 任务 | 状态 | 当前结果 |
|---|---:|---|---|
| M1 基础与知识入库 | 1–4 | 已完成并批准 | 登录、权限上传、可靠异步入库、Chunk、Vector、PostgreSQL FTS |
| M2 可解释 RAG | 5–7 | 已完成 | 任务 5–7 已通过督导复审 |
| M3 可靠 Agent | 8–12 | 进行中 | 任务 8–11 已通过督导复审；任务 12 已实现、等待复审 |
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
- 状态：**已通过督导复审（2026-08-24）**，验收提交 `d5ba8f3`（READY 过滤、CI、`effective_at`、文档同步）与 `b490bad`（CI 顺序、KB 最小权限边界、状态文案）。0009 迁移链、READY/权限 SQL 过滤与真实 PostgreSQL 测试均无阻塞。

### 任务 6：Reranker、上下文预算与引用回答

- 实现 BGE Reranker（OpenAI-compatible `/rerank`）；超时通过 `rerank_with_fallback` 保持 RRF 顺序并记录 `reranker_status=degraded`，不吞掉错误。
- 实现 Context Builder 与令牌预算：片段按输入（rerank 后）顺序贪心放入，永不超预算；稳定引用 ID `[DOC:<document_id>#<chunk_id>]`，携带标题、文档版本、章节、`effective_at` 与页码。
- 实现 DeepSeek V4 Flash 结构化回答 Provider（`GroundedAnswer` 契约）与 Citation Validator：事实型回答无有效引用时转为 `insufficient_evidence=True`，不会只删无效引用后保留确定性回答；有效引用保存快照（`document_id + document_version + chunk_id + section_path + page`）。
- Query Rewrite 放在 Generation（`rewrite.py`），由 Agent Orchestrator 按需调用，未放入 Retrieval；不实现 Run Journal、Tool Gateway、SSE 或前端。
- 状态：**已通过督导复审（2026-08-24）**，验收提交 `955fe22`（任务 6 实现）、`1f0e71c`（P1 修复：引用 fail-closed + 真实 HTTP 超时降级）、`05754bd`（墙钟超时取消路径回归测试）。

### 任务 6 复审修复（P1）

- Citation Validator 改为 **fail-closed**：事实回答只要包含任一未检索、过期或不存在的引用，整体转为 `insufficient_evidence=True`，不再保留有效引用后继续输出确定性回答。
- Reranker fallback 覆盖真实 HTTP 超时：`BgeReranker` 将 `httpx.TimeoutException` 在 Provider 边界转换为领域超时异常 `RerankerTimeoutError`（`TimeoutError` 子类），`rerank_with_fallback` 统一降级到 RRF 顺序；无关异常（如 `RuntimeError`）不被吞掉，直接传播。

### 任务 7：Run Journal 与 Transactional Outbox

- 新增 `agent_runs` 表持有单调 `next_seq` 计数器；`run_events`（`UNIQUE(run_id, seq)`）保存事件；`event_outbox`（`UNIQUE(run_id, seq)`）与事件同事务写入。
- 实现事务内 Journal API `append_event(session, run_id, event_type, payload)`：用 `UPDATE agent_runs SET next_seq = next_seq + 1 WHERE id = :run_id RETURNING next_seq` 原子递增并锁定 Run 行，写 `run_events` 与 `event_outbox`，不自行 commit；不存在的 run 抛 `RunNotFoundError`。
- 实现 Outbox Publisher `publish_pending_events`：`FOR UPDATE SKIP LOCKED` 获取未投递 row，只向 Redis 发布 `{run_id, seq}`（频道 `run_events:{run_id}`），成功后标记 delivered；失败记录 `last_error` 并保持未投递以便重试；重复投递安全。
- 真实 PostgreSQL 测试证明：20 个并发追加 seq 连续唯一、Outbox insert 失败时 Event 与 seq 一并回滚、publisher 投递后不重复、失败通知重试；真实 Redis pub/sub 收到 `run_events:{run_id}` 消息。
- 新增 `0010_runs_journal_outbox` 迁移（基于 `0009_document_effective_at`），Alembic 位于 `0010` head。
- 状态：**已通过督导复审（2026-08-24）**。任务 7 实现提交为 `911c5a0`（feat: persist run events with transactional outbox），复审修复提交 `43223ec`、`f81177e`、`c50a3c4`、`fac3b99`。非法 Unicode 字典键在序号更新前抛 `PayloadInvalidError`；Token 用量元数据（snake/camelCase）保留、access/session/id/refresh Token 继续脱敏；定向 28 passed、完整 123 passed。

### 任务 7 复审修复（P1）

- Journal 写入边界统一递归脱敏与载荷限制：`sanitize_payload` 在 `append_event` 持久化前执行。
  - 敏感键识别基于 snake/kebab/camelCase 词元（非整串后缀匹配）：强敏感名词（`secret`/`password`/`credential`/`cookie`/`authorization` 及复数）任意位置命中即脱敏；`token` 仅在非数量语境且非用量元数据语境脱敏（`token_count`/`token_size` 保留，`prompt_tokens`/`completion_tokens`/`input_tokens`/`output_tokens`/`total_tokens`/`token_usage_count` 等用量元数据保留，`access_token`/`session_token`/`id_token`/`refresh_token` 继续脱敏）；`key` 仅在前置凭据限定词（`api`/`private`/`signing`/`access`/`auth`/`master` 等）时脱敏，`partition_key`/`document_key`/`sort_key`/`cache_key` 等结构元数据保留。覆盖 `*_secret_value`、`api_key_value`、`authorization_header`、`apiKey`/`APISecret` 等 camelCase 组合键。
  - 值内邮箱与手机号（`Bearer`/`Basic` 凭证方案一并脱敏）、超长字符串截断到 `MAX_STRING_LENGTH`、整体序列化字节数超过 `MAX_PAYLOAD_BYTES` 时 fail-closed 抛 `PayloadTooLargeError`。
  - 递归入口显式校验：非字符串键、不支持的 value 类型、`NaN`/`Infinity`、循环引用（`id()` 路径追踪）、超过 `MAX_DEPTH` 的嵌套、以及含孤立代理项（lone surrogate）的字符串（键与值一致校验），统一转为 `PayloadInvalidError`（全部在 `next_seq` 更新之前完成，不消耗 seq、不回写任何行）。
- Publisher 采用 poison row 隔离：单条失败记录 `attempts`/`last_error` 后 `continue`，并在本次调用内排除已失败 ID，使一条永久失败的 poison row 不再阻塞其他可处理事件；投递按 `(run_id, seq)` 尽力排序（`SKIP LOCKED` 在并发 Publisher 下仅提供尽力排序，不保证严格顺序，SSE 契约容忍乱序并从 PostgreSQL 补拉）。新增“首条永久失败、后续事件仍成功投递”的回归测试。

### 任务 8：Tool Registry、Agent Loop 与演示服务

- 定义 `ToolDefinition`（`effect: read_only|side_effect`、`idempotency_capable`、`supports_reconciliation`、`input_schema: type[BaseModel]`、`invoke: Callable[..., Awaitable[ToolResult]]`）与 `ToolResult(ok/data/error)`；`ToolNotFoundError`/`ToolArgumentError` 区分未知工具与参数校验失败。
- 受限 6 工具注册表 `build_tool_registry(deps)`：`search_knowledge`/`get_order`/`check_refund_eligibility`/`get_refund_status` 为 READ_ONLY，`refund_order`/`send_email` 为 SIDE_EFFECT；全部工具声明幂等能力与 `supports_reconciliation`，供任务 9/10 选择重试或核对。
- 所有工具参数经 Pydantic 输入 schema 校验（`validate_arguments` 把 `ValidationError` 包装为 `ToolArgumentError`）；Design B：适配器接收已校验的参数模型而非裸标量。
- 有界 Agent Loop `AgentRunner`：每轮从决策提供方取一个动作，仅通过注册表执行工具调用，最多 8 轮或 6 次工具调用即 `bounded` 终止；纯问答、澄清可直返；`DecisionProvider` Protocol 抽象 LLM，`DeepSeekAgentDecider` 以 JSON 模式对话并解析决策契约（`answer`/`clarify`/`tool_call`）。
- HTTP 适配器（`get_order_adapter`/`check_refund_eligibility_adapter`/`refund_order_adapter`/`get_refund_status_adapter`/`send_email_adapter`）：超时映射为可重试 `ToolResult`、非成功状态码映射为失败结果；`mcp.py` 提供远期 MCP 适配器契约（当前 6 工具均不走 MCP）。
- `demo-services/` 演示服务：order/payment/email 三个 FastAPI 服务；payment 以订单号为服务端业务幂等键（重复退款共享同一 `refund_id`/`provider_reference`，201-if-new-else-200），支持 `success`/`timeout_before_effect`/`timeout_after_effect`/`unknown_5xx_after_effect` 故障模式，供任务 9/10 验证重试与核对语义。项目不引入 uvicorn/Dockerfile，测试以 in-process `ASGITransport` 验证。
- 状态：**已通过督导复审（2026-08-25）**。实现提交 `db2cae5`（feat: orchestrate bounded registered tools）；复审修复提交 `8e8adfb`（fix: harden agent provider decision contract：Provider 边界不再发送缺少 `tool_call_id` 的 `role=tool` 消息而改为标注为不可信工具输出的上下文消息、外部 JSON 决策采用严格 fail-closed 校验、AgentRunner 显式抛 `DecisionError` 替代 assert）；定向 35 passed、完整 158 passed。

### 任务 9：Policy、审批绑定与 Operation 持久化

- 确定性退款策略 `decide_refund_policy(amount)`：`<=100` ALLOW、`100<amount<=1000` REQUIRE_APPROVAL、`>1000` DENY；非有限/非正金额 fail-closed 抛 `PolicyError`；策略为纯函数，prompt/Agent 无法绕过。
- 服务端派生业务幂等键 `refund:{order_id}`（无轮次/时间戳/ID 后缀）与稳定参数哈希；`tool_operations` 持久化 Operation（run_id、tool_name、normalized_arguments、arguments_hash、idempotency_key、status、version、policy_decision、`retry_of_operation_id` 自引用审计链）。
- 幂等占用独立表 `operation_idempotency_occupancy`（`UNIQUE(tool_name, idempotency_key)`）：同一业务键同一时刻最多一个可执行 Operation，历史终态 Operation 保留相同业务键作审计；并发重复创建经事务级 advisory lock 串行化并收敛返回同一当前 Operation。
- 审批绑定不可变 `operation_id + arguments_hash + operation_version`：`decide_approval` 用 `SELECT ... FOR UPDATE` 串行化并发 Reviewer，仅第一个转换状态、后续返回 already_processed 事实；参数/版本被篡改抛 `ApprovalVersionConflictError`（409）、过期抛 `ApprovalExpiredError`（409）；REJECT 在状态变终态后释放占用。
- MANUAL_REVIEW 终态不可变：仅 ADMIN 可写 `manual_review_resolutions` 审计；仅存在 outcome=`RETRY_NEW_OPERATION` 的 resolution 时，重试在同一 PostgreSQL 事务中释放旧占用并创建新 Operation（`retry_of_operation_id` 审计链，重新经过 Policy 与 Approval）。门控由数据库触发器 `guard_occupancy_repoint`/`guard_occupancy_release` 证明（测试用原始 SQL UPDATE/DELETE 直接验证被拒绝），非应用层先查后插。
- 原子性：占用切换、Operation、run_event 与 outbox 同一事务；注入失败后断言占用/Operation/事件/seq 全部回滚。HTTP 路由 `/api/v1`：审批决策、人工复核 resolution（ADMIN）、重试（ADMIN）。
- 状态：**已通过督导复审（2026-08-25）**。批准提交：`e82a57c`（feat: bind durable approvals to immutable operations，主实现）、`276a5cc`（feat: bind retry authorizations one-shot to immutable replacements，一次性重试授权/副作用路由/occupancy 加固）、`26ede61`（fix: make replacement binding immutable and undeletable，P2 绑定不可变与删除保护）。

### 任务 9 复审修复（P1，2026-08-25）

督导复审五项修复已按 Red → Green → Refactor 完成：

1. **封闭副作用绕过路径**：`AgentRunner` 不再直接执行任何 SIDE_EFFECT 工具的适配器；`refund_order` 决策被路由到注入的 `side_effect_handler`（无 handler 时 fail-closed 返回错误结果），READ_ONLY 工具仍按任务 8 契约直接调用。新增 `build_refund_operation_handler` 生产接线：refund_order 决策 → 确定性 Policy → durable Operation 创建流程（同一事务写占用/审批/事件/outbox），任务 9 全程不调用 Payment Provider。回归测试证明任意金额（50/250/5000）的 `refund_order` 决策都不会触发 payment adapter。
2. **一次性消费重试授权**：`manual_review_resolutions.replacement_operation_id`（迁移 `0012`，`ON DELETE SET NULL`）持久绑定 resolution 与其唯一 replacement Operation。重试事务内用条件 `UPDATE ... WHERE replacement_operation_id IS NULL` 原子消费授权（无进程锁、无先查后插），替换 Operation 即使被 Policy 判为 DENIED 也是该 resolution 的确定结果；后续及并发请求都返回同一个 replacement，不再创建额外 DENIED 行。测试覆盖顺序、并发（ALLOW/REQUIRE_APPROVAL/DENY 各最多一个 replacement）与 DENY 确定结果。
3. **加固 occupancy repoint**：`guard_occupancy_repoint` 触发器重写为数据库级验证新占用者——必须存在、`tool_name`/`idempotency_key` 与占用一致、`retry_of_operation_id` 指向旧 MANUAL_REVIEW Operation、且是某有效 RETRY_NEW_OPERATION resolution 的 `replacement_operation_id`。新增 4 个原始 SQL 负向测试：错误工具、错误业务键、错误谱系、任意 Operation（正确 tool/key/lineage 但未绑定 resolution）均无法 repoint。
4. **原子性**：resolution 消费、replacement 创建、占用切换、状态事件与 outbox 同一事务提交；注入 append_event 失败后 resolution 的 claim、replacement、事件与 outbox 全部回滚，resolution 仍可被干净地再次消费。
5. **质量门禁**：定向 65 passed、完整 205 passed、Ruff/Mypy/Alembic `0012 (head)`/PG/Redis 健康全部通过，见下方验证基线。

### 任务 9 复审修复（P2，2026-08-25）

督导再次复审驳回一项 P2 数据库完整性问题：`manual_review_resolutions.replacement_operation_id` 原用 `ON DELETE SET NULL` 且无数据库层不可变保护——原始 SQL 可清空或改指已消费的重试授权；删除不持有 occupancy 的 DENIED replacement 也会把绑定重置为 NULL，使同一授权再次被消费。按 Red → Green → Refactor 完成最小修复：

1. **绑定不可变**：新增数据库触发器 `guard_resolution_binding`（`BEFORE UPDATE OF replacement_operation_id`）——`OLD IS NOT NULL AND NEW IS DISTINCT FROM OLD` 时 `RAISE EXCEPTION`，即从 NULL 首次绑定为合法 replacement 后永久不可清空、不可改指；NULL→value 的首次消费仍唯一允许。
2. **删除保护**：外键删除行为由 `ON DELETE SET NULL` 改为 `ON DELETE RESTRICT`（迁移 `0013_resolution_binding`，基于 `0012` 顺序新增、未改写已提交迁移），已绑定为 replacement 的 Operation 不得删除，Operation 审计记录不丢失。
3. **原始 SQL 负向测试**（tests/approvals/test_approval.py，真实 PostgreSQL）：UPDATE 已绑定值为 NULL 被拒、改指另一 Operation 被拒、DELETE 已绑定 DENIED replacement（不持有 occupancy）被外键拒绝、正常首次绑定仍成功、事务失败后原绑定保持不变。
4. **迁移链证据**：0001→0013 全链在全新数据库验证通过；0013 upgrade/downgrade/upgrade 往返验证通过（含迁移往返测试 `test_0013_resolution_binding_roundtrip`）；ORM 外键同步为 `ondelete="RESTRICT"`，与数据库行为一致。
5. **质量门禁**：定向 70 passed、完整 211 passed、Ruff format 144 文件、Ruff check 通过、Mypy 72 源文件无问题、Alembic `0013_resolution_binding (head)`、PG/Redis 健康全部通过，见下方验证基线。

### 任务 10：Claim/Lease、fencing 与状态事务

- **Claim 规则**：只有 READY/RETRYING 可通过条件 UPDATE 认领 → EXECUTING；`version` 单调 +1；生成一次性 `claim_token`（`secrets.token_urlsafe(32)`）；设置 `lease_owner`/`lease_expires_at`。并发 worker 至多一个胜出（`test_concurrent_claim_allows_exactly_one_winner`，asyncio.gather 双 session 串行化后 1 胜 1 抛 `OperationNotClaimableError`）。
- **Fencing 回写**：renewal/result 写入均匹配 `id + expected version + claim_token + lease_owner + 未过期 lease`；stale version/token、wrong owner、expired lease 均修改 0 行并抛 `LeaseConflictError`；晚到的旧 worker 响应不能覆盖新持有者的 result（`test_late_response_from_old_worker_cannot_overwrite_new_holder`）。
- **过期租约恢复**：READ_ONLY 从 EXECUTING 原子转 RETRYING（可重新认领）；SIDE_EFFECT 转 OUTCOME_UNKNOWN（不在 CLAIMABLE_STATUSES，claim 永远失败 → Provider 永远不会被重新调用）；EXECUTING 且租约过期时不重新调用 Provider。实际 reconciliation 归 Task 11。
- **续租与失权**：provider 调用期间由**并发 heartbeat** 每 ~`lease_seconds/3` 续租（独立 session/事务、走 PostgreSQL `clock_timestamp()`、提交 `operation_lease_renewed` event/outbox、不 bump version）——超过一个 lease 窗口的长调用仍保有写入权；renewal 失败后旧 worker 立即失权（`LeaseConflictError`）。Executor 分两个事务：claim 先提交（持久 lease），result write 在第二个 fenced 事务中 —— 失去租约时操作保持 EXECUTING（不可重新认领），而不是回滚回 READY。
- **状态事务**：每次状态更新 + `run_event` + `event_outbox` 在同一个 PostgreSQL 事务中（`next_seq` 原子递增）；注入 Event/Outbox 失败时 operation status/version/token/lease/seq 全部回滚（claim/success/renewal 三种路径各有回归测试）；Redis publish 不属于业务事实事务。
- **状态机**：`state_machine.py` 定义合法转换（含 OUTCOME_UNKNOWN/RECONCILING 完整性定义），非法转换 fail-closed 抛 `IllegalStateTransitionError`；MANUAL_REVIEW/SUCCEEDED/FAILED/DENIED/REJECTED 终态不可认领；未破坏任务 9 的 approval/occupancy/immutability 约束（EXECUTING occupancy 受 `guard_occupancy_release` 扩展保护列表保护）。
- **迁移**：`0014_operation_lease_fencing`（`ADD VALUE` 扩 4 个状态，OID 不变无需 dispose；新增 claim_token/lease_owner/lease_expires_at/provider_reference_id/result_payload 5 个可空列；`guard_occupancy_release` 保护列表扩展到 8 个非终态）。downgrade 重建枚举并恢复 4 状态保护列表。
- 提交：`f36e435`（feat: fence leased tool operation execution，7 文件 +1624 行，含 `execution/claim.py`、`execution/state_machine.py`、`execution/executor.py`、`tests/execution/test_claim.py` 20 个定向测试、`tests/integration/test_operation_transactions.py` 5 个事务测试）。
- **复审自检修复（2026-08-25，提交 `b6729c1` "fix: harden leased operation fencing"）**：发现并修复的唯一真实缺陷是时间语义（风险 5）——租约到期判断原先依赖应用本地时钟 `datetime.now(UTC)`。修复为：租约决策一律解析到 PostgreSQL `clock_timestamp()`（新增 `_resolve_now`/`_clock_value`，`now`/`Clock` 仅作为确定性测试注入点）；`_ensure_aware_utc` 将 naive 注入时钟规范化为显式 UTC。新增 8 个 DB 时钟测试（默认走数据库时钟、时钟偏移被 fencing 兜底而非依赖时间、fenced 写入在租约过期后被拒、并发恢复单胜者、invoke 抛异常/取消后保持 EXECUTING）与 2 个事务测试（recovery/result write 事务注入失败全回滚）。其余 7 项风险（claim 并发、两事务执行、fencing、续租生命周期、过期恢复、状态机/终态/占用、迁移 0014）审查后代码正确，无需改动。迁移 0014↔0013 往返与"带 EXECUTING 数据降级必须响亮失败且原子回滚"已在 scratch 数据库验证通过。
- **P1 复审驳回修复（2026-08-25，提交 `3c7cecb` "fix: renew operation leases during provider calls"）**：督导指出 `b6729c1` 的"无后台任务、内联续租"是错误结论——`await invoke()` 期间完全没有续租，调用时长超过 lease 时租约必过期。修复：`executor.py` 引入**与 invoke 并发的周期 heartbeat**（`asyncio.Task`，每 ~`lease_seconds/3` 用独立 session/事务续租，走 `clock_timestamp()`，提交 `operation_lease_renewed` event/outbox，不 bump version）。生命周期收敛：invoke 正常返回 → 停并 await heartbeat → fenced 写结果；invoke 抛异常/worker 取消 → 停 heartbeat、不写终态；heartbeat 失权 → 立即撤销 provider 并抛 `LeaseConflictError`（不响应取消的 provider 经 1s 有界宽限放弃而非无限等待）；并发/竞争一律由 fenced DB 写入决定；结束后无遗留 asyncio Task。同时：把 `token/expires_at` 的 `assert` 换成显式领域异常 `LeaseStateError`；SIDE_EFFECT 模糊失败（无法证明 Provider 未执行）不再直接 `mark_failed`，改为最小安全处理 `mark_unknown → OUTCOME_UNKNOWN`（新增 `operation_outcome_unknown` 事件，实际核对归 Task 11），契约最小扩展为 `ToolResult.provider_not_called`（默认 None=未知）。Red 证据：4 个 heartbeat 特性测试（invoke 阻塞期间 lease 延长、多 interval 多次续租、DB 时钟非应用时钟、heartbeat 运行时 `recover_expired` 不能接管）在 `b6729c1` 上失败；修复后全绿。
- **独立复审修复（2026-08-26，提交 `9863d4a` "fix: supervise detached operation providers"）**：发现 `3c7cecb` 对不响应取消的 provider 仅做 `shield + 1s` 有界等待后丢弃引用，却错误声称无遗留 Task。现在超时 provider 进入进程级受控集合，完成回调统一读取异常并移除，worker shutdown/tests 可显式 drain；正常、异常、取消和失权路径的 heartbeat 仍 cancel 且 await，不进入 detached 集合。补齐同轮双失败、清理 race、外部取消、不响应取消 provider 管理/回收、`provider_not_called=True/False/None` 和 OUTCOME_UNKNOWN 原子回滚测试。取消 Python Task 不能撤销已到达 Provider 的副作用；fencing 只禁止旧 worker 后续数据库回写。

### 任务 11：副作用分类、安全重试与 Reconciliation

- 结构化 `ProviderFailureKind` 与 `FailureDisposition` 不读取错误字符串：READ_ONLY 的连接错误、429、明确可重试 5xx 进入 RETRYING；SIDE_EFFECT 仅 `provider_not_called=True` 可安全 RETRYING，读取超时、发送后断连、未知 5xx、False/None 均进入 OUTCOME_UNKNOWN。
- `RetryPolicy` 提供持久化指数退避（`retry_not_before`）和 4 次上限；提前 claim 被数据库条件拒绝，达到上限后确定性 FAILED，不无限循环。
- 专用 reconciliation claim 仅接受 OUTCOME_UNKNOWN，原子进入 RECONCILING 并生成独立 token/owner/version/lease；普通 claim 仍只接受 READY/RETRYING。核对可使用 `provider_reference_id` 或稳定业务键 `refund:{order_id}`，并发核对仅一胜。
- Provider 已退款 → SUCCEEDED 并保存原 reference；明确 NOT_REFUNDED → RETRYING；查询失败或语义不明 → MANUAL_REVIEW。过期 reconciliation lease 原子返回 OUTCOME_UNKNOWN，不调用 Provider。
- 新增 `operation_attempts` 与迁移 `0015_operation_attempts`；执行/核对的状态、Attempt、run_event、outbox、next_seq 同事务，失败注入完整回滚。Attempt 请求、响应、错误复用递归脱敏、PII 清理和 1000 字符限制。
- 真实 Payment Service 验证 timeout-after-effect 与 unknown-5xx-after-effect 均只产生 1 条退款记录，随后核对为 SUCCEEDED；fencing 仍匹配 version/token/owner/live lease。
- detached provider supervisor 已接入 ARQ `WorkerSettings.on_shutdown`，worker 退出时显式 drain。

## 当前验证基线

任务 11 实现后的真实结果（2026-08-26，等待督导复审）：

- Task 11 定向：`24 passed in 1.86s`（`tests/execution/test_reliability.py`）。
- 扩展可靠执行定向：`81 passed`（Task 11 + claim/transaction/demo-service）。
- 完整测试：`283 passed in 38.49s`。
- Ruff format：`127 files already formatted`（排除既有且禁止触碰的 ACL 临时目录 `backend/JavaProjectsOpsPilot.pytest-temp-agent/`）。
- Ruff check：全部通过。
- Mypy `--no-incremental`：79 个源文件无问题。
- Alembic：`0015_operation_attempts (head)`；隔离 scratch 数据库 0001→0015 全链和 0015→0014→0015 往返通过。

Task 11 Attempt 生命周期复审修复（2026-08-26，等待再次督导复审）：

- 过期 EXECUTION Attempt 不再永久停留 RUNNING：READ_ONLY 恢复关闭为 `ABANDONED`，SIDE_EFFECT 恢复关闭为 `OUTCOME_UNKNOWN`；`completed_at` 使用 PostgreSQL `clock_timestamp()`，固定错误摘要经过既有脱敏限长边界。
- Attempt 关闭与 Operation、run_event、outbox、next_seq 位于同一调用者事务；事件失败注入证明全部回滚。只锁定并关闭最新 RUNNING EXECUTION Attempt，历史已完成 Attempt 不变，恢复后 Attempt number 连续。
- 新增 `0016_running_execution_attempt` 部分唯一索引，数据库保证每个 Operation 同时最多一个 `kind=EXECUTION,status=RUNNING` Attempt。
- 本轮 Task 11 定向 `29 passed`；Task 10+11 核心可靠执行组合 `77 passed`；完整 `288 passed in 39.01s`；Ruff format/check、Mypy（79 files）通过；Alembic 0016↔0015 往返及独立数据库 0001→0016 全链通过；PostgreSQL/Redis healthy。

Task 11 / 0016 脏数据升级兼容修复（2026-08-26，已通过督导复审）：

- `0016_running_execution_attempt` 建唯一索引前确定性回填 0015 历史脏数据：EXECUTING 按 `(attempt_number DESC, id DESC)` 仅保留最新 RUNNING；OUTCOME_UNKNOWN/RECONCILING 全部关闭为 `OUTCOME_UNKNOWN`；RETRYING 与其他非执行状态关闭为 `ABANDONED`。只更新 RUNNING EXECUTION，不覆盖历史完成 Attempt。
- 回填使用 `clock_timestamp()`、固定安全错误摘要；状态以 `operation.status::text` 比较，兼容 0014 新 enum 值与 0016 在同一 Alembic upgrade 事务中的 PostgreSQL 可见性规则。
- 真实 PostgreSQL 脏数据 Red：原 0016 创建索引报 `UniqueViolationError: Key (operation_id) ... is duplicated`。Green：迁移测试 2 passed；包含脏数据升级、索引拒绝第二条 RUNNING、0016→0015→0016 往返及建索引失败全事务回滚。
- 最新 Task 11 定向 `31 passed in 4.52s`；Task 10+11 核心组合 `79 passed in 29.97s`；完整 `290 passed in 42.14s`；Ruff/Mypy、0001→0016/往返、PostgreSQL/Redis 均通过。
- **督导最终批准（2026-08-26）**：批准提交 `ab89d40`（Task 11 主实现）、`d27ec00`（关闭遗留 RUNNING EXECUTION Attempt 与唯一约束）、`d128eb9`（0016 脏历史数据回填与升级原子性）；迁移为 `0015_operation_attempts` + `0016_running_execution_attempt`。督导独立定向 `31 passed`，本次文档闭环重新完整运行 `290 passed in 41.82s`。
- PostgreSQL：healthy，`pg_isready` accepting connections。
- Redis：healthy，`PONG`。

### 任务 5 复审修复

- Dense/FTS 候选 SQL 现在同时过滤 `KnowledgeScope` 与 `Document.status == READY`；真实 PG 集成测试覆盖六种状态，仅 READY 可检索，非 READY（UPLOADED/PARSING/CHUNKING/INDEXING/FAILED）均在候选层被排除。
- Dense 与 FTS 相同分数按 `chunk_id` 稳定排序，保证跨运行结果确定。
- GitHub CI 启动 PostgreSQL/pgvector 与 Redis 服务，执行健康检查与 Alembic upgrade head 后再运行完整测试（含集成）。
- 明确权限为 KB 级：部门与访问级别定义在 KnowledgeBase，文档继承；规格、模型、上传与测试已同步。
- Document 增加 `effective_at`（默认上传时间），新增 `0009_document_effective_at` 迁移。
- 实现计划中任务 7/9/16 的固定迁移文件名（0004/0005/0006）已移除，改为基于当前 head 生成新 revision。

## 设计修订（2026-08-25，督导裁决）

- 任务 9 的幂等键唯一性约束从 `tool_operations(tool_name, idempotency_key)` 移到独立占用表 `operation_idempotency_occupancy`（`UNIQUE(tool_name, idempotency_key)`）：同一业务幂等键同一时刻最多一个占用者/可执行 Operation，历史终态 Operation 保留相同业务幂等键作为审计记录。
- 业务幂等键保持稳定 `refund:{order_id}`（无轮次/时间戳/ID 后缀）；`tool_operations` 增加 `retry_of_operation_id` 自引用审计链。
- MANUAL_REVIEW 终态不自动释放占用；仅当存在 outcome=`RETRY_NEW_OPERATION` 的 `manual_review_resolutions` 时，才允许在同一 PostgreSQL 事务中释放旧占用并创建重试新 Operation（重新经过 Policy 与 Approval），该门控由数据库触发器证明，禁止应用层先查后插。
- 已同步修订核心设计规格 §4.3/§6.2/§7/§11、实现计划任务 9 章节。

### 任务 12：可靠 SSE（已实现，等待督导复审）

- 新增 `0017_run_ownership`：`agent_runs.owner_user_id` nullable FK `users.id`，删除使用 RESTRICT，历史/系统 Run 保持 NULL；索引支持所有权查询。过渡不变量：所有新用户 Run 必须经 `create_agent_run` 由服务端写入 Principal user_id；NULL 仅表示 legacy/system-owned，不是公共可见。
- 权限矩阵：USER/REVIEWER 仅自己的 owned Run；他人、NULL、不存在均 404；ADMIN 可读所有 Run；任何订阅先认证和授权，再创建 Redis Pub/Sub。
- PostgreSQL `run_events` 是唯一事实源；Redis 只承载 `{run_id,seq}` 不可信提示。连接先订阅，再补历史；按连续 seq 从 PG 输出，重复/乱序/永久丢通知及 Redis 断开均由周期 PG 轮询补齐。
- 通知 queue 上限 128，满时安全丢提示并依赖 PG；断开时 cancel+await reader，关闭 Pub/Sub/Redis。SSE 使用 `id/event/data`，heartbeat 为注释且不推进 seq；Last-Event-ID 非法、负数、非十进制或超过水位均 400。
- 真实 PG/Redis 定向 `12 passed in 3.03s`；完整 `302 passed in 43.91s`；Ruff 135 files、Mypy 82 files、Alembic `0017_run_ownership (head)`；0001→0017、0017↔0016、legacy NULL 与 FK RESTRICT 验证通过；PG/Redis healthy。

## 下一步：等待任务 12 督导复审

任务 12 已实现并停止等待督导复审。任务 13 未开始。

## 后续开发计划

- 任务 6：BGE Reranker、上下文预算、DeepSeek 引用回答与 Citation Validator（✅ 已完成，督导复审通过）。
- 任务 7：Run Journal、连续 seq 与 Transactional Outbox（✅ 已通过督导复审）。
- 任务 8：Tool Registry、受限 Agent Loop 与演示服务（✅ 已通过督导复审）。
- 任务 9：Policy、审批绑定与 Operation 持久化（✅ 已通过督导复审（2026-08-25），提交 `e82a57c`/`276a5cc`/`26ede61`）。
- 任务 10：Claim/Lease、fencing 与状态事务（✅ 已通过督导复审（2026-08-26），批准提交 `f36e435`、`b6729c1`、`3c7cecb`、`9863d4a`；只建立 OUTCOME_UNKNOWN/RECONCILING 安全状态边界）。
- 任务 11：副作用分类、安全重试与 Reconciliation（✅ 已通过督导复审（2026-08-26）；批准 `ab89d40`、`d27ec00`、`d128eb9`；迁移 `0015`/`0016`）。
- 任务 12：可靠 SSE（待开发）。
- 任务 13–15：Vue 管理端、五个主页面、引用详情抽屉和核心退款 E2E。
- 任务 13–15：Vue 管理端、五个主页面、引用详情抽屉和核心退款 E2E。
- 任务 16–17：v1.0/简历验收前必须完成 Evaluation 数据集、异步 Runner、故障矩阵和量化报告。
- 任务 18：可观测性、脱敏、容器部署、文档与 v1.0 发布。

完整的逐任务步骤与验收命令见[实现计划](superpowers/plans/2026-08-19-opspilot-v1-implementation.md)。

## 已知技术债

- `attempt=0` 的历史 Outbox 在 0008 迁移时会按 Document 状态回填并设置 lease，但当前 retry reconciler 只处理人工 attempt（`attempt > 0`）。它不影响人工重试或 M2；后续需选择统一管理初始入库 lifecycle，或在模型和文档中明确 status/lease 只服务人工 attempt。
- 当前本地 pytest 临时目录可能因 Windows ACL 产生访问警告；使用仓库内独立 `--basetemp` 可稳定运行，不影响测试结果。

## 开发约束

- 所有功能遵循 Red → Green → Refactor → Commit。
- PostgreSQL/pgvector、Redis/ARQ 相关能力必须使用真实服务验证。
- 不把文件 bytes、API Key、Authorization 或完整 PII 写入 Redis、Journal 或日志。
- 不在 M1/M2 中用 SQLite 或 Fake 数据库替代集成验收。
- `.env` 和 API Key 不提交到 Git。
