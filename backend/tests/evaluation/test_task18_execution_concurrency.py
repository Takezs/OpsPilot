import asyncio
import uuid

import pytest
from arq.connections import RedisSettings, create_pool
from arq.worker import Worker
from sqlalchemy import select

from opspilot.auth.models import Role
from opspilot.auth.schemas import Principal
from opspilot.db import async_session_factory
from opspilot.evaluation import tasks
from opspilot.evaluation.models import EvaluationCaseRecord
from opspilot.evaluation.schemas import AgentEvaluationCase, FrozenDatasetSnapshot
from opspilot.evaluation.service import cancel_test_execution, resume_test_execution
from opspilot.knowledge.schemas import AccessLevel
from tests.evaluation.test_task17_worker_fencing import _case, _cleanup, _create_execution


@pytest.mark.integration
async def test_snapshot_concurrency_runs_three_cases_and_duplicate_delivery_is_inert(monkeypatch):
    execution_id, run_id, user_id, identity = await _create_execution(concurrency=3)
    snapshot = FrozenDatasetSnapshot(identity, tuple(_case(f"case-{i}") for i in range(7)), ())
    monkeypatch.setattr(tasks, "capture_frozen_dataset", lambda _root: snapshot)
    active = peak = calls = 0
    three_started = asyncio.Event()
    release = asyncio.Event()

    async def processor(case, repetition):
        nonlocal active, peak, calls
        active += 1
        calls += 1
        peak = max(peak, active)
        if active == 3:
            three_started.set()
        try:
            await release.wait()
            return tasks.EvaluationCaseResult({"case": case.case_id}, {})
        finally:
            active -= 1

    context = {"job_id": "concurrency", "evaluation_case_processor": processor}
    task = asyncio.create_task(tasks.process_evaluation_execution(context, str(execution_id)))
    try:
        await asyncio.wait_for(three_started.wait(), 3)
        await tasks.process_evaluation_execution(
            context | {"job_id": "duplicate"}, str(execution_id)
        )
        assert calls == 3
        release.set()
        await asyncio.wait_for(task, 10)
        assert peak == 3
        assert calls == 7
        async with async_session_factory() as session:
            rows = list(
                await session.scalars(
                    select(EvaluationCaseRecord).where(
                        EvaluationCaseRecord.evaluation_run_id == run_id
                    )
                )
            )
        assert len(rows) == 7
        assert len({(row.dataset_case_id, row.repetition) for row in rows}) == 7
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await _cleanup(execution_id, run_id, user_id)


@pytest.mark.integration
async def test_real_redis_duplicate_jobs_share_one_postgres_claim(monkeypatch):
    execution_id, run_id, user_id, identity = await _create_execution(concurrency=3)
    snapshot = FrozenDatasetSnapshot(identity, tuple(_case(f"redis-{i}") for i in range(6)), ())
    monkeypatch.setattr(tasks, "capture_frozen_dataset", lambda _root: snapshot)
    redis = await create_pool(RedisSettings(host="127.0.0.1", port=6379))
    queue = "task18-synthetic-" + uuid.uuid4().hex
    job_ids = [queue + "-1", queue + "-2"]
    calls = []
    release = asyncio.Event()
    three = asyncio.Event()

    async def processor(case, repetition):
        calls.append(case.case_id)
        if len(calls) == 3:
            three.set()
        await release.wait()
        return tasks.EvaluationCaseResult({}, {})

    worker = Worker(
        [tasks.process_evaluation_execution],
        ctx={"evaluation_case_processor": processor},
        redis_pool=redis,
        queue_name=queue,
        burst=True,
        handle_signals=False,
        poll_delay=0.01,
        max_jobs=3,
    )
    work = None
    try:
        for job_id in job_ids:
            await redis.enqueue_job(
                "process_evaluation_execution", str(execution_id), _queue_name=queue, _job_id=job_id
            )
        work = asyncio.create_task(worker.async_run())
        await asyncio.wait_for(three.wait(), 5)
        release.set()
        await asyncio.wait_for(work, 10)
        assert len(calls) == len(set(calls)) == 6
        assert worker.jobs_complete == 2
        assert worker.jobs_failed == 0
    finally:
        release.set()
        if work is not None and not work.done():
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
        await redis.delete(
            queue,
            queue + ":health-check",
            *("arq:job:" + value for value in job_ids),
            *("arq:result:" + value for value in job_ids),
        )
        # The test owns the supplied pool and async_run has finished. ARQ's
        # signal-based close path references SIGUSR1, unavailable on Windows.
        await redis.aclose()
        await _cleanup(execution_id, run_id, user_id)


def principal(user_id):
    return Principal(
        user_id=str(user_id),
        role=Role.ADMIN,
        allowed_departments=frozenset(),
        max_access_level=AccessLevel.PUBLIC,
    )


@pytest.mark.integration
async def test_cancellation_stops_all_three_tasks_before_next_case(monkeypatch):
    monkeypatch.setenv("EVALUATION_LEASE_SECONDS", "1")
    execution_id, run_id, user_id, identity = await _create_execution(concurrency=3)
    snapshot = FrozenDatasetSnapshot(identity, tuple(_case(f"cancel-{i}") for i in range(6)), ())
    monkeypatch.setattr(tasks, "capture_frozen_dataset", lambda _root: snapshot)
    calls = 0
    active = 0
    three = asyncio.Event()

    async def processor(case, repetition):
        nonlocal calls, active
        calls += 1
        active += 1
        if active == 3:
            three.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    work = asyncio.create_task(
        tasks.process_evaluation_execution(
            {"job_id": "cancel", "evaluation_case_processor": processor}, str(execution_id)
        )
    )
    try:
        await asyncio.wait_for(three.wait(), 5)
        async with async_session_factory() as session:
            await cancel_test_execution(session, principal(user_id), execution_id)
            await session.commit()
        with pytest.raises(tasks.EvaluationLeaseLost):
            await asyncio.wait_for(work, 5)
        assert calls == 3
        assert active == 0
    finally:
        if not work.done():
            work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        await _cleanup(execution_id, run_id, user_id)


@pytest.mark.integration
async def test_same_execution_resume_only_fills_missing_repetitions(monkeypatch):
    execution_id, run_id, user_id, identity = await _create_execution(concurrency=3)
    agent = AgentEvaluationCase.model_validate(
        _case("agent-fixture").model_dump()
        | {
            "operation_expected": False,
        }
    )
    snapshot = FrozenDatasetSnapshot(identity, (), (agent,))
    monkeypatch.setattr(tasks, "capture_frozen_dataset", lambda _root: snapshot)
    calls = []
    fail = True

    async def processor(case, repetition):
        nonlocal fail
        calls.append(repetition)
        if repetition == 2 and fail:
            fail = False
            raise RuntimeError("synthetic worker interruption")
        return tasks.EvaluationCaseResult({"repetition": repetition}, {})

    context = {"job_id": "resume", "evaluation_case_processor": processor}
    try:
        with pytest.raises(RuntimeError, match="synthetic"):
            await tasks.process_evaluation_execution(context, str(execution_id))
        async with async_session_factory() as session:
            await resume_test_execution(session, principal(user_id), execution_id)
            await session.commit()
        await tasks.process_evaluation_execution(context, str(execution_id))
        assert calls == [1, 2, 2, 3]
        async with async_session_factory() as session:
            rows = list(
                await session.scalars(
                    select(EvaluationCaseRecord).where(
                        EvaluationCaseRecord.evaluation_run_id == run_id
                    )
                )
            )
        assert sorted(row.repetition for row in rows) == [1, 2, 3]
    finally:
        await _cleanup(execution_id, run_id, user_id)
