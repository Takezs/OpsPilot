# 界面与演示导览

截图拍摄于 2026-09-10，来自真实本机 Web/API、PostgreSQL 与 BGE 服务。内容为已有演示退款政策，不是客户业务数据；未替换 API 响应。

## 知识库

![文档版本与入库状态](images/knowledge.png)

账号只能看到授权范围内的知识库。上传后异步入库，页面轮询至 READY 或 FAILED；错误展示安全摘要。

## 检索调试

![四阶段检索](images/retrieval.png)

查询 `refund approval policy`，比较四阶段排序。截图为视口范围，下方可滚动。不同语料和模型配置会产生不同结果；FTS 也可能为空，不会凭空补齐候选。

点击卡片可打开精确版本引用抽屉。Agent 回答中的引用同样必须绑定实际文档、版本和 Chunk。

## 退款流程怎么演示？

先准备演示订单与 Reviewer 账号，再从工作台提交请求。审批通过只表示允许执行，不代表退款成功。发送后超时时，观察 OUTCOME_UNKNOWN / RECONCILING，再以核对结果判断成功。

故障控制仅用于显式隔离 E2E，不对真实业务服务使用。普通演示不启动冻结评测，也不删除旧 Run 或收据。

## 重拍截图

安装 frontend 依赖，启动本机服务后，从根目录运行：

```powershell
$env:OPSPILOT_SCREENSHOT_CREDENTIALS = '你的外部登录凭据文件绝对路径'
node frontend/capture-showcase.mjs
```

文件包含已有账号的 username/password，仅在进程内读取。脚本不导出 token 或浏览器存储，只读取知识库与检索页面。提交图片前须人工检查客户数据或敏感政策；不得提交凭据文件。
