# 面试证据指南

- 权限：Dense/FTS SQL 候选层过滤，不是 Python 后过滤。
- 可解释性：生成时版本化 CitationSnapshot，不回查“当前最新版”伪造证据。
- 可靠副作用：稳定幂等键 + OUTCOME_UNKNOWN/Reconciliation；lease fencing不撤销外部动作。
- 一致性：Operation/Event/Outbox 同一 PostgreSQL 事务，Redis 只通知身份。
- 评测：正式矩阵可靠恢复60/60，但任务成功59/60；一次额外只读工具调用导致质量失败被原样保留。
- 可观测性：只记录 run_id、摘要和结构化决定；Prompt、Provider body、密钥和PII不进入Trace。

所有数字以 [`results.json`](../evaluation/reports/0108b7b2-991d-49d3-9e6e-d2ff50ce5c38/results.json) 为唯一依据。
