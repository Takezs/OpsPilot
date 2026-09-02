import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, text, update

from opspilot.auth.models import Role, User
from opspilot.db import async_session_factory
from opspilot.evaluation.models import (
    EvaluationCaseRecord,
    EvaluationExecutionAttempt,
    EvaluationExecutionStatus,
    EvaluationRun,
    EvaluationRunStatus,
    EvaluationTestExecution,
)
from opspilot.evaluation.schemas import (
    EvaluationCase,
    EvaluationConfiguration,
    ExpectedOutcome,
    FrozenDatasetSnapshot,
)
from opspilot.evaluation.tasks import (
    EvaluationCaseResult,
    EvaluationLeaseLost,
    drain_detached_evaluation_tasks,
    process_evaluation_execution,
)


def _case(case_id: str) -> EvaluationCase:
    return EvaluationCase(
        schema_version="1.0.0",
        case_id=case_id,
        category="fencing",
        query=f"question {case_id}",
        relevant_chunk_ids=(),
        expected_citation_ids=(),
        required_facts=(),
        forbidden_facts=(),
        expected_tools=(),
        forbidden_tools=(),
        follow_up_required=False,
        approval_required=False,
        expected_final_state=ExpectedOutcome.ANSWERED,
    )


async def _create_execution() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, dict[str, str]]:
    user_id, run_id, execution_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    identity = {"snapshot": uuid.uuid4().hex}
    configuration = EvaluationConfiguration(
        model="model",
        embedding_model="embedding",
        reranker_model="reranker",
        top_k=5,
        prompt_version="fence-v1",
        random_parameters={"temperature": 0.0},
        concurrency=1,
        repetitions=3,
    )
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        session.add(
            User(
                id=user_id,
                username=f"eval-fence-{user_id}",
                password_hash="unused",
                role=Role.ADMIN,
                allowed_departments=[],
                max_access_level=1,
            )
        )
        session.add(
            EvaluationRun(
                id=run_id,
                dataset_version="fence-fixture",
                dataset_sha256=uuid.uuid4().hex * 2,
                status=EvaluationRunStatus.RUNNING,
                model="model",
                embedding_model="embedding",
                reranker_model="reranker",
                top_k=5,
                prompt_version="fence-v1",
                random_parameters={},
                configuration=configuration.model_dump(mode="json"),
                started_at=now,
            )
        )
        session.add(
            EvaluationTestExecution(
                id=execution_id,
                evaluation_run_id=run_id,
                dataset_version="fence-fixture",
                dataset_sha=uuid.uuid4().hex * 2,
                configuration=configuration.model_dump(mode="json"),
                dataset_identity=identity,
                configuration_sha=uuid.uuid4().hex * 2,
                status=EvaluationExecutionStatus.RUNNING,
                frozen_by_user_id=user_id,
                started_by_user_id=user_id,
                started_at=now,
            )
        )
        await session.flush()
        session.add(
            EvaluationExecutionAttempt(
                execution_id=execution_id,
                attempt_number=1,
                status="RUNNING",
                started_by_user_id=user_id,
                started_at=now,
            )
        )
        await session.commit()
    return execution_id, run_id, user_id, identity


async def _cleanup(execution_id: uuid.UUID, run_id: uuid.UUID, user_id: uuid.UUID) -> None:
    await drain_detached_evaluation_tasks()
    async with async_session_factory() as session:
        await session.execute(
            delete(EvaluationTestExecution).where(EvaluationTestExecution.id == execution_id)
        )
        await session.execute(delete(EvaluationRun).where(EvaluationRun.id == run_id))
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()


async def _steal(execution_id: uuid.UUID) -> None:
    async with async_session_factory() as session:
        await session.execute(
            update(EvaluationTestExecution)
            .where(EvaluationTestExecution.id == execution_id)
            .values(
                claim_token="new-token",
                lease_owner="new-owner",
                version=EvaluationTestExecution.version + 1,
                lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
            )
        )
        await session.commit()


@pytest.mark.integration
async def test_lost_between_cases_never_starts_next_processor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opspilot.evaluation import tasks

    execution_id, run_id, user_id, identity = await _create_execution()
    snapshot = FrozenDatasetSnapshot(identity, (_case("case-1"), _case("case-2")), ())
    calls: list[str] = []
    real_renew = tasks._renew
    renewals = 0

    async def renew_then_steal(*args: object) -> None:
        nonlocal renewals
        renewals += 1
        if renewals == 2:
            await _steal(execution_id)
        await real_renew(*args)  # type: ignore[arg-type]

    async def processor(case: EvaluationCase, _repetition: int) -> EvaluationCaseResult:
        calls.append(case.case_id)
        return EvaluationCaseResult({}, {"task_success": True})

    monkeypatch.setattr(tasks, "capture_frozen_dataset", lambda _root: snapshot)
    monkeypatch.setattr(tasks, "_renew", renew_then_steal)
    try:
        with pytest.raises(EvaluationLeaseLost):
            await process_evaluation_execution(
                {"job_id": "between", "evaluation_case_processor": processor}, str(execution_id)
            )
        assert calls == ["case-1"]
    finally:
        await _cleanup(execution_id, run_id, user_id)


@pytest.mark.integration
async def test_lost_after_skipped_case_never_starts_processor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opspilot.evaluation import tasks

    execution_id, run_id, user_id, identity = await _create_execution()
    snapshot = FrozenDatasetSnapshot(identity, (_case("case-1"), _case("case-2")), ())
    calls: list[str] = []
    real_renew = tasks._renew
    async with async_session_factory() as session:
        session.add(
            EvaluationCaseRecord(
                evaluation_run_id=run_id,
                dataset_case_id="case-1",
                repetition=1,
                actual_output={},
                deterministic_scores={},
                latency_ms=1,
            )
        )
        await session.commit()

    async def steal_before_invoke(*args: object) -> None:
        await _steal(execution_id)
        await real_renew(*args)  # type: ignore[arg-type]

    async def processor(case: EvaluationCase, _repetition: int) -> EvaluationCaseResult:
        calls.append(case.case_id)
        return EvaluationCaseResult({}, {})

    monkeypatch.setattr(tasks, "capture_frozen_dataset", lambda _root: snapshot)
    monkeypatch.setattr(tasks, "_renew", steal_before_invoke)
    try:
        with pytest.raises(EvaluationLeaseLost):
            await process_evaluation_execution(
                {"job_id": "skip", "evaluation_case_processor": processor}, str(execution_id)
            )
        assert calls == []
    finally:
        await _cleanup(execution_id, run_id, user_id)


@pytest.mark.integration
async def test_heartbeat_loss_cancels_blocked_processor_and_starts_no_next_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opspilot.evaluation import tasks

    monkeypatch.setenv("EVALUATION_LEASE_SECONDS", "1")
    execution_id, run_id, user_id, identity = await _create_execution()
    snapshot = FrozenDatasetSnapshot(identity, (_case("case-1"), _case("case-2")), ())
    calls: list[str] = []
    started, release = asyncio.Event(), asyncio.Event()

    async def processor(case: EvaluationCase, _repetition: int) -> EvaluationCaseResult:
        calls.append(case.case_id)
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return EvaluationCaseResult({}, {})

    monkeypatch.setattr(tasks, "capture_frozen_dataset", lambda _root: snapshot)
    work = asyncio.create_task(
        process_evaluation_execution(
            {"job_id": "blocked", "evaluation_case_processor": processor}, str(execution_id)
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        await _steal(execution_id)
        with pytest.raises(EvaluationLeaseLost):
            await asyncio.wait_for(work, timeout=3)
        assert calls == ["case-1"]
        async with async_session_factory() as session:
            count = await session.scalar(
                select(EvaluationCaseRecord.id).where(
                    EvaluationCaseRecord.evaluation_run_id == run_id
                )
            )
            assert count is None
    finally:
        release.set()
        await _cleanup(execution_id, run_id, user_id)


@pytest.mark.integration
async def test_input_failure_cannot_be_closed_by_worker_that_lost_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opspilot.evaluation import tasks

    execution_id, run_id, user_id, _ = await _create_execution()

    def fail_input(_root: object) -> FrozenDatasetSnapshot:
        raise ValueError("invalid frozen input")

    real_lock = tasks._lock_fenced_execution

    async def steal_before_terminal(*args: object) -> EvaluationTestExecution:
        await _steal(execution_id)
        return await real_lock(*args)  # type: ignore[arg-type]

    async def unused_processor(_case: EvaluationCase, _repetition: int) -> EvaluationCaseResult:
        raise AssertionError("processor must not run")

    monkeypatch.setattr(tasks, "capture_frozen_dataset", fail_input)
    monkeypatch.setattr(tasks, "_lock_fenced_execution", steal_before_terminal)
    try:
        with pytest.raises(ValueError, match="invalid frozen input"):
            await process_evaluation_execution(
                {"job_id": "lost-input", "evaluation_case_processor": unused_processor},
                str(execution_id),
            )
        async with async_session_factory() as session:
            execution = await session.get(EvaluationTestExecution, execution_id)
            assert execution is not None
            assert execution.status == EvaluationExecutionStatus.RUNNING
            assert execution.claim_token == "new-token"
    finally:
        await _cleanup(execution_id, run_id, user_id)


@pytest.mark.integration
async def test_terminal_failure_write_rolls_back_execution_run_and_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opspilot.evaluation import tasks

    execution_id, run_id, user_id, _ = await _create_execution()
    async with async_session_factory() as session:
        await session.execute(
            text(
                "CREATE FUNCTION reject_eval_run_terminal() RETURNS trigger LANGUAGE plpgsql "
                "AS $$ BEGIN RAISE EXCEPTION 'terminal write failure'; END $$"
            )
        )
        await session.execute(
            text(
                "CREATE TRIGGER reject_eval_run_terminal BEFORE UPDATE ON evaluation_runs "
                "FOR EACH ROW EXECUTE FUNCTION reject_eval_run_terminal()"
            )
        )
        await session.commit()

    def fail_input(_root: object) -> FrozenDatasetSnapshot:
        raise ValueError("invalid frozen input")

    async def unused_processor(_case: EvaluationCase, _repetition: int) -> EvaluationCaseResult:
        raise AssertionError("processor must not run")

    monkeypatch.setattr(tasks, "capture_frozen_dataset", fail_input)
    try:
        with pytest.raises(Exception, match="terminal write failure"):
            await process_evaluation_execution(
                {"job_id": "rollback-input", "evaluation_case_processor": unused_processor},
                str(execution_id),
            )
        async with async_session_factory() as session:
            execution = await session.get(EvaluationTestExecution, execution_id)
            run = await session.get(EvaluationRun, run_id)
            attempt = await session.scalar(
                select(EvaluationExecutionAttempt).where(
                    EvaluationExecutionAttempt.execution_id == execution_id
                )
            )
            assert execution is not None and execution.status == EvaluationExecutionStatus.RUNNING
            assert execution.claim_token is not None and execution.completed_at is None
            assert run is not None and run.status == EvaluationRunStatus.RUNNING
            assert attempt is not None and attempt.status == "RUNNING"
    finally:
        async with async_session_factory() as session:
            await session.execute(
                text("DROP TRIGGER IF EXISTS reject_eval_run_terminal ON evaluation_runs")
            )
            await session.execute(text("DROP FUNCTION IF EXISTS reject_eval_run_terminal()"))
            await session.commit()
        await _cleanup(execution_id, run_id, user_id)
