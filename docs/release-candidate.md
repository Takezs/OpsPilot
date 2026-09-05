# Task18 发布候选（冻结 test 前）

## 不可变配置

Canonical JSON 与 SHA 见 [`evaluation/release-candidate.json`](../evaluation/release-candidate.json)。该候选只允许 dev smoke；没有创建正式 test execution receipt，`final_test_executed` 保持 false。

## 隔离 RC 验证（2026-09-02）

- Compose project：`opspilot_task18_rc_20260902_01`；独立 PostgreSQL/Redis/文档卷与回环端口，不复用主库。
- 两次 seed：`users=1, knowledge_bases=0, documents=0, evaluation_test_executions=0`，同一 user ID；临时密码未落盘、未输出。
- dev smoke：只加载 `dev.jsonl`，经公开 API、真实 PostgreSQL/Redis/BGE/DeepSeek 退出 0。
- 镜像 ID：API `b0526c89...`、Worker `355d0cd3...`、Publisher `24635b5d...`、Web `31fb0f35...`、Order `78f875ff...`、Payment `2d428401...`、Email `c802a100...`。
- smoke 前后 test SHA 均为 `e0a5eb5a474eeaeeeeeb6a0efbed741e0d422f18fc48af0099ec0ec987ffbf8c`，manifest 标志均为 false，RC 与主库 receipt 均为 0。
- 验证结束后仅删除该 project 的容器、network、volumes；三类资源剩余数均为 0，主拓扑保持 healthy。

## 首轮复审加固（2026-09-03）

- Agent deadline覆盖decision、grounded generation、retrieval/rerank、READ_ONLY与SIDE_EFFECT durable边界；不响应取消任务受监督并有界drain。grounded generation计入模型调用预算。
- 日志先安全格式化再按credential key脱敏；ARQ startup补装最终handler filter；Trace只记录异常类型与hash/length摘要。
- 文档卷mountpoint为`10001:10001`、`0750`。隔离project `opspilot_task18_rc_20260903_02`的公开上传由非root API写入、Worker读取并经真实BGE到`READY`；重启API/Worker后文件和Chunk保持。
- Worker/Publisher各自通过Redis TTL heartbeat健康检查；Redis不可用时检查不再为healthy，恢复后均回到healthy。
- seed要求active ADMIN、显式`["*"]`知识范围和CONFIDENTIAL；冲突身份非零退出且不修改，兼容身份重复执行不改密码。
- 全新PG最终完整后端门禁为`467 passed, 4 skipped in 1373.19s`；真实DeepSeek+BGE全链在显式IPv4 Redis下连续`3 passed in 258.09s`，dev-only smoke再次退出0。冻结test SHA、manifest false和主库/RC零receipt均再次核对；隔离RC容器、network与volumes验证后已删除。
- 第二轮安全加固增加access query与quoted/nested mapping日志脱敏，以及grounded context/retry累计input-token预算。Red为7项真实失败，Green扩展定向163、全新PG完整`476 passed, 4 skipped in 93.91s`。
- 环境恢复后的当前代码复验：真实DeepSeek+BGE全链以独占Worker和IPv4 Redis连续`3 passed in 171.61s`。隔离project `opspilot_task18_rc_20260905_01`全拓扑healthy、0023 head；双seed幂等，UID10001 API公开上传后UID10001 Worker经真实BGE索引到`READY|1`，重启后文件与DB事实保持，dev-only smoke通过。日志canary与预算/脱敏聚焦`28 passed`；RC清理后主Worker/Publisher恢复。
- 冻结test SHA保持`e0a5eb5a474eeaeeeeeb6a0efbed741e0d422f18fc48af0099ec0ec987ffbf8c`，manifest false，主库与RC receipt均0，正式矩阵Run保持`COMPLETED|60`。
- 第三轮P1加固：access分号/混合分隔符及quoted escape/multiline旁路改由有界scanner fail-closed；Generation的实际system/user消息以不可变快照在收费与Provider发送间共享，每次retry按当次完整wrapper、metadata和正文重新计费。Red `10 failed, 30 passed`，Green聚焦90、完整独立PG后端`488 passed, 4 skipped`，真实Provider连续`3 passed in 133.41s`。
- 当前镜像隔离RC `opspilot_task18_rc_20260905_02`完成0001→0023、双seed、非root公开上传与真实BGE `READY|5`、重启持久化、dev smoke和分号access canary；正式receipt保持0，验证后隔离资源已删除、主拓扑恢复healthy。
- 第四轮P1加固重复percent与quoted escape key规范化，并把Decision完整system/tool schema/wrapper/state渲染成计费与发送共享的不可变快照。Red `6 failed, 40 passed`；Green聚焦86、完整独立PG `496 passed, 4 skipped`、真实Provider连续`3 passed in 287.23s`。
- 当前镜像隔离RC `opspilot_task18_rc_20260905_03`完成0023、双seed、非root公开上传真实BGE到`READY|5`、重启持久化和double-percent canary。dev smoke首轮因真实Provider返回`answer=null`按契约失败，独立新Run重试通过；两次结果均保留，不将失败伪报为Green。RC receipt为0且资源已删除。
- 第五轮P1把percent与backslash/Unicode escape合并为最多4轮的固定点key规范化，非法、残留、超深及超长输入均fail-closed。精确Red `6 failed, 35 passed`，Green redaction41、Task18聚焦104、独立PG完整`506 passed, 4 skipped`，Ruff/Mypy与frontend门禁通过。
- 本轮live复验未通过：容器配置重载出现Provider认证失败；独占本地Worker随后证明真实检索/生成可用，但无法访问未映射的demo Payment/Order端口，且两轮真实决策未创建Operation，最终`3 failed`。未修改prompt、未伪造引用或业务状态，等待督导裁决。
- 本节仍只记录冻结test前验证；没有创建或启动正式test execution。

## 备份与回滚

1. 启动冻结 test 前使用 `pg_dump --format=custom` 备份 PostgreSQL，并记录数据库、应用镜像 digest 和 Alembic head。
2. 备份 `opspilot_storage` 卷；Redis 不是事实源，不用 Redis 快照替代 PostgreSQL 备份。
3. 发布失败时停止新 Worker/Publisher，回滚到记录的镜像 digest；数据库只使用经验证的 Alembic downgrade。若冻结 test 已进入 RUNNING，绝不删除 receipt 或换配置重跑，只恢复同一 execution。
4. 恢复后核对正式矩阵 Run、60条case、Journal seq 和 Payment 幂等记录。

## 一次性冻结 test 清单（等待单独 go/no-go）

- [ ] 核对 dataset SHA 和 canonical configuration SHA。
- [ ] 核对 PostgreSQL 备份、镜像 digest、0023 head和服务健康。
- [ ] ADMIN freeze 返回唯一 execution ID；并发freeze只有一胜。
- [ ] ADMIN start 同一 execution ID；禁止创建第二配置/第二receipt。
- [ ] Worker 崩溃只 resume 同一 execution，补缺失 repetition。
- [ ] 完成后保存原始case、JSON/CSV/HTML与失败样本，不手改数字。
- [ ] 督导明确 go/no-go 前全部保持未勾选，不执行Task18步骤6/7。
