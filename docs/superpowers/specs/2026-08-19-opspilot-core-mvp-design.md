# OpsPilot 核心 MVP 设计规格

## 1. 目标与交付边界

OpsPilot 是一个企业智能 Agent 平台。核心 MVP 先交付一条可演示、可审计、可恢复的纵向链路：用户登录，上传企业文档，系统异步解析与索引，Agent 完成权限感知的混合检索和带引用回答，并通过受控工具网关执行订单退款。高风险退款必须经过确定性策略、持久化审批、业务幂等、数据库租约和结果核对。

核心 MVP 包含：

- 登录、固定角色与知识权限范围；
- PDF、DOCX、Markdown 和文本文件上传、版本管理与异步入库；
- BGE-M3 Embedding、Dense 检索、PostgreSQL FTS、RRF、BGE Reranker 和上下文预算；
- DeepSeek V4 Flash 结构化回答、稳定引用 ID 与引用校验；
- Agent Orchestrator、Tool Gateway、订单/支付/邮件演示服务；
- 退款策略、审批、Operation、幂等、Claim/Lease fencing、重试与 Reconciliation；
- PostgreSQL Append-only Run Journal、SSE 续传；
- 五个主页面：Agent 工作台、知识库、检索调试器、审批中心、Run 时间线；
- 引用详情抽屉，用于定位生成时的文档版本、章节和 Chunk 原文。

Evaluation 不属于核心 MVP 的首轮交付，但不是可无限延期的可选项。它是 v1.0 发布与简历量化验收前必须完成的发布切片，包括版本化数据集、异步实验 Runner、检索/回答/工具/安全/可靠性指标、故障注入、评测看板和报告导出。核心 MVP 必须预留评测数据模型、公开接口以及模型、Prompt、工具 Schema 和实验配置快照字段。

MVP 不包含多 Agent、Graph RAG、真实支付、真实客户数据、动态权限编辑器、Kubernetes、OCR 和通用低代码工作流。

## 2. 技术选择与运行约束

- API：Python 3.12、FastAPI、Pydantic 2、SQLAlchemy 2、Alembic。
- 数据：PostgreSQL 16、pgvector、PostgreSQL FTS。除非后续实际引入 OpenSearch、ParadeDB 或 BM25 扩展，不得把原生 `ts_rank` 称为 BM25。
- 异步：Redis 7、ARQ、独立 Worker。
- 模型：DeepSeek V4 Flash，OpenAI-compatible Base URL 为 `https://api.deepseek.com`。默认关闭思考模式，复杂 Agent 决策可按配置开启。
- Embedding：本地 BGE-M3；DeepSeek 不承担 Embedding。
- Reranker：BGE Reranker；超时后降级到 RRF 顺序并记录降级事件。
- 前端：Vue 3、TypeScript、Vite、Element Plus、Pinia、ECharts。
- 部署：Docker Compose 是标准集成与发布环境。PostgreSQL FTS、pgvector、Redis/ARQ、并发 Worker 和恢复测试必须使用真实服务验证，不能用 SQLite 替代相关集成测试。
- 密钥只通过本地 `.env` 或部署密钥注入，不能提交到仓库或写入对话、日志、Run Event。

## 3. 系统架构与组件边界

Vue Web 通过 REST 和 SSE 访问 FastAPI 模块化单体。FastAPI 包含 Auth、Knowledge、Retrieval、Generation、Agent Orchestrator、Tool Gateway、Approvals、Runs 和 Evaluation API。ARQ Worker 独立运行，负责文档解析/索引、审批后工具执行、重试和结果核对。

### 3.1 组件职责

- Auth 是所有受保护 API 的横切能力。Token 恢复 `user_id`、角色、部门集合和最大访问级别。
- Knowledge 管理知识库、文档版本、原始文件和入库状态，不执行检索。
- Retrieval 接收已经确定的 Query 和 `KnowledgeScope`，在数据库检索阶段过滤权限，输出 Dense、FTS、RRF、Reranker 各阶段候选。它不调用 LLM。需要 LLM 的 Query Rewrite 由 Agent Orchestrator 调用 Generation Provider 完成，再把改写结果传给 Retrieval。
- Generation 只消费 Context Builder 产出的上下文，不直接查询数据库。它调用 Chat Provider 并校验结构化引用。
- Agent Orchestrator 决定检索、回答、追问或提出 Tool Call。它不能直接调用外部业务服务。
- Tool Gateway 是 Agent 工具调用的唯一出口，由 Registry、Schema Validator、Policy Engine、Approval Service 和 Durable Executor 组成。
- Run Journal 是 FastAPI 与 Worker 共享的应用组件。PostgreSQL `run_events` 是事实源，Redis 只承载实时通知。
- Evaluation 只通过公开服务接口评测，不越过产品边界访问内部实现。

### 3.2 原始文件存储

核心 MVP 使用共享 Volume 保存原始文件，并通过 `FileStorage` 接口隔离实现，以便后续替换为 MinIO 或 S3。API 完成安全文件名生成、流式落盘和 Document 持久化后，只向 ARQ 投递 `document_id`。Worker 从 PostgreSQL 读取 `storage_path` 后访问文件。禁止通过 Redis 传递整份文件或文件正文。

### 3.3 外部边界

- AI Model Providers：DeepSeek/OpenAI-compatible Chat、BGE-M3 Embedding、BGE Reranker。
- External Tools：Order Service、Payment Service、Email Service。
- 所有外部业务调用必须经过 Tool Adapter 或选定传输方式的 MCP Adapter，并且不能绕过 Tool Gateway。

## 4. 数据模型与约束

### 4.1 身份与知识

- `users`：用户名、密码哈希、角色、允许部门、最大访问级别。
- `knowledge_bases`：名称、部门、访问级别、Embedding 模型和状态。知识权限是 **KB 级**：部门与访问级别定义在 KnowledgeBase 上，文档继承所属知识库的权限，不再在 Document 上重复存储。
- `documents`：知识库、标题、版本、生效时间（`effective_at`）、SHA-256、`storage_path` 和处理状态。部门与访问级别由所属 KnowledgeBase 决定。
- `chunks`：文档版本、章节路径、页码、内容、Token 数、Embedding、`tsvector` 和元数据。

### 4.2 对话与审计

- `conversations`、`messages`：对话与消息；消息保存引用快照。
- `agent_runs`：状态、Prompt 版本、模型、温度、工具 Schema 哈希、Token 与延迟。
- `run_events`：`run_id`、单个 Run 内单调递增的 `seq`、事件类型、脱敏载荷和时间。约束 `UNIQUE(run_id, seq)`。

引用快照至少保存 `document_id + document_version + chunk_id + section_path + page`，保证文档更新后仍能定位生成时证据。

### 4.3 可靠执行

- `tool_definitions`：名称、输入 Schema、风险级别、审批规则、`effect`（`read_only` 或 `side_effect`）、幂等能力和是否支持核对。
- `tool_operations`：Run、工具名、规范化参数、`arguments_hash`、服务端幂等键、状态、`version`、`claim_token`、`lease_owner`、`lease_expires_at`、Provider reference ID 和结果。
- `operation_attempts`：Attempt 编号、脱敏且限长的请求、响应与错误。
- `approval_requests`：Operation、参数哈希、Operation 版本、状态、审核人、意见和过期时间。
- `event_outbox`：`run_id`、`seq`、投递状态、尝试次数和投递时间；只通知事件身份，不复制 Event payload。
- `manual_review_resolutions`：原 Operation、管理员、处置结果、备注和关闭时间；用于审计关闭 MANUAL_REVIEW，不改变原 Operation 的终态事实。

约束 `UNIQUE(tool_operations.tool_name, tool_operations.idempotency_key)`。幂等键必须由服务端根据规范化业务参数派生，例如退款使用 `refund:{order_id}`；不得信任 Agent 提供的幂等键。

### 4.4 评测预留

- `evaluation_runs` 保存数据集版本、模型、Embedding、Reranker、top-k、Prompt、随机参数和指标。
- `evaluation_cases` 保存用例 ID、实际输出、确定性分数、延迟和错误。

## 5. 知识入库、检索与回答

### 5.1 入库链路

上传 → 共享文件存储 → Document 持久化 → 投递 `document_id` → Worker 解析 → 结构感知切分 → BGE-M3 批量 Embedding → Chunk、Vector 和 `tsvector` 入库 → Document READY。

上传时 `effective_at` 默认取当前时间（由模型默认值写入）。生效时间供 Context Builder 生成引用时携带，任务 6 之前不参与检索过滤。

切分先建立章节树，再按段落合并到配置 Token 范围；标题与正文保持同一 Chunk，表格不按行打散，相邻 Chunk 仅保留受控重叠。解析、Embedding 和索引任务通过 Document ID 与内容哈希保持幂等。

### 5.2 查询链路

用户身份 → `KnowledgeScope` → Agent Orchestrator 按需调用 Generation Provider 完成 Query Rewrite → 数据库权限过滤 → Dense + PostgreSQL FTS → RRF → Reranker → Context Builder → DeepSeek 结构化回答 → Citation Validator → 返回答案。

权限必须进入 Dense 和 FTS 的数据库查询条件。不能先检索机密 Chunk，再依靠 Prompt 隐藏。

Context Builder 为每个片段提供稳定 ID `[DOC:<document_id>#<chunk_id>]`，并携带标题、文档版本、章节、生效时间和页码。模型只能引用本次上下文 ID。事实型回答若没有合法引用，必须转为证据不足，不能只删除无效引用后保留原确定性回答。

## 6. Agent、审批与可靠工具执行

Agent Loop 最多 8 轮、最多 6 次工具调用。模型只能选择 Registry 暴露的工具，参数经过结构化解析。不得持久化或展示隐藏思维过程。

### 6.1 Operation 主流程

Agent 提议 Tool Call → Registry 查找 → Schema 校验 → Policy 判断 → 创建 Tool Operation → 服务端生成幂等键 → 持久化 CREATED/WAITING_APPROVAL 或 READY → 审批 → 写入队列 → Worker 执行 PostgreSQL Claim/Lease → 幂等检查 → 调用外部工具 → 保存结果与 Run Event。

Operation 与幂等键必须在审批前持久化。审批固定绑定 `operation_id + arguments_hash + operation_version`。订单号、金额或其他参数变化后，原审批失效，必须生成新 Operation 或新审批。

### 6.2 状态与策略

退款 `<=100` 自动允许，`100<amount<=1000` 需要 REVIEWER 或 ADMIN 审批，`>1000` 拒绝。该规则由确定性 Policy Engine 执行，Prompt 不能覆盖。

主要状态为 CREATED、VALIDATING、POLICY_CHECKING、WAITING_APPROVAL、READY、EXECUTING、RETRYING、OUTCOME_UNKNOWN、RECONCILING、SUCCEEDED、FAILED、DENIED、REJECTED 和 MANUAL_REVIEW。

失败处理先读取 ToolDefinition 的 `effect`、幂等能力和 `supports_reconciliation`。只读工具可以对连接错误、429 和可重试 5xx 进行退避重试。副作用工具只有在 Provider 明确返回“未执行”或传输层能确定请求未送达时才能直接进入 RETRYING；读取响应超时、发送后连接中断和语义不明确的 5xx 都表示请求可能到达 Provider，必须进入 OUTCOME_UNKNOWN，随后 RECONCILING。支付状态显示已退款则 SUCCEEDED，核对确认未退款才允许 RETRYING，无法确认则 MANUAL_REVIEW。进入结果不确定状态后禁止盲目再次退款。

MANUAL_REVIEW 是原 Operation 的终态。管理员可通过专用审计接口写入 `manual_review_resolutions`，将人工调查标记为已解决并记录处置说明，但原 Operation 仍保持 MANUAL_REVIEW。若管理员决定再次尝试，必须创建新的 Operation，重新派生幂等与参数哈希，并重新经过 Policy 和 Approval；禁止复用原 Operation 自动执行。

### 6.3 Claim/Lease 与 fencing

ARQ 只负责投递，不提供业务级租约。Worker 只能直接认领 READY 或 RETRYING 的 Operation，通过 PostgreSQL 条件更新增加 `version`、生成不可复用的 `claim_token`，同时写入 `lease_owner` 和 `lease_expires_at`。提交结果时必须匹配当前 Operation 版本和 claim token。

旧 Worker 即使外部调用稍后返回，也不能覆盖新持有者的数据库结果。长任务必须续租；续租失败或租约过期后，旧持有者立即失去数据库写权。fencing 本身不能阻止外部副作用重复，因此 EXECUTING 的租约过期后，新 Worker 不得直接再次调用副作用工具；它必须原子地把 Operation 转为 OUTCOME_UNKNOWN/RECONCILING，并使用 Provider operation/reference ID 或服务端业务幂等键查询真实状态。只有核对明确确认副作用不存在，状态才可转为 RETRYING 并重新认领执行。

### 6.4 事务与审计

MVP 固定采用 Transactional Outbox。每次 Operation 状态更新、对应 `run_event` 和 `event_outbox` row 必须在同一个 PostgreSQL 事务提交，避免业务状态、审计时间线和通知意图不一致。独立 Publisher 重试未投递 Outbox row，向 Redis 发布仅包含 `run_id + seq` 的通知，成功后幂等标记 delivered。Redis 发布失败不回滚 PostgreSQL 事实状态；Publisher 可安全重复发布，消费者按 `(run_id, seq)` 去重。

Attempt、Journal、日志和 Trace 写入前必须脱敏并限制长度。API Key、Authorization Header、完整邮箱、手机号和其他完整 PII 不得进入这些载荷。

## 7. API 与错误契约

公开接口包含认证、知识库、文档、检索调试、对话消息、Run、Run Events、SSE、审批和 Evaluation 预留接口。异步创建返回 `202 Accepted`，携带可查询的 `document_id`、`run_id` 或 `operation_id`。

成功响应采用 `{data, meta, request_id}`；错误响应采用 `{error: {code, message, details?}, request_id}`。机器错误码稳定，用户消息安全，管理员诊断仍需脱敏。

关键错误：

- `VALIDATION_ERROR`：422，不创建可执行 Operation。
- `IDEMPOTENCY_CONFLICT`：409；同语义请求返回已有 Operation，异常冲突记录告警。
- `APPROVAL_EXPIRED`：409，不执行并要求重新发起审批。
- `APPROVAL_VERSION_CONFLICT`：409，参数或 Operation 版本已变化。
- `LEASE_CONFLICT`：Worker 放弃本次写入并重读，不暴露成用户 500。
- `LEASE_EXPIRED_DURING_SIDE_EFFECT`：EXECUTING 租约过期且请求可能已送达，原子转入 OUTCOME_UNKNOWN/RECONCILING，禁止直接再次调用。
- `OUTCOME_UNKNOWN`：状态查询明确显示核对待处理或进行中。
- `MANUAL_REVIEW_REQUIRED`：原 Operation 的只读终态，禁止自动重复副作用；管理员只能记录审计处置，人工重试必须新建 Operation 并重新审批。

## 8. Run Journal 与 SSE

SSE 的 `Last-Event-ID` 对应单个 Run 内的 `seq`。SSE 服务先订阅该 Run 的 Redis 通知通道，再从 PostgreSQL 查询并发送 `seq > Last-Event-ID` 的历史事实事件。此后每个 Redis 通知只提供 `run_id + seq`，SSE 服务按 seq 从 PostgreSQL 读取事实 Event 并去重。服务同时周期性查询 PostgreSQL 的最大 seq；发现水位高于已发送连续 seq 时主动补拉缺失区间，因此即使 Redis 通知永久丢失也不会丢 Event。Redis 订阅先于历史补拉，补拉期间到达的通知先缓冲，历史发送完成后按 seq 归并。

前端为每个 Run 维护 `expected_seq` 和乱序 `buffer`：

1. 收到 `seq < expected_seq` 时视为重复，忽略渲染。
2. 收到 `seq == expected_seq` 时应用一次，递增 `expected_seq`，然后连续消费 buffer。
3. 收到 `seq > expected_seq` 时暂存到 buffer，通过 HTTP 从 PostgreSQL 补拉缺失区间。
4. 断线后使用指数退避和抖动重连，并携带最后连续应用的 seq。
5. 终态快照仅用于状态校准，不能掩盖事件缺失；连接结束前必须消除所有缺口。

前端按 `(run_id, seq)` 幂等渲染。断线、重复和乱序验收后，客户端获得的 seq 集合必须与 PostgreSQL `run_events` 完全一致，每个事件只渲染一次。

## 9. 前端信息架构与交互

### 9.1 五个主页面

- Agent 工作台：映射 conversations、messages、agent_runs 和 run_events，展示回答、工具卡片和实时状态。
- 知识库：映射 knowledge_bases 和 documents，展示上传、版本、权限、处理状态和失败原因；文档详情可作为知识库内路由或子页面。
- 检索调试器：并排展示 Dense、PostgreSQL FTS、RRF 和 Reranker 的 rank、score、文档、章节与正文。
- 审批中心：映射 approval_requests 和 tool_operations，展示不可变参数摘要、哈希、版本和策略原因。
- Run 时间线：按 seq 展示检索、模型、策略、审批、操作状态和核对事件，不展示隐藏思维过程。

引用详情抽屉是工作台与检索结果中的组件，不是独立主路由。它使用引用快照定位生成时的文档版本和 Chunk，展示章节路径、页码与原文高亮。

### 9.2 审批交互

USER 仅可查看；REVIEWER 和 ADMIN 才显示批准/拒绝按钮。点击后立即禁用并显示提交状态，但后端行锁、Operation 版本和审批版本检查才是最终并发保护。409 后刷新事实状态，明确显示已被处理、已过期或参数已变化，不能静默显示成功。批准只表示允许执行，不表示退款完成。

### 9.3 可靠执行状态

- EXECUTING：正在执行退款。
- OUTCOME_UNKNOWN：结果暂未确认，系统不会自动重复退款。
- RECONCILING：正在向支付服务核对真实结果。
- MANUAL_REVIEW：系统无法确认，需要管理员人工处理。

SSE 断线只显示正在重连，不把 Run 标记为失败。

## 10. 错误处理原则

- Provider 超时必须显式降级或进入可靠执行状态，不能吞掉错误。
- Reranker 超时降级到 RRF；记录 `reranker_status=degraded`。
- Embedding 或解析失败写入 Document 失败原因，重复任务不得产生重复 Chunk。
- 无有效引用的事实型回答转为证据不足。
- 权限拒绝、参数错误和业务拒绝不可重试。只读工具可对连接错误、429 和明确可重试的 5xx 退避重试。副作用工具按“请求是否可能到达 Provider”分类：只有明确未送达或 Provider 明确未执行才能 RETRYING；任何可能已送达的失败必须 OUTCOME_UNKNOWN → RECONCILING，核对确认未产生副作用后才可 RETRYING。
- 用户只看到安全摘要；管理员可查看脱敏、限长的诊断。

## 11. 测试策略与 MVP 验收

### 11.1 测试层级

- 单元测试：结构切分、RRF、引用校验、策略、幂等键派生、状态转换、脱敏、SSE buffer reducer。
- PostgreSQL/Redis 集成：pgvector、PostgreSQL FTS、权限过滤、唯一约束、Operation/Event/Outbox 原子提交、Outbox 重复发布、Redis 通知永久丢失后的 PG 水位补发、Claim/Lease/fencing。
- 服务集成与故障注入：审批参数篡改、审批过期、只读工具退避重试、副作用请求明确未送达、未知 5xx、timeout-before-effect、timeout-after-effect、Worker 重启、并发 Worker 与租约过期。
- Playwright E2E：上传、引用问答、审批退款、核对、时间线、SSE 断线/重复/乱序。

### 11.2 核心验收

- 文档上传后可完成异步入库并被检索。
- 普通用户无法在 Dense 或 FTS 查询结果中获得越权 Chunk。
- 回答引用可定位生成时的原文版本和 Chunk。
- 超过阈值的退款在审批前不能执行，审批参数变化后原批准不能复用。
- 10 个并发同语义退款只产生一个外部退款副作用，调用方获得同一 Operation。
- timeout-after-effect 必须通过 Provider reference ID 或业务幂等键核对成功，不得再次退款。
- Worker 或 API 重启后可从 PostgreSQL 恢复；旧 Worker 的迟到数据库结果被 fencing 拒绝。
- 旧 Worker 的退款调用已生效但 EXECUTING 租约过期时，新 Worker 接管必须先进入 RECONCILING 并确认已退款，外部退款记录仍只有一条。
- Operation 状态与审计事件不存在单边提交。
- SSE 在断线、重复、乱序和补发窗口下，客户端最终 seq 集合与 PostgreSQL 完全一致，每个事件只渲染一次。
- `docker compose up --build` 在具备 Docker 环境后启动 Web、API、Worker、PostgreSQL、Redis 和演示服务。

## 12. 实施切片

1. S1：仓库骨架、配置、认证、数据库、共享文件存储、知识库上传。
2. S2：解析、切分、BGE-M3、权限感知 Dense/FTS、RRF、Reranker、引用问答。
3. S3：Run Journal、Agent、Tool Gateway、演示服务、策略、审批、Operation、Lease fencing、重试与核对。
4. S4：五个主页面、引用抽屉、SSE 客户端和核心 E2E。
5. v1.0 必做切片：Evaluation Runner、版本化数据集、量化指标、故障矩阵、看板、报告、OpenTelemetry、部署与发布文档。

每个切片使用测试先行，完成后运行与风险相称的单元、集成和 E2E 验证。Docker 未安装期间只能宣称纯单元层通过，不能宣称 PostgreSQL、Redis、Worker 恢复或完整产品验收通过。
