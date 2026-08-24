# 交给其他 Agent 的开发提示词

复制下面整段内容，发送给接手开发的 Agent：

```text
你正在继续开发 OpsPilot。请直接执行，不要重新设计整个项目，也不要跳过测试。

工作目录：E:\pyproject\OpsPilot\.worktrees\opspilot-planning
分支：plan/opspilot-core-mvp
当前状态：M1（任务 1–4）已完成并批准；从 M2 / 任务 5 开始。

开始前按顺序阅读：
1. AGENTS.md
2. docs/development-progress.md
3. docs/agent-handoff.md
4. docs/superpowers/specs/2026-08-19-opspilot-core-mvp-design.md
5. docs/superpowers/plans/2026-08-19-opspilot-v1-implementation.md 中“任务 5”章节

先确认：
- git branch --show-current 为 plan/opspilot-core-mvp；
- git status，不覆盖任何已有用户修改；
- docker version、docker compose version 和 daemon 可用；
- PostgreSQL/pgvector 与 Redis 容器 healthy；
- Alembic 位于 head。

当前只实现任务 5“权限感知 Hybrid Retrieval”：
- 定义统一 RetrievalCandidate；
- 实现 Dense 和 PostgreSQL FTS 两路检索；
- 两路 SQL 必须在数据库候选层应用 KnowledgeScope 的部门/access_level 过滤；
- 实现 RRF、按 chunk 去重、Dense/FTS/RRF 调试阶段；
- 默认每路 30 条，融合 20 条；
- 术语只能写 PostgreSQL FTS，不得称为 BM25；
- 不实现 Reranker、LLM、Query Rewrite、引用回答、Run Journal 或前端，这些属于后续任务。

严格执行 TDD：
1. 先写失败测试并运行，记录正确红灯；
2. 写最小实现；
3. 运行定向测试变绿；
4. 重构；
5. 运行 Ruff、Mypy、完整 pytest、Alembic current 和真实容器健康检查；
6. 更新 docs/development-progress.md 和实现计划中的任务状态；
7. 单独提交任务 5，建议提交信息：feat: add permission aware hybrid retrieval (task 5)。

真实数据库要求：
- 权限、Dense、FTS 和索引查询必须在 PostgreSQL 16 + pgvector 上验证；
- 禁止用 SQLite、内存数据库或 Fake 数据库替代集成验收；
- Fake Embedding 只允许生成确定性查询向量，不允许替代 SQL 权限和索引验证。

不可破坏的约束：
- PostgreSQL 是事实源，Redis 只做队列/通知；
- 不修改 M1 已批准的上传、Outbox、retry lifecycle，除非任务 5 测试证明存在直接阻塞；
- 不处理 attempt=0 lifecycle 技术债，它不属于任务 5；
- 不提交 .env、API Key、Authorization、PII、临时目录或本地文件；
- 不清理或回退主工作区。

完成后报告：
- 修改的文件和行为；
- commit SHA；
- 红灯证据；
- 定向与完整测试的真实数量/输出摘要；
- Ruff、Mypy、Alembic、PostgreSQL、Redis 状态；
- 遗留问题；
- 下一步应是任务 6，但不要自行开始任务 6，先等待复审。

如果文档、代码和计划互相冲突，停止实现并明确列出冲突，不要猜测。
```

## 更省 token 的用法

如果 Agent 上下文较小，只发送上面的提示词，不要同时粘贴完整设计文档。让 Agent 按路径读取当前任务相关章节即可。

如果 Agent 完成任务 5 后需要继续任务 6，把提示词中的“任务 5”替换为实现计划中的下一个任务，并保留环境检查、TDD、完整门禁、单任务提交和等待复审规则。
