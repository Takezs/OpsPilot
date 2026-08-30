# OpsPilot Agent 交接说明

## 工作位置

- 仓库：`E:\JavaProjects\OpsPilot`
- 分支：`plan/opspilot-core-mvp`
- 任务 5（权限感知 Hybrid Retrieval）最新功能提交：`1bce950`
- M1（任务 1–4）与 M2 / 任务 5、任务 6 已批准（任务 6 督导验收提交 `955fe22`、`1f0e71c`、`05754bd`）
- 任务 7（Run Journal 与 Transactional Outbox）已通过督导复审
- 任务 8（Tool Registry、受限 Agent Loop 与演示服务）已通过督导复审，实现提交 `db2cae5`、复审修复提交 `8e8adfb`
- 任务 9（Policy、审批绑定与 Operation 持久化）已通过督导复审（2026-08-25），批准提交 `e82a57c`、`276a5cc`、`26ede61`
- 任务 10 已通过督导复审（2026-08-26），批准提交：`f36e435`、`b6729c1`、`3c7cecb`、`9863d4a`
- 任务 12 可靠 SSE 已通过督导复审（2026-08-26）
- 任务 13 Vue 应用壳、登录与 API 客户端已通过督导复审（2026-08-27），批准 `10b0d65`、`cb41271`
- 任务 14 知识库、检索调试器与引用抽屉已通过督导复审（2026-08-27），批准 `2b6dc34`、`6eab4a4`、`d6f98ed`；下一项任务 15

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

## 任务 10（已通过督导复审，2026-08-26）范围

目标：Claim/Lease、fencing 与状态事务。实现提交 `f36e435`；复审修复 `b6729c1`、`3c7cecb`；独立复审修复 `9863d4a`（fix: supervise detached operation providers）。

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
- **独立复审修复（`9863d4a`）**：`3c7cecb` 的不响应取消 provider 会在 `shield + 1s` 后继续成为未受控 Task，原“无遗留 Task”声明不真实。修复后该 Task 被强引用注册、最终异常被读取、完成后自动移除，并提供显式 drain；heartbeat 始终 cancel+await。同轮 invoke/heartbeat 完成时 heartbeat 失权优先、invoke 异常也被读取，旧 worker 永不写结果。`provider_not_called=True` 才允许 SIDE_EFFECT 确定失败，False/None 均进入 OUTCOME_UNKNOWN；状态/event/outbox/next_seq 注入失败全回滚。督导独立定向 48 passed；本轮完整 259 passed；Ruff/Mypy/Alembic/PG/Redis 通过。

**任务 10 只负责过期副作用 Operation 原子进入 OUTCOME_UNKNOWN/RECONCILING，不实现实际退款状态查询/核对决策（属任务 11），也不实现可靠 SSE（属任务 12）**。完整步骤见[实现计划](superpowers/plans/2026-08-19-opspilot-v1-implementation.md)中“任务 10”章节。

## 任务 11（已通过督导复审，2026-08-26）范围

- `errors.py` 使用结构化失败类型分类；SIDE_EFFECT 只有 `provider_not_called=True` 可安全重试，False/None、读取超时、发送后断连和未知 5xx 均 OUTCOME_UNKNOWN。
- `retry.py` 提供有界指数退避；`retry_not_before` 在 PostgreSQL claim 条件中强制等待，执行 Attempt 达到 4 次后不再循环。
- `reconciliation.py` 专用 claim OUTCOME_UNKNOWN → RECONCILING，使用独立 token/owner/version/lease；通过 Provider reference 或 `refund:{order_id}` 查询，收敛到 SUCCEEDED/RETRYING/MANUAL_REVIEW；过期核对 lease 可安全恢复到 OUTCOME_UNKNOWN。
- `operation_attempts`（迁移 `0015_operation_attempts`）记录脱敏、限长的执行/核对请求响应错误；状态、Attempt、Event、Outbox、next_seq 同事务。
- 真实 Payment Service 的 timeout-after-effect/unknown-5xx-after-effect 验证退款记录严格为 1；detached provider drain 已接入 `WorkerSettings.on_shutdown`。
- 验证：Task 11 定向 24 passed；扩展可靠执行定向 81 passed；完整 283 passed；Ruff/Mypy 通过；Alembic `0015 (head)`，全链和往返通过；PostgreSQL/Redis healthy。
- Attempt 生命周期复审修复：过期 READ_ONLY EXECUTION Attempt 关闭为 `ABANDONED`，SIDE_EFFECT 关闭为 `OUTCOME_UNKNOWN`，与 Operation/Event/Outbox/next_seq 同事务；`0016_running_execution_attempt` 部分唯一索引禁止同一 Operation 多个 RUNNING EXECUTION Attempt。本轮定向 29 passed、Task 10+11 核心组合 77 passed、完整 288 passed；0016 往返和 0001→0016 全链通过。
- 0016 升级兼容复审修复：索引创建前回填 0015 遗留的重复 RUNNING EXECUTION Attempt；EXECUTING 仅保留 `(attempt_number DESC, id DESC)` 最新项，OUTCOME_UNKNOWN/RECONCILING 关闭为 `OUTCOME_UNKNOWN`，其他状态关闭为 `ABANDONED`。真实 PG Red 为 duplicate-key UniqueViolation；Green 覆盖脏数据、索引、事务失败回滚与往返。最新定向 31 passed、核心组合 79 passed、完整 290 passed；Ruff/Mypy/全链/PG/Redis 通过。
- 督导批准提交：`ab89d40`（主实现）、`d27ec00`（Attempt 生命周期与唯一约束）、`d128eb9`（0016 脏历史回填与原子升级）；迁移 `0015_operation_attempts`、`0016_running_execution_attempt`。督导独立定向 31 passed；文档闭环完整套件重新运行 290 passed in 41.82s。

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
## 任务 12（已通过督导复审，2026-08-26）范围

- `0017_run_ownership` 新增 nullable `agent_runs.owner_user_id`（FK users.id，RESTRICT + index）。历史 NULL 不回填、不公共可见；新用户 Run 只经 `create_agent_run` 服务绑定 Principal。
- USER/REVIEWER owner-only，other/NULL/missing 均 404；ADMIN all。认证和授权在 Redis subscribe/历史查询前完成。
- `RedisSSEStream` 先订阅后补 PG 历史；Redis 仅 `{run_id,seq}` hint，所有 payload 从 PG 读取；连续水位、去重、乱序/缺通知/断线由 PG polling 收敛。
- buffer 128 上限，慢客户端丢 hint 不丢事实；清理 reader/pubsub/client；SSE id/event/data 与无 seq heartbeat；Last-Event-ID fail-closed。
- 定向 12 passed；完整 302 passed；Ruff/Mypy/0017 全链往返/真实 PG Redis 均通过。任务 13 未开始。
- 复审修复：新增幂等 `close()`，subscribe/open 失败时 pubsub 与 Redis client 各关闭一次、无 reader task，router 映射为 503；授权失败仍不创建 Redis client。event_type 统一严格 ASCII token 1–64，journal 在 next_seq 前拒绝、encode 防御历史脏值，payload 换行仍 JSON 转义。Redis hint 对空对象/数组/字符串/数字/null/错误 run/错误 seq/bool/负数/超 4096 bytes 全部丢弃且不 reconnect，后续合法 hint/PG polling 正常。本轮严格 Red `9 failed` → Green `9 passed`；上一轮未保存行为 pytest Red 的历史流程偏差不得删除。最终定向 32 passed、完整 312 passed；等待再次复审。
- 批准提交：`9a68056`（主实现、0017 ownership、gap-free SSE）、`d8b391d`（资源/协议/恶意 hint 加固）。督导独立定向 32 passed、0017 head；文档闭环完整 312 passed in 44.71s。历史 TDD 流程偏差保留：主实现未保存行为 Red，复审修复保存 9 failed→9 passed。

## 任务 13（已通过督导复审，2026-08-27）范围

- 新建 Vue 3 + TypeScript + Vite 前端，依赖 Element Plus、Pinia、Vue Router、Axios、Vitest、Playwright；五个页面均为路由壳，任务 14/15 功能未实现。
- 认证复用后端 `/api/v1/auth/login`；Axios interceptor 独占 Authorization 注入。刷新从经结构、角色、过期校验的 JWT 恢复最小 Principal；401 并发收敛为一次清理和一次登录重定向。
- 守卫覆盖未认证访问、USER 直接访问 approvals，导航可见性不作为唯一授权；returnUrl 限定站内受保护路径；错误提示不暴露原始响应、堆栈或 token。
- Red：无生产源码时 Vitest 因缺少 `src/router/security` 失败。Green：Vitest 2 passed，Playwright 登录 E2E 3 passed，typecheck/build 通过；后端 312 passed in 42.64s，Ruff/Mypy/0017、PG/Redis 通过。
- 复审修复：后端新增 `GET /api/v1/auth/me`，JWT 仅验证身份凭据，返回数据库当前 active Principal；角色变更即时生效，伪造/过期/inactive 均 401。前端 localStorage token 不再建立角色，只在共享 async initialization 中调用 `/me`，守卫 await 后才渲染；登录也统一读取 `/me`。并发 navigation 只发一次 `/me`，并发 401 只 replace 一次且重新登录复位。Red 为后端 `1 failed, 12 passed`（404）和前端 `1 failed, 1 passed`（initialize 缺失）；Green auth 13、Vitest 2、Playwright 4，完整后端 313 passed in 43.33s。
- 督导批准 `10b0d65`、`cb41271`；独立 frontend unit 2、typecheck/build、backend auth 13、0017 head 均通过。批准闭环完整后端 313 passed in 43.54s，login E2E 4 passed。下一项任务 14。

## 任务 14（已通过督导复审，2026-08-27）范围

- 后端提交 `2b6dc34` 新增权限感知 KB/document list/detail、精确版本 citation detail 与四阶段 retrieval debug；scope/READY在候选和metadata SQL双重约束，unauthorized/missing统一404，failure只返回固定安全消息。
- Retrieval API复用 Dense、PostgreSQL FTS、RRF和现有reranker timeout fallback；不调用LLM或rewrite，保留后端顺序/score，返回限长脱敏excerpt与degraded状态。
- 前端 KnowledgeBaseView/DocumentStatusTable 仅轮询上传document，指数退避上限4s，终态/401/卸载停止；RetrievalDebugger/StageColumn保留四阶段语义；CitationDrawer按精确版本请求、纯文本高亮、无独立路由。
- Red：后端404为 `2 failed` + `1 failed`；前端缺module `1 failed, 1 passed`；首轮E2E `2 failed`后修正测试契约。Green：后端相关19、完整317 passed in 44.35s；frontend unit4、Task14 E2E2、login4，typecheck/build与全门禁通过。
- 督导批准 `2b6dc34`、`6eab4a4`、`d6f98ed`；独立 frontend unit5、typecheck、Task14 E2E2及后端scope/retrieval定向8均通过。
- 复审修复：pollDocument把AbortSignal传入飞行中Axios GET，并在响应后/写入前二次fence；CitationDrawer用AbortController+generation阻止切换/关闭/卸载后的旧响应覆盖，取消不显示错误。Red为轮询`1 failed, 1 passed`与citation模块缺失suite failed；Green frontend unit5，Task14 E2E2、login4、后端相关19、完整317 passed in 44.68s。已批准。
- 不声称通过未实际运行的命令。
- 不提交 `.env`、API Key、Authorization、PII、临时目录或本地文件。

## 任务 15（已通过督导复审，2026-08-30）

- 后端提交 `778c192`：0018 `run_messages`、Run/Operation两个独立job outbox、公开Run/History/Approval/Timeline读取、ARQ编排及真实HTTP demo退款E2E。
- 前端提交 `e69181c`：工作台、审批中心、Run时间线和集中式授权SSE客户端；USER路由权限沿用Task13，批准文案不等同退款成功。
- Red→Green：后端 `5 failed + 5 failed → 11 passed`；前端 `1 failed suite → 5 passed`。完整后端328 passed；前端unit10、E2E7、typecheck/build通过。
- 0018已验证upgrade/downgrade/upgrade；PG/Redis healthy；Payment独立Uvicorn进程有界清理且ORD-002退款严格1。
- 2026-08-29 复审修复完成，等待再次复审：0019为Run message job增加数据库claim/lease与可恢复执行；生产cron恢复过期Operation/Reconciliation并原子产生下一version intent；outbox使用PG时钟有界退避；Run读取再次脱敏；SSE改为PG history事实单通道串行分页drain。
- Red证据：recovery intent实际v8却写v9；重复job会并发进入processor；outbox故障同row热循环100次；SSE先3后history 1/2会丢3；Run detail泄露敏感诊断。修复后相关可靠执行组合89 passed，完整后端337 passed，前端unit13 passed、Playwright mock回归5 passed。
- 迁移：0019 head↔0018↔head及独立scratch 0001→0019均通过；PostgreSQL/Redis健康。
- 明确阻塞：本机无DeepSeek凭据（仅BGE endpoint已配置），worker真实引用链仍不能验收；未使用Fake Provider，也未把现有直接handler后端测试或Playwright mock称为核心完整E2E。等待督导，不得进入Task16。
- 第二轮复审修复：Run job heartbeat使用独立PG事务、约lease/3续租与token/owner fencing；FIRST_COMPLETED失权优先，取消不响应processor由受控detached集合和shutdown hook回收。Run状态统一从durable Operation/message事实在原事务重算，MANUAL_REVIEW/不确定态保持RUNNING，最终退款SUCCEEDED后Run收敛COMPLETED。SSE history失败不再产生未处理Promise，改为中止stream、进入RECONNECTING；401停止重连，卸载后无陈旧写。
- 本轮Red：heartbeat缺失3 failed、scanner冲突候选单轮20次、真实退款后Run仍RUNNING、SSE生命周期3 failed。Green：后端相关34 passed、完整350 passed；frontend unit18、Playwright mock回归5、typecheck/build；Ruff/Mypy通过。真实Provider阻塞不变。
- 第三轮复审修复：delivery watchdog以PG锁和ack grace恢复delivered但未claim的Run/Operation intent；reply存在则Run job收敛COMPLETED，Operation状态/version已前进则intent固定标记stale。0020回填0019脏历史并新增三态字段组合CHECK，真实PG负向SQL、事务失败回滚、往返和全链通过。
- Agent生产composition现显式接收`RunJobFence`；退款Operation创建前在同一事务锁定并验证claim，消除跨session TOCTOU。失权detached旧worker测试为0 Operation/Event/job，新owner恢复后幂等收敛为1。第三轮定向24、完整357、frontend unit18/E2E mock5；真实Provider阻塞不变。
- 方案A grounded composition已接线并完成引用复审加固：空BuiltContext不是成功检索且不签发ID；Runner自签`search_call_id`通过可信control消息与untrusted工具摘要分离。单一成功上下文后的plain answer模型正文被丢弃并进入同一严格Generation/ValidatedAnswer路径；缺/错ID、多上下文、无/错citation均fail-closed，不自动伪造引用。Task14 enrichment公共SQL服务继续按Run owner数据库Principal执行scope+READY过滤。
- assistant单条回复由确定性renderer组合grounded正文和持久化Operation事实，非SUCCEEDED不宣称退款成功；四类BGE/DeepSeek provider在worker生命周期统一关闭。2026-08-30最终live验收从公开Run/message经真实outbox、Redis/ARQ、DeepSeek+BGE/PG检索与Generation、不可变引用、审批HTTP、Payment timeout-after-effect和reconciliation收敛SUCCEEDED；后续结构化引用加固与连续三轮证据见下方最终Green及批准记录。
- 2026-08-30 DeepSeek 连通性修复：Clash Verge mixed proxy 定位为 `127.0.0.1:7897`；新增 `DEEPSEEK_PROXY_URL`，由 Worker 显式传入 DeepSeek decision/generation Provider 的 HTTP client，不依赖父进程继承系统代理。真实 models/chat 健康请求均为 200；仍须完成 Task15 全链 E2E，禁止提前进入 Task16。
- 2026-08-30 再复审状态：引用加固相关真实PG/单元52 passed，完整后端387 passed/3 live opt-in skipped，Ruff check、Mypy、Alembic0020通过。修复后的live连续运行曾为2 passed/1因旧120秒ARQ watchdog无reply；watchdog修复后Docker重启使Xinference在线模型消失，三轮均在embedding前置阶段404。当前PG/Redis healthy、Xinference HTTP可达但embedding_healthy=False；等待管理员重新launch bge-m3与bge-reranker-v2-m3后必须重新连续3轮，尚不得宣称Task15最终通过。
- 2026-08-30 BGE恢复后的最终Green：真实Provider诊断定位文本citation缺末尾`]`，未采用容错补字符；DeepSeek改为明确输出结构化document/chunk身份，适配器仅规范化格式，现有BuiltContext校验仍拒绝不存在/错配引用。单轮live先通过1 passed/39.18s，随后正式连续3轮3 passed/111.15s；每轮唯一marker精确命中document/version/chunk，公开history与PG Journal连续，Payment退款严格1。完整后端389 passed/3 skipped，frontend unit18、Playwright7、typecheck/build、Ruff/Mypy/Alembic0020及PG/Redis/BGE健康均通过。
- 督导最终批准（2026-08-30）：批准`d910b8e`与`39c75b8`及此前Task15实现/可靠性提交链。独立关键定向54 passed，Ruff check、Mypy 96、Alembic0020、PostgreSQL/Redis均通过。Task15已闭环；下一项仅为Task16，不得提前进入Task17。

## 任务 16（完成，等待督导复审）

- 严格Red→Green：evaluation包/Schema/指标初始导入产生2个collection error；补契约后缺`AgentEvaluationCase`/citation指标产生2个collection error；0021迁移模型缺失产生1个collection error；语料文件缺失产生1 failed；业务结果精确核验缺`ActualAgentOutcome`产生1个collection error。实现后定向16 passed。
- 冻结语料：dev60、test140、agent60、独立攻击集40；`test.jsonl` SHA-256为`3db1b431a1af6714c1ee4c8d88c297aee36e38dd2c01071c0936a4976010ba19`，manifest明确`final_test_executed=false`。生成脚本和机器复核记录已提交，复现测试逐字节比较所有六个生成产物。
- 确定性指标包含Recall@5、Precision@5、MRR、nDCG@5、工具P/R/F1、未经审批执行率、重复副作用率；另精确核验citation ID、订单号、金额、幂等键、审批状态、工具参数及最终状态。LLM Judge仅补充且异常不影响确定性分数。
- 迁移`0021_evaluation_tables`基于0020，真实PG验证0001→0021、top_k/lifecycle/latency/唯一约束负例及0021→0020→0021；主库current为0021 head。
- 门禁：Task16定向16 passed；完整后端405 passed、3 skipped；Mypy 103 source files通过。Ruff全仓format因既有不可读`backend/JavaProjectsOpsPilot.pytest-temp-agent/`触发工具panic，Task16明确文件集format/check通过。PG accepting、Redis PONG。
- 未实现Task17 runner、公开API、实验矩阵或看板；等待Task16督导复审。

## 需要维护的文档

任务完成后更新：

- `docs/development-progress.md`：里程碑、已完成内容、最新验证数字、下一任务。
- 实现计划中的任务状态行。
- 如设计发生经批准的变化，更新架构规格；不要静默偏离。
