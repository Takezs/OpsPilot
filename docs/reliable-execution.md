# 可靠执行

审批绑定 operation ID、参数哈希和版本。Worker 以 PostgreSQL claim/lease、token/owner/version fencing 写回；fencing 只阻止旧 Worker 写数据库，外部副作用防重依靠稳定业务幂等键。

可能已到达 Provider 的失败进入 OUTCOME_UNKNOWN，再由 Reconciliation 按 provider reference 或幂等键核对。只有明确未执行才可重试。Operation、Attempt、Run Event、Outbox 和下一 job intent在同一事务提交。正式故障矩阵三个故障点各20次，可靠恢复60/60、重复副作用0、丢失Operation 0；任务成功率为59/60，不能表述成60/60。
