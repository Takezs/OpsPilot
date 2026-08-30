# OpsPilot v1 Evaluation Corpus Review

本目录由 `evaluation/generate_datasets.py` 确定性生成。数量、分类覆盖和冻结哈希的机器可读事实位于 `review.json` 与 `manifest.json`；本记录不手抄或重算报告数字。

冻结检查：

- `dev.jsonl` 与 `test.jsonl` 使用独立 case ID；`agent_tasks.jsonl` 与 `attacks.jsonl` 也有独立命名空间。
- Schema 覆盖相关 Chunk、精确 citation ID、必要/禁止事实、预期/禁止工具、追问、审批、订单号、金额、业务幂等键和最终状态。
- 攻击集与 dev/test 分离，覆盖提示注入、工具输出注入、越权检索、审批绕过和重复副作用请求。
- `test.jsonl` 的 SHA-256 在 `manifest.json` 中冻结；Task 16 仅验证 Schema、覆盖与哈希，没有运行最终 test 评分。
- 生成器复现测试要求六个机器生成文件逐字节一致，防止手改用例或复核数字。
- LLM Judge 仅为可选补充；超时或失败只记录安全错误类型，不阻断确定性指标。

复核结论：Schema 和生成规则已冻结，可供后续 Task 17 的公开 API Runner 使用；Task 17 在参数冻结前不得执行最终 test 集。
