import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from opspilot.auth.models import Role, User
from opspilot.auth.schemas import Principal
from opspilot.db import async_session_factory
from opspilot.evaluation.models import (
    EvaluationJobOutbox,
    EvaluationRun,
    EvaluationTestExecution,
)
from opspilot.evaluation.outbox import (
    recover_expired_evaluation_claims,
    recover_stale_evaluation_delivery,
)
from opspilot.evaluation.schemas import (
    DatasetManifest,
    EvaluationConfiguration,
    FreezeEvaluationRequest,
)
from opspilot.evaluation.service import (
    EvaluationConflictError,
    freeze_test_execution,
    start_test_execution,
)
from opspilot.evaluation.tasks import EvaluationLeaseLost, _lock_fenced_execution, _with_heartbeat
from opspilot.knowledge.schemas import AccessLevel


def _request(dataset_sha: str, *, top_k: int = 5) -> FreezeEvaluationRequest:
    return FreezeEvaluationRequest(
        dataset_version="task17-fixture",
        dataset_sha=dataset_sha,
        configuration=EvaluationConfiguration(
            model="deepseek-chat",
            embedding_model="bge-m3",
            reranker_model="bge-reranker-v2-m3",
            top_k=top_k,
            prompt_version="task17-v1",
            random_parameters={"temperature": 0.0},
            concurrency=3,
            repetitions=3,
        ),
    )


def _dataset_sha() -> str:
    manifest = DatasetManifest.model_validate_json(
        (Path(__file__).parents[3] / "evaluation" / "datasets" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    return manifest.test_sha256


@pytest.mark.integration
async def test_concurrent_freeze_and_start_each_have_one_winner() -> None:
    user_id = uuid.uuid4()
    dataset_sha = _dataset_sha()
    principal = Principal(
        user_id=str(user_id),
        role=Role.ADMIN,
        allowed_departments=frozenset(),
        max_access_level=AccessLevel.PUBLIC,
    )
    async with async_session_factory() as session:
        session.add(
            User(
                id=user_id,
                username=f"eval-admin-{user_id}",
                password_hash="unused",
                role=Role.ADMIN,
                allowed_departments=[],
                max_access_level=1,
            )
        )
        await session.commit()

    async def freeze(top_k: int) -> uuid.UUID | None:
        async with async_session_factory() as session:
            try:
                row = await freeze_test_execution(
                    session, principal, _request(dataset_sha, top_k=top_k)
                )
                await session.commit()
                return row.id
            except EvaluationConflictError:
                await session.rollback()
                return None

    winners = [item for item in await asyncio.gather(freeze(5), freeze(10)) if item is not None]
    assert len(winners) == 1
    execution_id = winners[0]

    async def start() -> bool:
        async with async_session_factory() as session:
            try:
                await start_test_execution(session, principal, execution_id)
                await session.commit()
                return True
            except EvaluationConflictError:
                await session.rollback()
                return False

    assert sorted(await asyncio.gather(start(), start())) == [False, True]
    async with async_session_factory() as session:
        row = await session.get(EvaluationTestExecution, execution_id)
        assert row is not None
        run_id = row.evaluation_run_id
        await session.execute(
            delete(EvaluationJobOutbox).where(EvaluationJobOutbox.execution_id == execution_id)
        )
        await session.execute(
            delete(EvaluationTestExecution).where(EvaluationTestExecution.id == execution_id)
        )
        await session.execute(delete(EvaluationRun).where(EvaluationRun.id == run_id))
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()


@pytest.mark.integration
async def test_stale_delivery_and_expired_claim_recover_once() -> None:
    user_id = uuid.uuid4()
    dataset_sha = _dataset_sha()
    principal = Principal(
        user_id=str(user_id),
        role=Role.ADMIN,
        allowed_departments=frozenset(),
        max_access_level=AccessLevel.PUBLIC,
    )
    async with async_session_factory() as session:
        session.add(
            User(
                id=user_id,
                username=f"eval-recovery-{user_id}",
                password_hash="unused",
                role=Role.ADMIN,
                allowed_departments=[],
                max_access_level=1,
            )
        )
        await session.commit()
        execution = await freeze_test_execution(session, principal, _request(dataset_sha))
        await start_test_execution(session, principal, execution.id)
        await session.commit()
        execution_id, run_id = execution.id, execution.evaluation_run_id
    async with async_session_factory() as session:
        outbox = await session.scalar(
            select(EvaluationJobOutbox).where(EvaluationJobOutbox.execution_id == execution_id)
        )
        assert outbox is not None
        outbox.delivered_at = datetime.now(UTC) - timedelta(minutes=10)
        await session.commit()
    assert sorted(
        await asyncio.gather(
            recover_stale_evaluation_delivery(grace_seconds=1),
            recover_stale_evaluation_delivery(grace_seconds=1),
        )
    ) == [0, 1]
    async with async_session_factory() as session:
        execution = await session.get(EvaluationTestExecution, execution_id)
        outbox = await session.scalar(
            select(EvaluationJobOutbox).where(EvaluationJobOutbox.execution_id == execution_id)
        )
        assert execution is not None and outbox is not None
        execution.claim_token, execution.lease_owner = "token", "worker"
        execution.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        outbox.delivered_at = datetime.now(UTC)
        await session.commit()
    assert sorted(
        await asyncio.gather(
            recover_expired_evaluation_claims(), recover_expired_evaluation_claims()
        )
    ) == [0, 1]
    async with async_session_factory() as session:
        execution = await session.get(EvaluationTestExecution, execution_id)
        outbox = await session.scalar(
            select(EvaluationJobOutbox).where(EvaluationJobOutbox.execution_id == execution_id)
        )
        assert execution is not None and execution.claim_token is None
        assert outbox is not None and outbox.delivered_at is None
        await session.execute(
            delete(EvaluationJobOutbox).where(EvaluationJobOutbox.execution_id == execution_id)
        )
        await session.execute(
            delete(EvaluationTestExecution).where(EvaluationTestExecution.id == execution_id)
        )
        await session.execute(delete(EvaluationRun).where(EvaluationRun.id == run_id))
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()


@pytest.mark.integration
async def test_heartbeat_prevents_expired_claim_recovery() -> None:
    user_id = uuid.uuid4()
    dataset_sha = _dataset_sha()
    principal = Principal(
        user_id=str(user_id),
        role=Role.ADMIN,
        allowed_departments=frozenset(),
        max_access_level=AccessLevel.PUBLIC,
    )
    async with async_session_factory() as session:
        session.add(
            User(
                id=user_id,
                username=f"eval-heartbeat-{user_id}",
                password_hash="unused",
                role=Role.ADMIN,
                allowed_departments=[],
                max_access_level=1,
            )
        )
        await session.commit()
        execution = await freeze_test_execution(session, principal, _request(dataset_sha))
        await start_test_execution(session, principal, execution.id)
        execution.claim_token, execution.lease_owner = "heartbeat-token", "heartbeat-worker"
        execution.lease_expires_at = datetime.now(UTC) + timedelta(seconds=1)
        await session.commit()
        execution_id, run_id = execution.id, execution.evaluation_run_id

    async def slow_result() -> str:
        await asyncio.sleep(1.4)
        return "done"

    task = asyncio.create_task(
        _with_heartbeat(slow_result, execution_id, "heartbeat-worker", "heartbeat-token", 1)
    )
    await asyncio.sleep(1.1)
    assert await recover_expired_evaluation_claims() == 0
    assert await task == "done"
    async with async_session_factory() as session:
        execution = await session.get(EvaluationTestExecution, execution_id)
        assert execution is not None and execution.lease_expires_at is not None
        assert execution.lease_expires_at > datetime.now(UTC)
        version = execution.version
        execution.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    async with async_session_factory() as session:
        with pytest.raises(EvaluationLeaseLost):
            await _lock_fenced_execution(
                session, execution_id, "heartbeat-worker", "heartbeat-token", version
            )
        await session.rollback()
        execution = await session.get(EvaluationTestExecution, execution_id)
        assert execution is not None
        execution.lease_owner = "new-owner"
        execution.claim_token = "new-token"
        execution.version += 1
        execution.lease_expires_at = datetime.now(UTC) + timedelta(seconds=30)
        await session.commit()
        with pytest.raises(EvaluationLeaseLost):
            await _lock_fenced_execution(
                session, execution_id, "heartbeat-worker", "heartbeat-token", version
            )
        await session.rollback()
        current = await _lock_fenced_execution(
            session, execution_id, "new-owner", "new-token", version + 1
        )
        assert current.id == execution_id
        await session.rollback()
    async with async_session_factory() as session:
        await session.execute(
            delete(EvaluationJobOutbox).where(EvaluationJobOutbox.execution_id == execution_id)
        )
        await session.execute(
            delete(EvaluationTestExecution).where(EvaluationTestExecution.id == execution_id)
        )
        await session.execute(delete(EvaluationRun).where(EvaluationRun.id == run_id))
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()
