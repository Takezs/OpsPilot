# 本地启动指南

## 前置条件

Docker Engine/Desktop 与 Compose v2；可用 DeepSeek API（可能产生费用）；已加载 BGE-M3 embedding 与 reranker 的兼容服务。BGE-M3 应输出 1024 维。开发测试另需 Python 3.12、Node.js 与 npm。

## 配置

复制根目录 `.env.example` 为 `.env`，仅本机填写。重点设置 `POSTGRES_PASSWORD`、`JWT_SECRET`（自行生成足够长随机值）、`DEEPSEEK_API_KEY`、`BGE_API_KEY`、`BGE_BASE_URL`、`BGE_EMBEDDING_MODEL`、`BGE_RERANKER_MODEL`。开发 JWT 示例值不能用于 production。

Docker Desktop 下宿主模型地址常用 `http://host.docker.internal:8080/v1`。模型字段改为实际 UID，如 `bge-m3`、`bge-reranker-v2-m3`；不要混淆宿主与容器 localhost。Linux 需自行保证该宿主地址可解析。需要代理时设置容器可访问的 `DEEPSEEK_PROXY_URL`，否则留空。

## 初始化新开发环境

已有数据库先备份并核对迁移版本。不要执行 `down -v`。

```bash
docker compose up -d postgres redis
docker compose build
docker compose run --rm --no-deps api alembic -c alembic.ini upgrade head
```

当前没有公共注册接口、默认账号或一键初始化向导。维护者须显式创建 User（角色、allowed_departments/max_access_level）及匹配范围的 KnowledgeBase；密码使用 `AuthService.hash_password` 生成哈希，不得存明文。模型见 `backend/src/opspilot/auth/models.py` 与 `knowledge/models.py`。测试 fixture 不应直接当生产账号初始化器。这是首次部署的人工步骤，不宣称开箱即用。

## 启动应用

```bash
docker compose up -d
docker compose ps
```

Web：<http://127.0.0.1:8088>；API 文档：<http://127.0.0.1:8000/docs>。用自己初始化的账号登录，上传演示文档，等待 READY 后再检索。

BGE 容器运行不代表模型已加载。model not found 时在模型管理端加载准确 UID，不用 Fake Provider 掩盖环境错误。

## 开发验证与安全

后端依赖见 `backend/pyproject.toml`。前端 `npm ci` 后可运行 `npm run test`、`npm run typecheck`、`npm run build`；浏览器测试需 Playwright Chromium。

后端集成测试必须使用独立 PostgreSQL/Redis 环境；迁移测试会创建临时库或降级。不要把完整 pytest 指向重要数据库。冻结评测有独立授权约束，本指南不启动它。

密钥、数据卷、模型缓存不属于 GitHub 源码交付。默认回环端口与演示服务不构成公网生产部署方案。
