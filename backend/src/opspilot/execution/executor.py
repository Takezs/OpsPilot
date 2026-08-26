"""Lease-guarded execution driver: claim, invoke, heartbeat and fence the write.

``execute_operation`` composes the Task 10 primitives into a worker loop for one
Operation. The claim is committed first so the lease is durable: once a worker
is EXECUTING, a lost lease (failed renewal) can never put the Operation back
into a claimable state — the recovery path moves it to OUTCOME_UNKNOWN (side
effect) or RETRYING (read only) instead.

While the provider call runs, a concurrent heartbeat renews the lease every
~``lease_seconds/3``, so a call that outlives one lease window keeps its write
right. Each renewal is its own session and transaction and resolves PostgreSQL
``clock_timestamp()`` by default (the injected ``now`` clock is a deterministic
test seam only), so every worker judges expiry against the same database clock
regardless of its host clock.

The result write is a separate fenced transaction
(``id + version + claim_token + owner + live lease``); a renewal or result
write that matches zero rows raises ``LeaseConflictError`` and nothing is
committed. The DB fence — not an in-memory check — decides every race: a
heartbeat that loses the lease revokes the in-flight provider call and surfaces
the conflict immediately, and a provider that returns while the lease was lost
is fenced out by the result write. The provider task is awaited to completion
(or, for a provider that ignores cancellation, abandoned after a bounded grace)
so no asyncio task is leaked, and a revoked worker never writes a terminal
state.
"""

import asyncio
import random
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.execution.attempts import finish_attempt, start_attempt
from opspilot.execution.claim import (
    LeaseConflictError,
    LeaseStateError,
    claim_operation,
    mark_failed,
    mark_retrying,
    mark_succeeded,
    mark_unknown,
    renew_lease,
)
from opspilot.execution.errors import FailureDisposition, classify_failure
from opspilot.execution.models import Operation, OperationAttempt
from opspilot.execution.retry import RetryPolicy
from opspilot.tools.types import ToolEffect, ToolResult

Invoker = Callable[[Operation], Coroutine[Any, Any, ToolResult]]
Clock = Callable[[], datetime]
SessionFactory = Callable[[], AsyncSession]

# Bounded grace for abandoning a provider that ignores cancellation: cooperative
# providers unwind instantly, and a stuck one must not block the revocation.
_CANCEL_GRACE_SECONDS = 1.0
_RETRY_POLICY = RetryPolicy()
_DETACHED_PROVIDER_TASKS: set[asyncio.Task[Any]] = set()


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    """Retrieve a finished task's result so its exception is never orphaned."""
    if not task.done() or task.cancelled():
        return
    try:
        task.result()
    except BaseException:
        pass


def _forget_detached_provider(task: asyncio.Task[Any]) -> None:
    _consume_task_result(task)
    _DETACHED_PROVIDER_TASKS.discard(task)


def _detach_provider_task(task: asyncio.Task[Any]) -> None:
    """Keep ownership of a non-cooperative provider until it really finishes."""
    if task.done():
        _consume_task_result(task)
        return
    _DETACHED_PROVIDER_TASKS.add(task)
    task.add_done_callback(_forget_detached_provider)


def detached_provider_task_count() -> int:
    """Return the number of cancellation-resistant providers still supervised."""
    return len(_DETACHED_PROVIDER_TASKS)


async def drain_detached_provider_tasks() -> None:
    """Cancel and collect supervised providers during worker shutdown/tests."""
    tasks = tuple(_DETACHED_PROVIDER_TASKS)
    if not tasks:
        return
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _clock_value(session: AsyncSession, clock: Clock | None) -> datetime:
    """Resolve the authoritative "now": the injected clock or the DB clock."""
    if clock is not None:
        return clock()
    resolved: datetime | None = await session.scalar(select(func.clock_timestamp()))
    if resolved is None:  # pragma: no cover - clock_timestamp() never yields NULL
        return datetime.now(UTC)
    return resolved


async def _bounded_wait(task: asyncio.Task[Any], *, detach_on_timeout: bool = False) -> None:
    """Wait for a task to finish without propagating its exception.

    A provider that ignores cancellation is abandoned after a bounded grace
    rather than awaited forever; its external request may remain outcome-unknown,
    but this worker never touches the database again.
    """
    if task.done():
        _consume_task_result(task)
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=_CANCEL_GRACE_SECONDS)
    except TimeoutError:
        if detach_on_timeout:
            _detach_provider_task(task)
    except asyncio.CancelledError:
        if task.done():
            _consume_task_result(task)
            return
        if detach_on_timeout:
            _detach_provider_task(task)
        raise
    else:
        _consume_task_result(task)


async def _heartbeat_loop(
    session_factory: SessionFactory,
    operation_id: uuid.UUID,
    *,
    owner: str,
    token: str,
    expected_version: int,
    lease_seconds: int,
    clock: Clock | None,
    stop: asyncio.Event,
) -> None:
    """Renew the lease every ~``lease_seconds/3`` until ``stop`` is set.

    Runs concurrently with the provider invocation. Each renewal opens its own
    session and transaction, resolves the same clock seam as the rest of the
    executor (PostgreSQL ``clock_timestamp()`` by default) and appends the
    ``operation_lease_renewed`` event/outbox row atomically. A lost lease
    surfaces as ``LeaseConflictError`` from this task; the executor then revokes
    the provider and re-raises it.
    """
    interval = max(1, lease_seconds // 3)
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        async with session_factory() as session:
            timestamp = await _clock_value(session, clock)
            await renew_lease(
                session,
                operation_id,
                owner=owner,
                token=token,
                expected_version=expected_version,
                lease_seconds=lease_seconds,
                now=timestamp,
            )
            await session.commit()


async def execute_operation(
    session_factory: SessionFactory,
    operation_id: uuid.UUID,
    *,
    owner: str,
    lease_seconds: int,
    effect: ToolEffect,
    invoke: Invoker,
    now: Clock | None = None,
) -> Operation:
    """Claim, run under a fenced lease, and write the terminal result.

    ``invoke`` performs the tool call and returns a journal-safe ``ToolResult``.
    The claim transaction is committed before the invocation so an interrupted
    or stale worker leaves the Operation in EXECUTING (never claimable again).

    While ``invoke`` is in flight a concurrent heartbeat renews the lease every
    ~``lease_seconds/3``, so a provider call that outlives one lease window does
    not lose its write right. After the invocation the heartbeat is stopped and
    awaited, then the terminal result is written fenced. A lost lease — from the
    heartbeat or the result write — raises ``LeaseConflictError``; the provider
    result is then never committed, and a non-cooperative provider is abandoned
    rather than awaited forever. A SIDE_EFFECT failure that cannot prove the
    provider was never reached maps to OUTCOME_UNKNOWN, never FAILED.
    """
    # Transaction 1: durable claim.
    async with session_factory() as session:
        timestamp = await _clock_value(session, now)
        operation = await claim_operation(
            session, operation_id, owner=owner, lease_seconds=lease_seconds, now=timestamp
        )
        attempt = await start_attempt(
            session,
            operation,
            kind="EXECUTION",
            request={
                "tool_name": operation.tool_name,
                "arguments": operation.normalized_arguments,
                "idempotency_key": operation.idempotency_key,
            },
        )
        await session.commit()

    token = operation.claim_token
    expires_at = operation.lease_expires_at
    if token is None or expires_at is None:
        raise LeaseStateError(f"operation {operation_id} was not claimed with a live lease")
    claimed_version = operation.version
    attempt_id = attempt.id

    # Run the provider invocation concurrently with the lease heartbeat.
    invoke_task: asyncio.Task[ToolResult] = asyncio.create_task(invoke(operation))
    stop = asyncio.Event()
    heartbeat_task: asyncio.Task[None] = asyncio.create_task(
        _heartbeat_loop(
            session_factory,
            operation_id,
            owner=owner,
            token=token,
            expected_version=claimed_version,
            lease_seconds=lease_seconds,
            clock=now,
            stop=stop,
        )
    )
    try:
        while True:
            done, _ = await asyncio.wait(
                {invoke_task, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat_task in done:
                failure = heartbeat_task.exception()
                if failure is not None:
                    # The lease was lost: revoke the provider, never write a result.
                    invoke_task.cancel()
                    await _bounded_wait(invoke_task, detach_on_timeout=True)
                    raise failure
                # The heartbeat stopped before it was asked to: a programming
                # error, so fail closed rather than write while unguarded.
                invoke_task.cancel()
                await _bounded_wait(invoke_task, detach_on_timeout=True)
                raise LeaseConflictError(
                    f"lease heartbeat stopped unexpectedly for operation {operation_id}"
                )
            if invoke_task in done:
                outcome = invoke_task.result()  # raises the provider error, if any
                break
    finally:
        stop.set()
        if not heartbeat_task.done():
            heartbeat_task.cancel()
        await _bounded_wait(heartbeat_task)
        if not invoke_task.done():
            invoke_task.cancel()
            await _bounded_wait(invoke_task, detach_on_timeout=True)
        else:
            _consume_task_result(invoke_task)

    # Transaction 2: fenced terminal write. The heartbeat kept the lease alive
    # while invoke ran; the fenced update is the single authoritative decision.
    async with session_factory() as session:
        timestamp = await _clock_value(session, now)
        try:
            if outcome.ok:
                reference = outcome.data.get("provider_reference") if outcome.data else None
                operation = await mark_succeeded(
                    session,
                    operation_id,
                    owner=owner,
                    token=token,
                    expected_version=claimed_version,
                    result=outcome.data,
                    provider_reference_id=reference if isinstance(reference, str) else None,
                    now=timestamp,
                )
            else:
                disposition = classify_failure(effect, outcome)
                if disposition is FailureDisposition.RETRY and _RETRY_POLICY.can_retry(
                    attempt.attempt_number
                ):
                    operation = await mark_retrying(
                        session,
                        operation_id,
                        owner=owner,
                        token=token,
                        expected_version=claimed_version,
                        error=outcome.error,
                        retry_delay_seconds=_RETRY_POLICY.delay_for(
                            attempt.attempt_number, jitter=random.random()
                        ),
                        now=timestamp,
                    )
                elif disposition is FailureDisposition.FAIL or (
                    disposition is FailureDisposition.RETRY
                    and not _RETRY_POLICY.can_retry(attempt.attempt_number)
                ):
                    operation = await mark_failed(
                        session,
                        operation_id,
                        owner=owner,
                        token=token,
                        expected_version=claimed_version,
                        error=outcome.error,
                        now=timestamp,
                    )
                else:
                    # A SIDE_EFFECT failure that cannot prove the provider was never
                    # reached: the effect may or may not have happened. Record the
                    # uncertainty; reconciliation decides the verdict.
                    operation = await mark_unknown(
                        session,
                        operation_id,
                        owner=owner,
                        token=token,
                        expected_version=claimed_version,
                        error=outcome.error,
                        now=timestamp,
                    )
            stored_attempt = await session.get(OperationAttempt, attempt_id)
            if stored_attempt is None:
                raise LeaseStateError(f"operation attempt {attempt_id} disappeared")
            finish_attempt(
                stored_attempt,
                status="SUCCEEDED" if outcome.ok else "FAILED",
                response=outcome.data,
                error=outcome.error,
                completed_at=timestamp,
            )
            await session.commit()
        except LeaseConflictError:
            await session.rollback()
            raise
    return operation
