# OpsPilot Agent 交接说明

## 工作位置

- 仓库：`E:\JavaProjects\OpsPilot`
- 分支：`plan/opspilot-core-mvp`
- 任务 5（权限感知 Hybrid Retrieval）最新功能提交：`1bce950`
- M1（任务 1–4）与 M2 / 任务 5、任务 6 已批准（任务 6 督导验收提交 `955fe22`、`1f0e71c`、`05754bd`）
- 任务 7（Run Journal 与 Transactional Outbox）已通过督导复审
- 任务 8（Tool Registry、受限 Agent Loop 与演示服务）已通过督导复审，实现提交 `db2cae5`、复审修复提交 `8e8adfb`
- 任务 9（Policy、审批绑定与 Operation 持久化）已通过督导复审（2026-08-25），批准提交 `e82a57c`、`276a5cc`、`26ede61`
- 任务 10（Claim/Lease、fencing 与状态事务）P1 复审修复完成，等待督导复审（2026-08-25），实现提交 `f36e435`、自检修复 `b6729c1`（数据库时钟）、P1 修复 `3c7cecb`（provider 调用期间并发 heartbeat 续租 + SIDE_EFFECT 模糊失败映射 OUTCOME_UNKNOWN）
- 下一任务：任务 11，副作用分类、安全重试与 Reconciliation（为 OUTCOME_UNKNOWN 的 SIDE_EFFECT Operation 实现实际 reconciliation 与安全重试路径；可靠 SSE 属任务 12）

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
- Tool Registry、受限 Agent Loop 与演示服务（任务 8）：`ToolDefinition`（`effect: read_only|side_effect`、`idempotency_capable`、`supports_reconciliation`、`input_schema`、`invoke`）与 `ToolResult(ok/data/error)`，`ToolNotFoundError`/`ToolArgumentError`；`build_tool_registry(deps)` 只暴露 6 个工具，所有参数经 Pydantic schema 校验（Design B：适配器接收已校验参数模型）；`AgentRunner` 有界循环——每轮仅通过注册表执行工具，最多 8 轮/6 次工具调用即 `bounded` 终止，纯问答与澄清可直返；`DecisionProvider` Protocol 抽象 LLM、`DeepSeekAgentDecider` 以 JSON 模式解析 `answer`/`clarify`/`tool_call` 契约；HTTP 适配器把超时映射为可重试 `ToolResult`、非成功状态码映射为失败；`demo-services/` 提供 order/payment/email 三个 FastAPI 服务，payment 以订单号为服务端业务幂等键并支持 `timeout_before_effect`/`timeout_after_effect`/`unknown_5xx_after_effect` 故障模式（不引入 uvicorn，测试用 in-process `ASGITransport`）。
- Policy、审批绑定与 Operation 持久化（任务 9）：确定性退款策略 `decide_refund_policy(amount)`（`<=100` ALLOW、`100<amount<=1000` REQUIRE_APPROVAL、`>1000` DENY，非有限/非正金额 fail-closed 抛 `PolicyError`）；服务端派生稳定业务幂等键 `refund:{order_id}` 与稳定参数哈希；`tool_operations` 持久化 Operation（normalized_arguments、arguments_hash、idempotency_key、status、version、policy_decision、`retry_of_operation_id` 自引用审计链）；幂等占用独立表 `operation_idempotency_occupancy`（`UNIQUE(tool_name, idempotency_key)`），并发重复创建经事务级 advisory lock 串行化并返回同一当前 Operation；审批绑定不可变 `operation_id + arguments_hash + operation_version`，`decide_approval` 用 `SELECT ... FOR UPDATE` 串行化并发 Reviewer（仅第一个转换状态、后续返回 already_processed，篡改 409、过期 409）；MANUAL_REVIEW 终态不可变、仅 ADMIN 写 `manual_review_resolutions`，仅 outcome=`RETRY_NEW_OPERATION` 的 resolution 可同一事务释放旧占用并创建重试新 Operation（重新经过 Policy 与 Approval），门控由数据库触发器 `guard_occupancy_repoint`/`guard_occupancy_release` 证明；resolution 的 `replacement_operation_id` 一次性消费绑定在数据库层不可变（触发器 `guard_resolution_binding` 拒绝清空/改指）且外键为 `ON DELETE RESTRICT`（已绑定 replacement 不可删除，迁移 `0013_resolution_binding`，ORM 同步 RESTRICT）；占用切换、Operation、run_event 与 outbox 同一事务（注入失败全回滚）；迁移 `0011_operations_approvals`。

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

预期 PostgreSQL、Redis 为 healthy，`pg_isready` 接受连接，Redis 返回 `PONG`，Alembic 为 `0014_operation_lease_fencing (head)`。

## 任务 7（已通过督导复审）范围与验收

目标：Run Journal 与 Transactional Outbox。完整步骤见[实现计划](superpowers/plans/2026-08-19-opspilot-v1-implementation.md)中“任务 7”章节。

- 并发追加 20 个 Event 后断言 `(run_id, seq)` 连续唯一；注入 Outbox insert 失败后断言业务状态与 Event 均回滚。
- 事务内 Journal API：`append_event(session, run_id, event_type, payload)` 在调用者事务中锁定 Run seq，写 `run_events` 与 `event_outbox`，不自行 commit。
- Outbox Publisher：`FOR UPDATE SKIP LOCKED` 获取未投递 row，只向 Redis 发布 `{run_id, seq}`，成功后标记 delivered；重复发布安全。
- 真实 PostgreSQL/Redis 集成测试与 `0010_runs_journal_outbox` 迁移均已通过；任务 7 已通过督导复审（2026-08-24）。

## 任务 8（已通过督导复审）范围

目标：Tool Registry、Agent Loop 与演示服务。实现提交 `db2cae5`（feat: orchestrate bounded registered tools），复审修复提交 `8e8adfb`（fix: harden agent provider decision contract：Provider 边界不发送缺少 `tool_call_id` 的 `role=tool` 消息、外部 JSON 决策严格 fail-closed 校验、AgentRunner 显式抛 `DecisionError`）。定向 35 passed（注册表 8 + 受限循环 19 + 演示服务 8）、完整 158 passed。任务 8 不实现审批、Operation fencing、SSE 或前端（属任务 9–12/13–15）。

## 任务 9（已通过督导复审，2026-08-25）范围

目标：Policy、审批绑定与 Operation 持久化。批准提交：
- `e82a57c`（feat: bind durable approvals to immutable operations）——主实现：确定性策略、Operation/幂等占用持久化、不可变审批绑定、MANUAL_REVIEW resolution 门控。
- `276a5cc`（feat: bind retry authorizations one-shot to immutable replacements）——一次性重试授权（`replacement_operation_id` 一次性消费绑定，迁移 `0012_resolution_consumption`）、加固的 `guard_occupancy_repoint` 触发器、AgentRunner 副作用路由。
- `26ede61`（fix: make replacement binding immutable and undeletable）——P2：`guard_resolution_binding` 触发器使绑定数据库层不可变、外键改 `ON DELETE RESTRICT`（迁移 `0013_resolution_binding`）。

定向 70 passed（execution/test_policy.py 8 + approvals 43 + agent/test_runner.py 19）、完整 211 passed。新增迁移 `0011_operations_approvals`、`0012_resolution_consumption`、`0013_resolution_binding`（均基于当前 head 的新 revision，未复用固定迁移文件名）。

督导复审五项修复已落地：
1. AgentRunner 不直接执行 SIDE_EFFECT 工具；refund_order 决策经注入的 `side_effect_handler` 进入 durable Operation 流程（生产接线 `build_refund_operation_handler`），无 handler 时 fail-closed，任务 9 不调用 Payment Provider。
2. RETRY_NEW_OPERATION resolution 由 `replacement_operation_id` 持久绑定唯一 replacement Operation（条件 UPDATE 原子消费，无进程锁/先查后插）；DENIED replacement 也是确定结果，后续/并发请求返回同一 replacement。
3. `guard_occupancy_repoint` 数据库级验证新占用者（存在、tool_name/idempotency_key 一致、retry_of 指向旧 MANUAL_REVIEW、resolution 绑定一致），4 个原始 SQL 负向测试证明错误工具/业务键/谱系/任意 Operation 均无法 repoint。
4. resolution 消费、replacement 创建、占用切换、状态事件与 outbox 同一事务，注入失败全回滚。

P2 修复（数据库完整性）已落地：`replacement_operation_id` 绑定在数据库层不可变（`guard_resolution_binding` 触发器拒绝清空/改指已绑定值，首次 NULL→value 唯一允许），外键改为 `ON DELETE RESTRICT`（迁移 `0013_resolution_binding`，已绑定 replacement 不可删除、审计不丢），ORM 同步 `ondelete="RESTRICT"`；原始 SQL 负向测试覆盖清空/改指/DELETE 拒绝、首次绑定成功与事务失败后绑定不变。

已批准设计修订（2026-08-25）已落地：幂等键唯一性在独立占用表 `operation_idempotency_occupancy`（`UNIQUE(tool_name, idempotency_key)`）；业务幂等键稳定为 `refund:{order_id}`；`tool_operations.retry_of_operation_id` 审计链；MANUAL_REVIEW 占用不自动释放，仅存在 outcome=`RETRY_NEW_OPERATION` 的 resolution 时方可同一事务释放旧占用并创建重试新 Operation（数据库触发器证明门控）。审批、Operation fencing、SSE 与前端仍属任务 10–12/13–15，未在任务 9 实现。

## 任务 10（已实现，自检修复完成）范围

目标：Claim/Lease、fencing 与状态事务。实现提交 `f36e435`（feat: fence leased tool operation execution，7 文件 +1624 行）；复审自检修复提交 `b6729c1`（fix: harden leased operation fencing，4 文件 +428/-16 行）；P1 复审修复提交 `3c7cecb`（fix: renew operation leases during provider calls，4 文件 +605/-45 行）。

已落地：
- **Claim**：只有 READY/RETRYING 可条件 UPDATE 认领 → EXECUTING；`version` 单调 +1；一次性 `claim_token`（`secrets.token_urlsafe(32)`）；`lease_owner`/`lease_expires_at`；并发 Worker 至多一胜（`OperationNotClaimableError`）。
- **Fencing**：renewal/result 写入匹配 `id + expected version + claim_token + lease_owner + 未过期 lease`；stale version/token、wrong owner、expired lease 均修改 0 行并抛 `LeaseConflictError`；晚到旧 worker 响应不能覆盖新持有者 result。
- **过期恢复**：READ_ONLY → RETRYING（可重新认领）；SIDE_EFFECT → OUTCOME_UNKNOWN（不可认领 → Provider 不会被重新调用），原子转换且不调用 Provider；实际 reconciliation 属任务 11。
- **续租与失权**：provider 调用期间由**并发 heartbeat**（`asyncio.Task`）每 ~`lease_seconds/3` 续约（独立 session/事务、走 PostgreSQL `clock_timestamp()`、提交 `operation_lease_renewed` event/outbox、不 bump version）——超过一个 lease 窗口的长调用仍保有写入权；renewal 失败即失去 DB 写入权（`LeaseConflictError`），旧 worker 立即被撤销且永不写终态。Executor 两事务：claim 先提交（持久 lease），result write 在第二个 fenced 事务 —— 失权时保持 EXECUTING（不可重新认领）。
- **状态事务**：状态更新 + `run_event` + `event_outbox` 同一 PostgreSQL 事务（`next_seq` 原子递增）；注入 Event/Outbox 失败时 status/version/token/lease/seq 全回滚；Redis publish 在外部。
- **状态机**：`state_machine.py` 合法转换表 fail-closed；终态（MANUAL_REVIEW/SUCCEEDED/FAILED/DENIED/REJECTED）不可认领；未破坏任务 9 的 approval/occupancy/immutability（`guard_occupancy_release` 保护列表扩展到 8 个非终态）。
- **迁移**：`0014_operation_lease_fencing`（`ADD VALUE` 扩 4 状态，OID 不变；5 个新可空列；downgrade 重建枚举）。0001→0014 全链与 0014↔0013 往返在全新数据库验证通过。
- 新增文件：`src/opspilot/execution/claim.py`、`state_machine.py`、`executor.py`；测试 `tests/execution/test_claim.py`（20）+ `tests/integration/test_operation_transactions.py`（5）。定向 25 passed、完整 236 passed。
- **复审自检修复（`b6729c1`）**：唯一真实缺陷为时间语义——租约决策原先用应用本地 `datetime.now(UTC)`。改为默认解析 PostgreSQL `clock_timestamp()`（`_resolve_now`/`_clock_value`，`now`/`Clock` 仅测试注入），naive 时钟经 `_ensure_aware_utc` 规范化为显式 UTC。新增 DB 时钟/并发恢复/invoke 异常与取消 8 项测试 + recovery/result write 事务回滚 2 项测试。定向 35 passed、完整 246 passed。
- **P1 复审修复（`3c7cecb`）**：督导指出 `b6729c1` 的"无后台任务、内联续租"是错误结论——`await invoke()` 期间完全没有续租，调用时长超过 lease 时租约必过期。修复：`executor.py` 引入与 invoke 并发的周期 heartbeat（`asyncio.Task`，每 ~`lease_seconds/3` 用独立 session/事务续租，走 `clock_timestamp()`，提交 `operation_lease_renewed` event/outbox，不 bump version）。生命周期收敛：invoke 正常返回 → 停并 await heartbeat → fenced 写结果；invoke 抛异常/worker 取消 → 停 heartbeat、不写终态；heartbeat 失权 → 立即撤销 provider 并抛 `LeaseConflictError`（不响应取消的 provider 经 1s 有界宽限放弃而非无限等待）；并发/竞争一律由 fenced DB 写入决定；结束后无遗留 asyncio Task。同时把 `token/expires_at` 的 `assert` 换成显式领域异常 `LeaseStateError`；SIDE_EFFECT 模糊失败（无法证明 Provider 未执行）不再直接 `mark_failed`，改为最小安全处理 `mark_unknown → OUTCOME_UNKNOWN`（新增 `operation_outcome_unknown` 事件，实际核对归 Task 11），契约最小扩展为 `ToolResult.provider_not_called`（默认 None=未知）。Red 证据：4 个 heartbeat 特性测试（invoke 阻塞期间 lease 延长、多 interval 多次续租、DB 时钟非应用时钟、heartbeat 运行时 `recover_expired` 不能接管）在 `b6729c1` 上失败；修复后全绿。定向 44 passed、完整 255 passed。

**任务 10 只负责过期副作用 Operation 原子进入 OUTCOME_UNKNOWN/RECONCILING，不实现实际退款状态查询/核对决策（属任务 11），也不实现可靠 SSE（属任务 12）**。完整步骤见[实现计划](superpowers/plans/2026-08-19-opspilot-v1-implementation.md)中“任务 10”章节。

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
- 不顺手实现任务 11 或更后任务。
- 不声称通过未实际运行的命令。
- 不提交 `.env`、API Key、Authorization、PII、临时目录或本地文件。

## 需要维护的文档

任务完成后更新：

- `docs/development-progress.md`：里程碑、已完成内容、最新验证数字、下一任务。
- 实现计划中的任务状态行。
- 如设计发生经批准的变化，更新架构规格；不要静默偏离。
