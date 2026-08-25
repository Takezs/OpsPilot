# 交给其他 Agent 的开发提示词

复制下面整段内容，发送给接手开发的 Agent：

```text
你正在继续开发 OpsPilot。请直接执行，不要重新设计整个项目，也不要跳过测试。

工作目录：E:\JavaProjects\OpsPilot
分支：plan/opspilot-core-mvp
当前状态：M1（任务 1–4）、M2（任务 5、任务 6、任务 7）与 M3 / 任务 8（Tool Registry、受限 Agent Loop 与演示服务）已通过督导复审；从 M3 / 任务 9（Policy、审批绑定与 Operation 持久化）开始。

开始前按顺序阅读：
1. AGENTS.md
2. docs/development-progress.md
3. docs/agent-handoff.md
4. docs/superpowers/specs/2026-08-19-opspilot-core-mvp-design.md
5. docs/superpowers/plans/2026-08-19-opspilot-v1-implementation.md 中“任务 9”章节

先确认：
- git branch --show-current 为 plan/opspilot-core-mvp；
- git status，不覆盖任何已有用户修改（除既有的本地文件 .idea/、Docker Desktop Installer.exe、wsl.2.7.3.0.x64.msi 外工作区干净）；
- docker version、docker compose version 和 daemon 可用；
- PostgreSQL/pgvector 与 Redis 容器 healthy；
- Alembic 位于 head（当前为 0010_runs_journal_outbox）。

当前只实现任务 9“Policy、审批绑定与 Operation 持久化”：
- 按实现计划“任务 9”章节的步骤实现确定性退款 Policy（amount <= 100：ALLOW；100 < amount <= 1000：REQUIRE_APPROVAL；amount > 1000：DENY）、Operation 持久化（审批前持久化、服务端生成幂等键 refund:{order_id}、规范化参数稳定派生 arguments_hash、UNIQUE(tool_name, idempotency_key)、并发/重复创建返回已有 Operation）、不可变审批绑定（operation_id + arguments_hash + operation_version，篡改 409、过期不能批准、仅 REVIEWER/ADMIN、并发 Reviewer 单事务胜出）、状态 + run_event + outbox 同事务提交、MANUAL_REVIEW 终态与 manual_review_resolutions 审计；
- 严格不实现任务 10 内容：Claim/Lease、fencing、Worker 执行、结果核对（Reconciliation）或 SSE；
- 新增迁移必须基于当前 head 0010_runs_journal_outbox 生成新 revision，不得复用固定迁移文件名（0004/0005/0006 已被占用）。

严格执行 TDD：
1. 先写失败测试并运行，记录正确红灯；
2. 写最小实现；
3. 运行定向测试变绿（重点：tests/execution/test_policy.py；tests/approvals/test_approval.py 及 tests/approvals/；真实 PostgreSQL 集成测试覆盖 Operation/审批持久化、参数或版本篡改 409、两个 Reviewer 并发审批、唯一幂等冲突、状态 + run_event + outbox 原子回滚、审批过期、MANUAL_REVIEW resolution 不修改原终态）；
4. 重构；
5. 运行 Ruff、Mypy、完整 pytest、Alembic current 和真实容器健康检查；
6. 更新 docs/development-progress.md 和实现计划中的任务 9 状态；
7. 单独提交任务 9，建议提交信息：feat: bind durable approvals to immutable operations。

真实数据库要求：
- Operation/审批持久化、事务、并发与幂等唯一约束必须在 PostgreSQL 16 + pgvector 上验证；
- 禁止用 SQLite、内存数据库或 Fake 数据库替代集成验收；
- Redis 只能作为队列/通知，不能成为 Operation 或审批的事实源。

新增迁移要求：
- 必须基于当前 head 0010_runs_journal_outbox 生成新 revision（alembic revision）；
- 不得复用实现计划旧文档中的固定迁移文件名 0004/0005/0006（已被现有迁移占用）。

不可破坏的约束：
- PostgreSQL 是业务事实、Run Journal 和 Outbox 的事实源，Redis 只做队列/通知；
- Operation 状态、run_event 和 outbox row 必须在同一 PostgreSQL 事务中提交；
- 审批绑定不可变的 operation_id + arguments_hash + operation_version；
- 不修改 M1 已批准与任务 5、任务 6、任务 7、任务 8 已完成的代码（上传、Outbox、retry lifecycle、检索 SQL 过滤、Reranker/Context/Citations、Run Journal/Event Outbox、Tool Registry/Agent Loop），除非任务 9 测试证明存在直接阻塞；
- 不处理 attempt=0 lifecycle 技术债，它不属于任务 9；
- 不提交 .env、API Key、Authorization、PII、临时目录或本地文件；
- 不清理或回退主工作区。

完成后报告：
- 修改的文件和行为；
- commit SHA；
- 红灯证据；
- 定向与完整测试的真实数量/输出摘要；
- Ruff、Mypy、Alembic、PostgreSQL、Redis 状态；
- 事务与并发测试证据；
- 与计划的任何偏离；
- 遗留问题；
- 下一步应是任务 10，但不要自行开始任务 10，先等待复审。

如果文档、代码和计划互相冲突，停止实现并明确列出冲突，不要猜测。
```

## 更省 token 的用法

如果 Agent 上下文较小，只发送上面的提示词，不要同时粘贴完整设计文档。让 Agent 按路径读取当前任务相关章节即可。

如果 Agent 完成任务 9 后需要继续任务 10，把提示词中的“任务 9”替换为实现计划中的下一个任务，并保留环境检查、TDD、完整门禁、单任务提交和等待复审规则。
