# OpsPilot 架构

OpsPilot 是模块化单体：FastAPI 负责授权 API，ARQ Worker 执行索引、Agent 与可靠 Operation，独立 Publisher 扫描 PostgreSQL transactional outbox。PostgreSQL 是业务、Journal、Attempt、Evaluation 的唯一事实源；Redis 仅承载 ID 通知与队列。

生产拓扑使用非 root API、Worker、Publisher 和 Web 容器。API/Worker 共享最小文档卷；Publisher 与 Web 无文档卷。密钥仅由运行环境注入，不进入镜像。Trace 失败不参与业务事务，Trace 属性复用 Journal 的脱敏与限长规则。

发布事实和指标只引用正式报告 [`results.json`](../evaluation/reports/0108b7b2-991d-49d3-9e6e-d2ff50ce5c38/results.json)。
