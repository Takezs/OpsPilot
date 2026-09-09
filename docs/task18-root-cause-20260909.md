# Task 18：直接诊断与评测器修复（2026-09-09）

本轮接手实际代码和运行环境，不再以转发报告代替执行。原冻结 execution、receipt 和结果保持原样。

## 实测事实

- 主库只读查询：receipt 为 1，状态 COMPLETED；目标 evaluation_run 有 320 条结果；迁移 0025_eval_msg_corr。
- 当前主库共 12 个 Chunk。按既有生成器的固定 namespace 和命名规则计算，test/agent 非澄清模板要求 171 个不同 Chunk ID；与当前主库交集为 0。这证明当前评测知识前置条件不满足，不能根据非空候选推断模型遵从性问题。此查询不证明历史每一时刻的数据库内容。
- 47 条无候选与模板结构一致：test 20 条澄清，加上 agent 9 条澄清 × 3 次 = 47；这些用例原本就没有 relevant_chunk_ids，Runner 会跳过检索。因此不能将其直接解释为 47 次检索失败。
- DeepSeek 使用运行中 Worker 的 Settings/认证配置，对临时独立上下文首次生成即返回精确 citation，insufficient_evidence=false。
- 非冻结公开 Run/message 诊断真正完成：真实 BGE 1024 维、5 个候选、精确 Chunk 命中、1 条精确 document/version/chunk 引用。加强后使用只存在于 PG Chunk 的独立随机 verification_code（用户 query 不包含该 code），真实回答包含该 code。两轮临时数据均按自身随机 ID 清理，不调用 Payment 控制接口。
- 诊断入口为 backend/tests/e2e/diagnose_grounded_readonly.py；没有重跑冻结测试或创建 evaluation receipt。超时或存在 Operation 时保留临时数据，避免删除运行中任务。
- 本地 Web http://127.0.0.1:8088 返回 HTTP 200；这不等于 v1.0 验收通过。

## 实际代码修复

1. 评测器在检索和创建 Run 之前，用同一 USER 的公开知识 API 检查预期文档 READY、版本、精确 Chunk 归属及非空正文。缺失/越权/不可用时明确阻止该 case 执行。此检查是运行前置防护，不能替代整个数据集在创建收据之前的部署验收。
2. 重复副作用指标只计 EXECUTION 成功，不再把 RECONCILIATION 成功当作退款执行成功。多个成功执行仍只是代理信号；真实重复退款必须核对 Payment 业务记录，不能用该指标替代。
3. 无 Operation 时，业务结果使用 Journal 的 response_kind（ANSWERED/CLARIFICATION），不再错误地与 Run 生命周期 COMPLETED 比较。
4. 0024/0025 仅修正 import 排序和格式，未改变 revision 或迁移语义。

## 验证

- 计数误报 Red：2 failed、3 passed；修复后通过。
- 缺证据前置 Red：3 failed；修复后加入 READY/精确 ID/空内容/授权边界测试。
- 回答状态 Red：2 failed；修复后正常回答/澄清均按业务语义判定。
- 定向组合 64 passed；Mypy 122 source files 通过；Ruff src/tests/alembic/generator 全范围 check 通过。
- Bugbot 只读复审：无 P0/P1；诊断 code 与 query marker 相同的 P2 已修复，增量复审通过。
- 独立 scratch 全量门禁：589 passed、4 skipped（1386.48 秒），退出码 0；0001→0025 升级成功，临时数据库已清理。
- 前端 19 unit、8 Playwright 回归通过，typecheck/build 通过（既有 bundle 大小提示）。
- 修复版运行镜像：sha256:665b4e348c03fe3b35f9fc37eea14d83041dbef80a632308c0860643e2db89b9；已更新本机 API/Worker/Publisher，继续使用原外部凭据文件与数据库，未创建新的评测收据。此为本机修复部署，不是 v1.0 tag 或对外发布。

## 剩余发布前置

需要独立、版本化的来源知识和订单/场景 fixture，以及与其一致的评测预期。不能把既有 expected_facts 倒灌为知识以凑分，也不能把缺失来源归咎于模型。先建立并验证来源/权限/身份/场景部署契约，再进行后续评测决策。历史冻结分数原样保留，不覆写为修复后的分数；当前未 tag、push 或外部部署。
