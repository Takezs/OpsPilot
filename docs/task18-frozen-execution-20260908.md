# Task18 唯一冻结评测：FAILED，等待督导裁决

2026-09-08 督导批准候选 `6e6802ca7683afab6f472662e85eb6343c9c52f7` 后，通过 ADMIN 公开 freeze/start 契约创建唯一正式收据。未更改配置、模型、Prompt、语料或业务代码。冻结 test 已执行一次，当前结果不完整，不能宣称完成 v1.0 验收。

- Execution：`360b3d08-0f2a-4105-87f1-cd02ed8bc93b`；Evaluation Run：`1e591a28-d522-4702-9695-c550ff41d703`。
- Receipt 数：1；状态：FAILED；启动时间：2026-09-08 09:59:38 UTC。
- Dataset SHA：`e0a5eb5a474eeaeeeeeb6a0efbed741e0d422f18fc48af0099ec0ec987ffbf8c`。
- Configuration SHA：`ce62b4409c5ac4e5e1d3be3b5577e59b123c0d06fd17f590830453a492c43693`。
- Worker 镜像：`sha256:ef979611da5879c6064a1c713f0a1b35a9e41ca6b438fbb30b8e478e54905a1d`，由批准提交构建且通过 bytes 身份校验。

## 失败与唯一恢复

Attempt 1：09:59:38–10:00:54 UTC，`TimeoutError: public Run API did not produce an assistant result`。适配器等待窗口为 240×0.25 秒；保存10条case结果，创建13个Run，之后均COMPLETED。原Run和Journal未删除。

按授权对同execution、同config调用一次既有resume。Attempt 2：10:02:54–10:04:04 UTC，再次同类TimeoutError。保存结果从10增至12，新增5个Run。既有适配器对缺失结果创建新Run；这项偏离已即时报告，原Run全部保留，未手工补写case结果。

督导明确禁止相同失败后的第三次盲目resume，已停止恢复。总计2个Attempt、1次resume；不是第二次创建冻结收据。未恢复备份、重建receipt、修改timeout或重采样以提升分数。

## 持久化结果及限制

应有140个test case + 60个Agent case×3，共320个case/repetition；实际12个唯一结果，缺308个，无额外或重复tuple。已保存12个结果全部task_success=false。聚合值仅覆盖这12条，不能解释为完整test成绩：task_success_rate=0，Recall@5/MRR/nDCG@5均0，citation_correctness_rate=1/6，p95 latency=51874ms。副作用/未审批执行指标的0不构成安全验收，因为尚未完成Agent部分。

12条有结果Run的PG Journal连续。正式运行窗口内共18个Run（Attempt1创建13个、Attempt2新增5个），最终全部COMPLETED、各3条连续Journal，无遗留RUNNING；其中6个Run没有对应持久化case结果，不手工补写。完整Run状态快照另保存在报告目录`run-audit.json`，保留延迟Run，不只展示已评分Run。Operation数0；正式Payment日志观测到的POST /refunds请求数0。未调用Payment reset/count/fault端点，未启用DEMO_E2E/EVAL_FAULT_MATRIX或控制secret，未使用E2E订单。

报告目录：`evaluation/reports/1e591a28-d522-4702-9695-c550ff41d703/`。JSON/CSV/HTML各经公开API导出两次并验证字节相同；另保留包含成功/失败原始持久化行的cases.json、收据/Attempts/Journal摘要execution-audit.json。

- results.json SHA：`66101a288889bfb71e02a681baa84f97424716ab313a0b168b29d723bfd6cb74`
- results.csv SHA：`fd220d34e2a03998a3be1c1ab834e7414e22ebc22b5737045ee6f7d1fb2fbac4`
- results.html SHA：`c19cfbc94f83e34ec57cbb2cdac953df4b79f6814ef656b0be64aa62968703a5`
- cases.json SHA：`ebc7b86906cd1cade5dcbe6df7c385c3be1f585e5db7ac4a1e6b6214a2ead57c`
- run-audit.json SHA：`ca578a6e9772ccc956287e5ab47256cb68bb26138ace2de5ba07056b8c60a280`

## 备份与边界

执行前仓库外备份`E:\OpsPilot-release-20260908\before-freeze.dump`，SHA `dce621b1db2ea0b168ff8627e7f347a1a9482d65d3861cde33ddd57d94b16928`，已通过pg_restore目录读取及二次SHA校验。原始日志、执行后PG快照及角色文件在同一受限ACL目录，未加入Git。USER权限固定evaluation/support、max_access_level=1；未根据冻结答案重建知识库或Payment事实。

18个Run终态确认后另存`after-drain.dump`（不覆盖较早快照），SHA `1b3078c089d4c15f96380ff3105db77fcbf3df3234a8ccefdf3cb59e5d2334e6`。正式报告没有删除失败、重算评分或通过补写掩盖缺失。

启动前receipt=0、Alembic0023、PG/Redis/API/DeepSeek models+chat/BGE embedding+reranker及三角色文件鉴权通过；secret canary未命中。manifest文件依照批准约束不可修改，因此其final_test_executed仍false；实际已执行状态以PG唯一receipt及本报告为准，不能据此再次执行。

发布结论：步骤6尝试已消耗唯一机会，但完整性失败。停止等待督导裁决；未开始步骤7、tag、push或外部发布。既有用户未跟踪文件、安装包和ACL临时目录未编辑或清理。

## P1 修复后的最后一次恢复

督导批准 `7a46169bf2478e4c146adae34f3c08f424e37a93` 后重建镜像 `sha256:e611f83737a715e68927f9b3555d456239a3317a148d555d534371fa1a61bdc2`，并授权同 execution 最后一次恢复。恢复在处理 `test-013` 时立即失败：运行中的主 Compose 数据库尚未应用候选的 0024/0025 migrations，API 查询新建的 `agent_runs.evaluation_correlation` 返回 `UndefinedColumnError`。因此没有新增 Run、case、Operation 或 Payment 请求，现有12条case、18个已完成Run和历史Attempts均未改写。Attempt 数现为3（第三次FAILED），receipt仍为1，结果仍12/320。禁止再恢复；该部署迁移阻塞和不完整结果交由督导裁决。
