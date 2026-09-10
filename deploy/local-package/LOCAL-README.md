# OpsPilot 本机发布包

打开地址：http://127.0.0.1:8088 。首次或电脑重启后，启动 Docker Desktop，再双击 `START.cmd`。启动完成会打开浏览器；登录使用现有 OpsPilot 账号。

本包面向当前已配置的 Windows 电脑，复用 `opspilot` Compose 项目及现有数据卷。不会删除、初始化或恢复数据库，不会创建评测收据或执行冻结测试。它是本机可用版本，不宣称 v1.0 冻结评测验收通过。

## 前置条件

- Docker Desktop 正常运行，现有 `opspilot-bge` 服务可用；`bge-m3` 和 `bge-reranker-v2-m3` 两个模型已经加载。
- 现有外部凭据目录 `E:\OpsPilot-release-20260908` 中的 `runtime.json`、`postgres.secret`、`user.json`、`reviewer.json` 保持可用。包内没有这些密钥文件，请勿把它们放进发布包或发送给别人。
- 现有 PostgreSQL 已为 `0025_eval_msg_corr`。本启动器不运行数据库迁移，不能作为全新空数据库安装器。
- 默认沿用 DeepSeek 代理 `http://host.docker.internal:7897` 和 BGE `http://host.docker.internal:8080/v1`。如代理地址变化，可运行 `Start-OpsPilot.ps1 -DeepSeekProxy <地址>`；直连时传空字符串。

## 内容与检查

- `images.tar`：当前已验证的应用和依赖镜像，不含数据库卷、BGE 模型缓存或密钥。
- `release-manifest.json`：应用提交、准确镜像 ID、镜像包 SHA-256 和验证信息。
- `source.zip`：对应提交的源码快照。
- `Start-OpsPilot.ps1 -CheckOnly`：只校验配置、外部文件路径及镜像存在性。

镜像缺失时，启动器先核验 `images.tar` 的 SHA-256，再导入镜像。现有镜像仍在时直接使用准确 ID。更新只使用包内镜像，不自动下载新的应用版本。

## 已验证和限制

后端隔离测试 589 passed / 4 skipped；前端 19 单元测试、8 浏览器回归；真实 BGE + DeepSeek + 公开 Run/message 链精确引用通过。旧冻结评测的 receipt 和 320 行结果保留。

此版本修复了缺失评测知识前置、协调查询被误计为重复退款、回答业务状态被误判的问题。后续正式评测仍须先准备独立版本化来源知识与订单场景。

如启动失败，先确认 Docker Desktop、Clash 代理和 BGE 两个模型可用。不要删除容器数据卷，不要执行 `docker compose down -v`。现有 BGE 容器重启后若模型未加载，需在管理界面重新启动模型。
