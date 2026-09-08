# Evaluation

Task16 数据集版本化并冻结；test SHA 为 `e0a5eb5a474eeaeeeeeb6a0efbed741e0d422f18fc48af0099ec0ec987ffbf8c`。2026-09-08已消耗唯一正式执行机会：receipt=1，execution `360b3d08-0f2a-4105-87f1-cd02ed8bc93b` 两次Attempt（仅一次resume）均等待超时，12/320结果，状态FAILED，等待督导裁决；不得再次freeze或盲目resume。详见[执行报告](task18-frozen-execution-20260908.md)。Task17 以 PostgreSQL receipt 固定 canonical configuration、dataset SHA、case/repetition 和执行状态，ARQ 仅投递 execution ID。

正式 dev 故障矩阵 Run 为 `0108b7b2-991d-49d3-9e6e-d2ff50ce5c38`。报告必须由持久化事实生成，不手改数字；JSON SHA 为 `d9f1c5a26abbce5ee0e521146a39864c0fcbdc517533c7869d7aca84e843c220`。冻结 test 只能在单独 go/no-go 后由 ADMIN 启动一次。
