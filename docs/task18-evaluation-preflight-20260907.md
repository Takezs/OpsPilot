# Task18 evaluation deployment preflight, 2026-09-07

Scope: repair the preconditions for Task18 step 6 after its one-time GO was withdrawn. This is not a frozen evaluation execution or release authorization. Baseline commit: ed451fe766afb29b17efd54ef3153a50621f7dc2. Final verification completed on 2026-09-08; this report accompanies the independent candidate commit `fix: bind evaluation runtime to frozen configuration`.

## Findings and changes

- The ordinary backend image excludes the frozen artifact payload. A dedicated non-root, read-only evaluation runtime now embeds and verifies manifest/test/agent_tasks/generator identities. Deployment uses immutable image IDs; the synthetic fixture entrypoint exists only in a separate test image.
- The evaluation API URL and credentials were not deployed as a complete role-specific runtime. The overlay supplies the explicit Compose API URL and read-only runtime/USER/REVIEWER credential files. ADMIN credentials remain in the isolated launcher. Authentication verifies the actual `/auth/me` role and refreshes short-lived tokens between cases.
- Recorded configuration did not control the executing models, temperature, retrieval top_k or concurrency. The deployed canonical configuration is checked against the receipt and API composition before a claim. Provider constructors receive its models/temperature, retrieval receives top_k, and the executor runs a bounded case-level pool. Repetitions remain sequential within each case.
- Existing PostgreSQL fencing, heartbeat, persisted-result checks and cancellation remain in force. Synthetic PG tests cover concurrent duplicate invocation, real Redis duplicate delivery, cancellation and same-execution missing-repetition recovery.

Approved configuration SHA: `ce62b4409c5ac4e5e1d3be3b5577e59b123c0d06fd17f590830453a492c43693`. Models: deepseek-v4-flash, bge-m3, bge-reranker-v2-m3; temperature 0; top_k 5; prompt task18-rc1; repetitions 3; concurrency 3.

## Red to Green evidence

- Concurrent dispatch: 1 failed when the old serial executor could not reach three active cases; initial Green plus fencing 6 passed. Latest duplicate/cancel/resume suite: 4 passed in 7.31s, including actual Redis delivery. Its first Redis cleanup failed on Windows-only missing SIGUSR1 after functional assertions passed; the test now closes its own supplied Redis pool after async_run completes.
- Hash-only identity: corrected synthetic fixture Red 1 failed because the old preflight deserialized opaque case bytes; Green 1 passed. The earlier malformed manifest fixture failure was not accepted as a valid Red.
- Runtime contract: 4 failed before module implementation, then 4 passed.
- Role credential files: 3 failed before module implementation, then 3 passed. Runtime secrets file loading: 1 failed before loading was implemented, then credential suite 4 passed.
- Static corpus/schema/identity tests: 10 passed offline with socket connection audit. Supervisor explicitly permits these static reads/parses; they do not invoke the evaluation processor, providers, public Run API, scoring or receipt writes. Frozen artifacts and manifest remain immutable.

## Isolated Compose evidence

Greeting-only fixture `opspilot_task18_eval_20260907_01`: COMPLETED, six persisted results (three regular cases and three Agent repetitions), observed concurrency 3, no observation of 4. Repeated report exports are identical, SHA `b286b3477deeae75140945e79dbe009b79fbcd1357d101c3c31a1c89eab37fd0`. Real startup preflight passed for DeepSeek models/chat, BGE embedding/reranker, PG, Redis and API. All Compose services healthy. Image environment/history and log credential canaries absent; role file mounts read-only. Isolated resources were removed after verification.

This greeting fixture proves scheduling, configuration and credentials only. It does not prove refund side-effect safety, citation correctness or reconciliation.

Separate refund fixture `opspilot_task18_eval_20260907_refund01`: COMPLETED, six unique persisted case/repetition results after duplicate delivery through real Redis, observed peak 3. Three ordinary cases plus three refund repetitions, each with its own derived E2E order namespace. The test-only schema admits E2E orders without changing the frozen schema. USER creates Runs and REVIEWER approves through public APIs. Only Payment receives the temporary Linux control volume, read-only, with file owner 10001 and mode 0400 set by the launcher; fault controls are enabled only in the isolated test overlay. All three initial refunds time out after effect, then reconcile successfully. No system/business prompt was changed and no Operation was created directly in PostgreSQL.

- Repetition 1: Run `114ca6f9-4a09-4269-9fd8-133b92a80be0`, order `E2E-c51e9e919a2262f5f7d0cbd9a380c531`.
- Repetition 2: Run `84d58fea-ada4-4d78-b67f-51b1d2b2a0e3`, order `E2E-47ba97cdfb42724e6ecd3564ccf4e94f`.
- Repetition 3: Run `09e6e5d7-11e4-4c33-98e3-d20771a93f60`, order `E2E-ef65851331f54dbc214166fa281381aa`.

Each has public/PG seq 1..11, exactly equal; `operation_outcome_unknown`, `operation_reconciliation_started`, `operation_reconciled_succeeded` present; final SUCCEEDED and Payment count=1. The three controls reset distinct orders before execution, never between repetitions. Report repeated-export SHA: `ae1b325637db7ef75b430c5ef1b3a29115011f24fad23855aa6f927b5b1f522f`. Log/image environment/history canaries absent; all isolated services healthy, then exact isolated volumes/containers/network removed. These are non-frozen acceptance results, not formal test scores. Their case tool expectations are deliberately not used to claim task-success metrics.

This preflight round therefore contains two isolated real-Provider acceptance launches: one greeting run and one refund run, both completed; the latter includes three complete refund chains. These counts are separate from prior Task18 live-provider attempts. Non-empty citations were validated by the prior auth03 live tests, not by these new synthetic cases, whose expected citations are empty.

Runtime image used by refund01: `sha256:852949c778e38a3cbc2667482ba1bdec19ef1a6eb28274aa2f5627e2413b8759`. Fixture image: `sha256:e53d6eecfae3700cf6badaee6064705026254d7a961ebf2ed6b03279eb2e2430`. Fixture formatting was subsequently normalized without changing behavior. Runtime payload also passed a standalone no-network/read-only hash verification.

## Gates and boundaries

- Preliminary full scratch backend: 560 passed, 4 skipped (before additional concurrency/secret tests). Final scratch: **564 passed, 4 skipped in 1388.02s**, migrated 0001 through 0023; exact database `opspilot_task18_gate_41e932cd0d2a` safely dropped after completion. Final focused Runner/redaction/runtime/credentials/identity/public adapter: **102 passed in 9.41s**; separate PG/Redis concurrency/cancel/resume: **4 passed in 7.31s**.
- Frontend: 19 unit tests and 8 Playwright tests passed; typecheck/build passed. Existing large bundle warning remains.
- Mypy: 122 source files passed. Ruff src/tests/demo-services check passed; format 208 files passed. The unrelated historical 0019 migration format debt remains outside this selected format scope.
- Main Compose: all nine services healthy; main audit baseline remains receipt=0, evaluation_cases=77, evaluation_execution_attempts=0.
- Frozen test has only been statically read/parsed for integrity, never executed or scored by the Runner. No formal receipt, step 7, tag, push or deployment has been started.
- Historical Task18 live evidence remains in the prior Compose/control reports: 20 resolved historical attempts plus unresolved FF evidence. The initial six failed runs were deleted by the old test finally block; no surviving PostgreSQL rows are claimed.

Final audit on 2026-09-08: all nine main services healthy, Redis PONG, Alembic0023 head, immutable artifact verification true, manifest final_test_executed=false. Main receipt/case/attempt counts 0/77/0. Task17 matrix COMPLETED|60 and report SHA `d9f1c5a26abbce5ee0e521146a39864c0fcbdc517533c7869d7aca84e843c220` unchanged. No remaining refund01 containers or volumes. Existing untracked user files, .idea, .workbuddy, installers and ACL pytest directories were not edited or cleaned. No unrelated tracked changes are included; attempt=0 lifecycle debt is unchanged.

Disposition: submit this candidate for supervisor Bugbot and go/no-go, then stop. Do not start the frozen test without a new explicit GO. No release/tag/push is authorized by these gate results.
