# Task 18 步骤 1–5：Compose 控制面修复验收

日期：2026-09-06（Asia/Shanghai）；下列日志时间使用 UTC。
工作分支：`plan/opspilot-core-mvp`，验收源码基线 `9eeb432f3bbb1c22a0648ee2e0d9972c004b6de0` 加本报告所在提交的 Payment / 测试 / Compose diff。
本文件是开发验证记录，不是冻结评测执行收据。未执行冻结 test、未创建正式 receipt、未开始步骤 6/7、tag 或 push。

## Findings 与最小修复

1. 旧 live 测试启动本地 API/Worker/Publisher/demo，而 Docker Worker 同时消费队列；业务 Payment 与断言 Payment 可能不属于同一拓扑。新增显式 Compose 模式使用已有公开 API 与 Payment E2E 控制面，不启动第二套进程。隔离验收使用回环端口 28000/28102；普通模式保留 18000/18102。
2. 容器 Provider URL 的 `127.0.0.1` 指向容器自身。容器 DeepSeek proxy 使用 `http://host.docker.internal:7897`，BGE 使用 `http://host.docker.internal:8080/v1`；Order/Payment/Email 使用 Compose DNS。
3. Payment count 接受 DEMO_E2E，reset 原先只接受 EVAL_FAULT_MATRIX；仅启用 DEMO_E2E 的三轮在创建 Run 前全部 reset 404。reset 现在接受任一显式测试开关，仍拒绝未定义订单，默认配置仍 404。仅重置 demo 内存，不清 PostgreSQL 审计，不改退款幂等语义。
4. 接手时未提交的 Runner 词法规则将工具名提及当成调用要求，阻止正常解释、否定和缺参澄清。督导裁决撤去；Runner 生产代码对基线零 diff。未更改用户 Prompt、system prompt 或扩大意图协议；完整参数的工具决定仍由模型产生，经 Registry/schema/Policy/Approval/durable handler。

`OPSPILOT_DEMO_E2E` 和 `OPSPILOT_EVAL_FAULT_MATRIX` 仅用于显式演示/测试，禁止在生产启用。Payment 映射仅绑定 `127.0.0.1`；未开放外部监听，不使用真实支付凭据。

## Red → Green

- 接手前词法规则原 Red `1 failed, 30 deselected`、Green `3 passed, 28 deselected`、Runner+redaction `88 passed` 是历史证据；该规则最终被撤去，不作为最终正确行为。
- 本轮语义回归 Red：`3 failed, 1 passed, 30 deselected`，失败为否定、解释、缺参澄清均被耗尽 8 轮而无结果。
- Payment Red：`1 failed, 1 passed, 9 deselected`，仅 DEMO_E2E 时 reset 404，默认关闭负例通过。
- 修复后聚焦 Runner / Budget / Observability / Demo：`126 passed in 12.39s`。
- 真实同镜像控制面：DEMO_E2E reset 成功=true，关闭 reset 拒绝=true，未知订单拒绝=true。关闭检查在同镜像临时 Payment 进程的 ASGI HTTP 边界完成；启用和未知订单检查从宿主实际回环 HTTP 访问完成。
- 隔离 RC 首次迁移因缺少合规 JWT 配置被生产校验拒绝；注入独立测试配置后 0001→0023 成功。该环境预检不计作 live 测试 attempt。

## 同镜像与配置

- Compose project：`opspilot_task18_rc_20260906_01`。
- Network：`opspilot_task18_rc_20260906_01_default`。
- API 实际容器 image：`sha256:80e153778d15142b7e4c388dba75dd8c669b38ba4ca6501944bbab9a08907b76`，启动 2026-09-05T16:35:11Z。
- Worker image：`sha256:e5505b9eb55e45b713852a301d665baae3f15e0f71831b09d6226c41d58c9ed3`，启动 16:35:17Z。
- Payment image：`sha256:f902a95dc90b27b2d52093d83d4b9eb8b4ca2f44e79ea8d00be9e0ee0d7f183a`，启动 16:32:45Z。
- 非敏感实际容器 configuration SHA-256：`be4c5827d9fb7dfb07a2feabba1faac96c93f026e52c9c407cac7dcb30e7b94b`。输入为按 API/Worker/Payment 顺序的 name/image/command/非敏感环境白名单/networks，JSON `sort_keys=True,separators=(',',':')`；不包含密钥，也不是最终冻结评测 configuration SHA。
- 非敏感环境：production；DeepSeek base `https://api.deepseek.com`、运行默认模型 `deepseek-v4-flash`；BGE `bge-m3` / `bge-reranker-v2-m3`；Order `http://order:8101`、Payment `http://payment:8102`、Email `http://email:8103`；Payment DEMO_E2E=true、目标 ORD-002。
- 单轮与随后三轮期间没有源码、配置更改或容器重启。三轮结束后补建 Web 时 API 标签发生更新，`compose images` 对旧标签解析报错；`docker inspect` 确认实际运行 API 容器仍为上述 image、启动时间未变。验收归属实际容器摘要，不归属可变 latest 标签。

## 本轮逐次 live 事实

单轮（16:35:54–16:36:22）：`0962dd66c302447284f5d8fb8b69378b`，Run `62aed94b-66c8-4429-9f37-67830d423327`，Operation `5933fef5-2265-4605-80fb-15a3ef69bec5`；`1 passed in 28.37s`，seq=13。

随后连续三轮，`3 passed in 114.96s`：

- iteration 0（16:36:53–16:37:47）：`34ae5ea8c1984df1a917ed7ab3e91154`，Run `17a6f27d-4273-4371-abf9-87fd44a8efc4`，Operation `01b9cbd4-0b1f-4a00-82dc-a3d7d0697537`；seq=13。
- iteration 1（16:37:47–16:38:11）：`ed009c9808b94ecaac11bd3c27c0a383`，Run `05451807-8df6-431b-aebe-621bbe50b365`，Operation `8e1656ab-28b3-4d6f-b4a7-a1e7957597a1`；seq=13。
- iteration 2（16:38:11–16:38:47）：`5ba8297b8004475bb39c2df64d69477f`，Run `43df9789-5600-423c-8efb-5c604b6f3002`，Operation `a71a7dbb-f1a5-4365-b9bb-cad3c0fc4fc9`；seq=12。

四次均独立验证真实 DeepSeek+BGE、固定 document/version/chunk 精确 citations、公开 history 与 PG journal seq 从 1 连续且逐项相等、审批前 WAITING_APPROVAL、Payment timeout-after-effect → OUTCOME_UNKNOWN → RECONCILING → SUCCEEDED、Run COMPLETED、Payment refund count 严格 1。证据是当次测试输出的 `LIVE_EVIDENCE` 与断言，测试 finally 随后清理自身 Run/User/KB；不宣称这些 PG 行现在仍存在。

## 本轮复审历史 attempts：追加清单与缺口

来源：官方开发任务 `01a03db4-e16a-7051-aad8-49b206b1f6e6` 的 read_thread，以及同一任务本地原始 `item_completed` 记录的完整 aggregated_output。命令 ID 是来源标识，不伪装成业务 attempt UUID；时间为命令结束时间，无法恢复开始时间处不推算。

- 10:55:52，`exec-fd716081-bd21-470f-b7fe-3c64ac209f83`，iterations 0/1/2，`3 failed in 575.06s`。三个 Run 为 `98788573-c8e8-4323-8612-2e664d999fff`、`9fc8f7ff-a9e0-46b6-ae4e-8c482916435a`、`dc4070a3-dd9a-4743-87ed-9a309d8a249b`；均 history seq2 超时，Provider 配置/连接阶段，无 Operation。
- 11:06:23，`exec-a87b1768-33b0-4056-8506-036daf05f84a`，iterations 0/1/2，`3 failed in 572.67s`。Run 为 `9313feca-3d94-4a65-a65c-9ecc33f87cd5`、`c7866cfb-554a-429b-8891-31458acc8b7f`、`ad4704ba-c53f-494d-a695-90e03e95728e`；均 history seq2 超时，无 Operation。这是另一组三次，旧“六次总尝试”交接漏计，不能与前组归并。
- 11:15:26，`exec-673c87a5-39e1-4e6b-a291-77121737a675`，`3 failed in 467.07s`。iteration 0 到达退款终态断言但缺 reconciliation 事件，Run/Operation UUID 未输出；iteration 1 Run `54ff34c5-5af6-4b58-9e37-33a462fda7dd`、iteration 2 Run `cbc42e43-7703-4690-914b-db5cf3c7fd65`，均 COMPLETED 但 operations=[]。
- 11:27:17，`exec-89997ab5-f958-495c-8d4f-cc6254dcb144`，iteration 0，`1 failed in 35.58s`；业务已创建 Operation 并到退款终态检查，缺 reconciliation；UUID 未输出。
- 11:29:03，`exec-f31d03f9-4a73-4101-b343-eb13b195fd17`，iteration 0，`1 failed in 44.96s`；同类缺 reconciliation，UUID 未输出。
- 11:32:47，`exec-8cd3af42-d3f3-4af7-8c00-3f2863549ee6`，iteration 0，`1 failed in 159.95s`；业务已通过 reconciliation 检查，断言所查 Payment count=0，暴露混合拓扑，UUID 未输出。
- 11:35:07，`exec-ca345c63-aa39-450f-b0d3-f444098b6390`，iteration 0，`1 passed in 67.10s`；完整链，业务 UUID 未输出；不得补造。
- 11:36:38，`exec-5be6e15c-ea1a-4915-850d-619b6fb1f866`，exit=1，输出只有 `FF`，没有完整总结、iteration/Run/Operation 身份或失败阶段。单列 **unresolved evidence**，不和下一组三轮合并，也不假定第三轮完成。
- 16:00:50，`exec-1781483b-23d7-4ce4-be49-7d6b66f9dc04`，iterations 0/1/2，`3 failed in 3.92s`；全部 reset 404，Run 明确未创建，分类 SETUP/CONTROL_PLANE。
- 本轮另追加上述单轮和连续三轮 4 次，不与历史单轮重复计数。

本轮复审窗口（2026-09-05T10:35Z起）可完整归属结束命令与轮次的 attempts **20**：setup/control-plane=3，business-chain=17，完整成功=5。互斥 primary failure stages：SETUP_CONTROL_PLANE=3、HISTORY_CONNECTION=6、NO_OPERATION=2、MISSING_RECONCILIATION=3、PAYMENT_COUNT=1，共15失败；setup 3不包含在history 6中。20 次中 **12 次有真实 Run/attempt 身份**（历史 8、本轮 4），另 **8 次只有唯一命令/轮次证据**，不补造业务 UUID。

另有 **1 条未解析命令证据 `FF`**，可证明出现两个失败标记，但不能证明完整执行次数、阶段或业务身份。因此所有历史尝试的最终总数仍未完全解析；20 是已解析小计，**不是宣称总共只尝试 20 次**。旧“6 attempts”和阶段“至少10 attempts”均不得继续当最终总数。

旧 live 的 finally 删除 Run/User/KB；最初六次失败在 PostgreSQL 中没有保留，只能由原始测试输出和 worker 日志证明，无法恢复的 PG 行不得伪称存在。该证据缺口与 `FF` 身份缺口保留，不因本轮 3/3 被抹去。

### 更早版本的执行输出索引（不混入本轮修复分母）

追加检索同一任务2026-09-02至本轮窗口前的原始命令，也发现以下输出。它们跨越更早提交、配置和拓扑；不能用其成功替代本轮RC验收，也不能隐去失败。这里只登记原始命令结束时间和结果，不虚构业务UUID：

- 09-02 04:49:22，`exec-7ffad546-d81c-4962-9447-a813b3522ee8`：3 passed /253.53s。
- 09-04 10:09:17，`exec-ce03ff2a-b8e8-4979-bd35-48ca5b125150`：3 failed /573.70s。
- 09-04 10:19:38，`exec-97b1ce04-5666-4dfe-acfe-c1c9d83ba42f`：3 failed /574.53s。
- 09-04 10:26:18，`exec-2a45b8ec-e564-4c88-9210-c3f99cd9d7c8`：3 passed /258.09s。
- 09-04 18:22:55，`exec-1f2da57f-2243-4dad-94f0-9ab664546637`：3 failed /3.20s。
- 09-04 18:31:57，`exec-02a05fde-de30-4702-bd29-a49fa41303e7`：3 passed /171.61s。
- 09-04 19:04:07，`exec-637255b2-2224-4feb-b244-687ec49bb47a`：3 passed /133.41s。
- 09-05 01:02:47，`exec-c78963f8-dd31-4a07-873f-009575d45813`：3 passed /287.23s。
- 09-05 01:35:29，`exec-63268cb8-cf41-462f-ab92-ba2b910f99d9`：3 failed /575.70s。
- 09-05 01:43:54，`exec-50d04a8b-caef-4773-8f18-efcdc06d01a3`：exit1，仅F，unresolved。
- 09-05 01:48:30，`exec-fd9b3617-b903-4482-939d-bfb4468aa3d9`：exit1，仅F，unresolved。
- 09-05 01:59:59，`exec-9e9cd85d-9939-46b0-a618-c5ff6b3d949a`：3 failed /640.12s。

更早窗口合计30个有结束数量的测试结果（15通过、15失败），另2条仅F的未解析输出。该附录未逐条恢复业务身份和主失败阶段，属于更早版本evidence索引，不称为本轮20条resolved attempt。全生命周期精确total仍不能从这些缺口推定。

## 门禁与正式审计基线

- worker 容器内布尔：DeepSeek models=true/chat=true，BGE embedding=true/reranker=true，PostgreSQL=true，Redis=true，Order=true/Payment=true/Email=true；未输出密钥、token 或响应正文。
- Ruff check 全范围通过；format 全范围 215 formatted、仅既有已批准 `0019_run_job_claims.py` 格式债不通过；排除此文件后 215 文件通过。本轮文件格式均通过，没有顺带修改0019。
- Mypy：117 source files 通过。主库 Alembic：`0023_evaluation_dataset_identity (head)`；RC 从0001→head通过。
- frontend unit=19，Playwright=8（登录、Task14、Task15及evaluation UI mock回归），typecheck/build通过。构建仍有既有大bundle提示。
- 完整 scratch backend：`528 passed, 4 skipped in 1371.29s`，`scratch_full_exit=0`；新建独立数据库0001→0023后执行完整pytest，既有安全前缀守卫的context manager结束后删除精确scratch库。原始命令`exec-8c9ade50-cc1d-446a-b883-1477d5deead0`于2026-09-05T16:59:04Z退出0；会话切换后从原始item_completed恢复结果，没有重复启动。随后查询scratch前缀数据库列表为空。
- 主库与RC `evaluation_test_executions=0`；Task17正式Run仍 `COMPLETED|60`。
- Task17 results SHA：`d9f1c5a26abbce5ee0e521146a39864c0fcbdc517533c7869d7aca84e843c220`。
- 冻结test SHA：`e0a5eb5a474eeaeeeeeb6a0efbed741e0d422f18fc48af0099ec0ec987ffbf8c`，manifest `final_test_executed=false`。督导明确没有另一个已登记 sentinel，以这些正式事实为基线。
- RC 已以精确 project `down --volumes` 清理；按 project label 查询容器/卷/network 均为空。清理后主拓扑九服务 healthy、Redis PONG、主库 receipt=0、正式矩阵COMPLETED|60。主服务始终使用接手时镜像，没有用新RC镜像替换；本轮真实修复验收属于上述隔离镜像。
- 本报告随最小修复提交，提交信息为`fix: enforce compose agent workflow completion`；准确SHA由最终督导消息与Git提交记录提供（报告不自引用未产生的SHA）。Runner生产文件零diff；提交只包含Payment、Compose、测试与交接文档。既有未跟踪用户文件、`.idea/`、`.workbuddy/`、安装包及旧ACL pytest目录未修改、未清理。

下一步仅提交本轮最小修复后等待督导 Task18 步骤1–5 go/no-go；`attempt=0` 技术债保持原状。
