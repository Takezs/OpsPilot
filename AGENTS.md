# OpsPilot Agent 开发规则

本文件适用于整个仓库。任何自动化 Agent 在修改代码前都必须遵守以下规则。

## 开始前必读

按顺序阅读：

1. `docs/development-progress.md`
2. `docs/agent-handoff.md`
3. `docs/superpowers/specs/2026-08-19-opspilot-core-mvp-design.md`
4. 当前任务在 `docs/superpowers/plans/2026-08-19-opspilot-v1-implementation.md` 中的章节

不要一次性重读整个实现计划；只读全局约束、当前任务和直接依赖章节，以节省上下文。

## Git 与工作区

- 在 `E:\JavaProjects\OpsPilot` 工作。
- 使用分支 `plan/opspilot-core-mvp`。
- 不修改、清理或回退主工作区中的用户文件。
- 不使用 `git reset --hard`、`git checkout --` 或递归删除仓库内容。
- 每个计划任务单独提交；提交前运行该任务的定向测试和完整质量门禁。
- 不要提交 `.env`、API Key、测试临时目录或本地存储文件。

## 实现纪律

- 严格执行 Red → Green → Refactor → Commit。
- 先写最小失败测试并确认失败原因，再写生产代码。
- 保持纵向切片，不提前实现后续任务。
- 遇到规格冲突或无法证明的行为时停止并报告，不自行扩大架构。
- 不以 SQLite、内存数据库或 Fake 服务替代 PostgreSQL/pgvector、Redis/ARQ 的集成验收。
- Fake Provider 只允许用于纯单元测试；权限 SQL、迁移、索引、并发和恢复必须使用真实 PostgreSQL。

## 不可破坏的架构约束

- PostgreSQL 是业务事实、Run Journal 和 Outbox 的事实源；Redis 只负责队列和通知。
- API 保存文件后只投递 ID；禁止通过 Redis 传文件 bytes。
- 所有知识检索必须在数据库候选层应用 `KnowledgeScope`，不能先检索再在 Python 中过滤。
- 术语使用 PostgreSQL FTS；未引入相应引擎/扩展时不得称为 BM25。
- Retrieval 模块不调用 LLM；Query Rewrite 属于 Generation/Orchestrator。
- 副作用请求可能到达 Provider 时不得盲目重试，必须进入 OUTCOME_UNKNOWN/RECONCILING。
- Operation 状态、run_event 和 outbox row 必须在同一 PostgreSQL 事务中提交。
- Lease fencing 只保护数据库回写；外部副作用防重依赖服务端幂等键和 Reconciliation。
- 审批绑定不可变的 `operation_id + arguments_hash + operation_version`。
- Journal、Attempt、日志和 Trace 必须脱敏并限制长度。
- Evaluation 是 v1.0/简历验收前必做内容，不是可无限延期的可选项。

## 当前开发位置

- M1 / 任务 1–4 已批准。
- M2 / 任务 5：权限感知 Hybrid Retrieval 已通过督导复审。
- M2 / 任务 6：Reranker、上下文预算与引用回答已通过督导复审。
- M2 / 任务 7：Run Journal 与 Transactional Outbox 已通过督导复审。
- M3 / 任务 8：Tool Registry、受限 Agent Loop 与演示服务已通过督导复审（实现 `db2cae5`、复审修复 `8e8adfb`）。
- M3 / 任务 9：Policy、审批绑定与 Operation 持久化已通过督导复审（2026-08-25）：实现 `e82a57c`、复审修复 `276a5cc`（一次性重试授权、副作用路由与 occupancy 加固）、P2 修复 `26ede61`（replacement 绑定不可变与不可删除）。
- M3 / 任务 10：Claim/Lease、fencing 与状态事务已通过督导复审（2026-08-26），批准提交 `f36e435`、`b6729c1`、`3c7cecb`、`9863d4a`；Task 10 只建立 OUTCOME_UNKNOWN/RECONCILING 安全状态边界。
- M3 / 任务 12 可靠 SSE 已通过督导复审（2026-08-26）。
- M4 / 任务 13 Vue 应用壳、登录与 API 客户端已通过督导复审（2026-08-27），批准提交 `10b0d65`、`cb41271`。
- M4 / 任务 14 知识库、检索调试器与引用抽屉已通过督导复审（2026-08-27），批准提交 `2b6dc34`、`6eab4a4`、`d6f98ed`；下一项是任务 15。
- M4 / 任务 15 工作台、审批、Run时间线与核心退款E2E已通过督导复审（2026-08-30）；最终引用加固批准提交 `d910b8e`、`39c75b8`，督导独立定向54 passed，Ruff/Mypy/Alembic0020及PostgreSQL/Redis门禁通过。下一项是任务16。
- M5 / 任务 16 v1.0评测数据集与确定性指标已通过督导复审（2026-08-30），批准提交`bbeea2f`、`34fcba9`、`c43332d`；冻结test SHA为`e0a5eb5a474eeaeeeeeb6a0efbed741e0d422f18fc48af0099ec0ec987ffbf8c`且尚未执行。下一项是任务17。
- M5 / 任务 17 异步实验 Runner、故障矩阵与评测看板已通过督导复审（2026-09-02），批准提交 `def4c63`、`8e6d406`、`2aff943`。正式矩阵 Run `0108b7b2-991d-49d3-9e6e-d2ff50ce5c38` 可靠恢复 60/60、任务成功 59/60；Alembic 0023 head，冻结 test 未执行。下一项是任务18。
- M5 / 任务 18 步骤1–5第四轮P1修复与复验已完成，等待督导再次go/no-go复审；credential key重复percent/quoted escape规范化及Decision实际Prompt快照计费已加固。真实Provider全链连续3/3通过；冻结test未执行，步骤6/7、v1.0 tag均未开始。
- `attempt=0` outbox lifecycle 是已记录的非阻塞技术债，不要顺带重构。

## 常用验证命令

从仓库根目录执行：

```powershell
docker compose ps
docker compose exec -T postgres pg_isready -U opspilot -d opspilot
docker compose exec -T redis redis-cli ping
```

从 `backend` 目录执行：

```powershell
..\.venv\Scripts\ruff.exe format --check .
..\.venv\Scripts\ruff.exe check .
..\.venv\Scripts\mypy.exe --no-incremental src
..\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider `
  --basetemp=E:\JavaProjects\OpsPilot\.pytest-temp-agent
..\.venv\Scripts\alembic.exe -c alembic.ini current
```

Windows 默认临时目录可能出现 ACL 错误；使用仓库内唯一 `--basetemp`。不要把 ACL 错误误报为代码或数据库失败。

## 完成任务后的交接格式

报告必须包含：

- 完成的计划任务和范围；
- commit SHA 与提交信息；
- 定向测试和完整测试的真实通过数量；
- Ruff、Mypy、Alembic、Docker/服务健康证据；
- 与计划的任何偏离；
- 遗留问题和下一任务；
- 对 `docs/development-progress.md` 的同步更新。
