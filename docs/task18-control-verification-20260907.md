# Task18 控制面鉴权与并发隔离复审证据（2026-09-07）

范围仅为步骤1–5中督导对 `0716a789284763124291938c4d096cadfa64cef0` 提出的两个P1。步骤6/7、冻结test、最终执行收据、tag及push禁止。历史记录见 [上一轮报告](task18-compose-verification-20260906.md)，本报告不覆盖旧失败证据。

## Findings 与修复

- 旧控制面只检查demo模式，未验证调用者；共享ORD-002的reset会使两个测试进程互相清除退款事实。现在reset/count/fault均要求显式模式、32字节独立随机控制凭据及合法合成订单。缺失、错误、非hex/长度异常签名一律404同正文。
- HMAC-SHA256绑定 `opspilot-demo-control:v1`、HTTP method、action及完整order；常量时间比较。count签名不能reset，其他订单签名不能重置或注入故障，普通ORD订单不属于控制面命名空间。
- 控制凭据在镜像外生成，经匿名stdin传给无网络临时初始化容器和测试子进程。仅Payment只读挂载临时卷，文件UID10001、0400；不放入Config.Env、镜像层、URL、命令参数或响应。JWT/Provider凭据与控制凭据相互独立。
- 每次使用独立 `E2E-<32 lowercase hex>` 订单。固定PostgreSQL管理数据库advisory lock在同一连接覆盖reset、完整流程、count和cleanup；有界等待，连接丢失取消测试。两个真实HTTP子进程证明锁区间不重叠、各订单独立超时及退款次数1。没有使用全局pytest串行化替代该锁。
- Trace原先只净化属性值，丢失敏感键语义；现在保留键上下文脱敏，并覆盖实际SpanExporter结果。普通日志控制头也纳入脱敏。
- 不恢复工具名词法强制，不调整模型Prompt/意图协议。业务提示仅将固定订单替换为本次唯一订单。Compose模式只使用Docker API/worker/publisher/demo；容器Provider路由为host.docker.internal，避免旧本地与Docker混合拓扑。

## Red → Green

- 鉴权/命名空间最初8 failed、2 passed；初步Green10 passed；锁模块缺失Red1 failed，随后demo/锁22 passed。
- 日志/trace canary定向Red1 failed、15 deselected；保留属性键语义并补充日志凭据键后81 passed。实际SpanExporter最终4 passed，无master/HMAC、Bearer、邮箱或电话canary。
- 模式关闭后已注册合成订单仍可访问：Red1 failed、16 deselected后修复。
- 首次scratch全量2 failed、546 passed、4 skipped（90.43s）：两个旧可靠性测试仍注入未鉴权故障头。适配独立订单及签名；第一次适配reset路径漏掉`/__e2e`而2 failed、27 deselected，纠正后2 passed、27 deselected。该次定向遗留两条Run已按精确Run/Operation UUID及时间条件删除（DELETE 2），不涉及评测记录。
- 故障头拒绝正文一致性Red2 failed、17 deselected；将鉴权放在合成订单检查之前后19 passed。
- 真实锁连接被关闭时取消执行、随后可重新获锁：2 passed（包含跨进程有界等待验证）。

## 本轮 Compose 历史（全部保留）

- `opspilot_task18_rc_20260906_auth01`：初始化secret卷时Compose run继承只读挂载，写文件失败。0次业务attempt；改用无网络临时docker run初始化卷。失败项目已完整清理。
- `opspilot_task18_rc_20260907_auth02`：两个独立HTTP进程并发验证1 passed（2.33s），PID16292/2936，两单各count1/timeout/cross-order rejected。单轮1 passed（42.18s），随后3 passed（213.56s）；全部4次精确引用、PG/public seq连续、对账成功、count1，seq_count均13。
  - 单轮 attempt `e4cdd10d76e24c8c93197e0db06426e1`，Run `1401102a-9125-4fe7-bde9-83dcc6bdc350`，order `E2E-0fdd91eb17944b978c7bbd20fc874b14`。
  - 三轮0 attempt `18b3f8ae7b0f4eeebcef888f4604cfc1`，Run `1d320d42-788f-48e7-9587-6bcb9a537222`，order `E2E-5bdf5c37b03b4fb4afc3b0f2612d7aa0`。
  - 三轮1 attempt `43c6028dbaf84c168ad8a44f4987761a`，Run `80be532b-898b-4159-9549-563750fec0c4`，order `E2E-11bfeee8361f4cc59dd14dcb7808881a`。
  - 三轮2 attempt `cdf448fd32ba4f2b8c7ac1a7f8c57159`，Run `362cb124-28e5-49d3-b4fd-0bbb7a88c75b`，order `E2E-fc068a43bee24ccfbeb4d08ba59aada9`。
- auth02通过后发现并修复拒绝正文一致性，故使用最终代码重建auth03进行验收，不是失败后靠重复运行挑选成功。
- auth03最终代码：并发1 passed（2.37s），单轮1 passed（156.06s），连续3 passed（153.43s）；三轮订单分别为 `E2E-b9fbd082c0bc42cf9d85ab775cf1d2f9`、`E2E-a9af05dc7a934c1cacfc0bb8fbc4aa45`、`E2E-566e26533ba1428fa7fe8ed14407b18d`，每轮seq_count 13、引用/seq/reconciliation/count均成立。

## 最终门禁

- 独立scratch从0001→0023 head：551 passed、4 skipped（89.73s），精确scratch数据库已删除。中间版本548 passed、4 skipped（86.53s）不代替最终结果。
- Ruff check通过；src/tests/demo format共198文件通过；既有0019迁移格式债保持不动。Mypy118 source files通过。
- Frontend unit19 passed；登录/Task14/Task15及evaluation UI Playwright8 passed（3.7s）；typecheck/build通过。build仅既有chunk size提示。
- auth03 worker内部：DeepSeek models/chat、BGE embedding/reranker、PostgreSQL、Redis、order/payment/email HTTP九项均true。仅输出布尔及非敏感配置，未打印响应正文或凭据。
- 配置：DeepSeek `https://api.deepseek.com` / `deepseek-chat`，proxy `http://host.docker.internal:7897`；BGE `http://host.docker.internal:8080/v1`，`bge-m3` / `bge-reranker-v2-m3`。
- 主审计复核：receipt=0，正式矩阵`0108b7b2-991d-49d3-9e6e-d2ff50ce5c38`为COMPLETED/60；results SHA `d9f1c5a26abbce5ee0e521146a39864c0fcbdc517533c7869d7aca84e843c220`。
- manifest `final_test_executed=false`，冻结SHA `e0a5eb5a474eeaeeeeeb6a0efbed741e0d422f18fc48af0099ec0ec987ffbf8c`。未运行正式冻结评测或Task17矩阵；未创建最终receipt，步骤6/7/tag/push未开始。

## 证据限制与交接

上一轮历史已解析20次，另有仅FF的未解析命令，不能宣称这是全部总数。旧最初六次失败PG行被旧finally删除，只能由输出/worker日志证明。本轮LIVE_EVIDENCE在清理前验证PG/history，测试finally及RC删除后同样不宣称PG行继续存在。独立真实HTTP控制探针不是Provider业务attempt。

当前会话可用官方工具中未暴露send_message_to_thread（已检索工具清单）；阶段消息无法送达督导，不能宣称已发送。完整报告在此保留，完成提交后停止，等待官方消息能力恢复及督导go/no-go。
