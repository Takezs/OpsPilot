from typing import Any

from arq import cron
from arq.connections import RedisSettings

from opspilot.config import Settings
from opspilot.evaluation.outbox import (
    ArqEvaluationQueue,
    publish_pending_evaluation_jobs,
    recover_expired_evaluation_claims,
    recover_stale_evaluation_delivery,
)
from opspilot.jobs.outbox import (
    ArqOperationQueue,
    ArqRunQueue,
    publish_pending_operation_jobs,
    publish_pending_run_jobs,
)
from opspilot.jobs.queues import PUBLISHER_QUEUE
from opspilot.jobs.recovery import (
    recover_expired_operations,
    recover_expired_run_jobs,
    recover_stale_delivered_jobs,
)
from opspilot.knowledge.outbox import (
    publish_pending_document_jobs,
    reconcile_expired_retry_attempts,
)
from opspilot.knowledge.router import ArqDocumentQueue
from opspilot.observability.redaction import install_safe_logging
from opspilot.runs.outbox import RedisRunEventNotifier, publish_pending_events

install_safe_logging()


async def publish_document_outbox(ctx: dict[str, Any]) -> int:
    return await publish_pending_document_jobs(ArqDocumentQueue(ctx["redis"]))


async def reconcile_document_retries(ctx: dict[str, Any]) -> int:
    return await reconcile_expired_retry_attempts()


async def publish_operation_outbox(ctx: dict[str, Any]) -> int:
    return await publish_pending_operation_jobs(ArqOperationQueue(ctx["redis"]))


async def publish_run_event_outbox(ctx: dict[str, Any]) -> int:
    return await publish_pending_events(RedisRunEventNotifier(ctx["redis"]))


async def publish_run_job_outbox(ctx: dict[str, Any]) -> int:
    return await publish_pending_run_jobs(ArqRunQueue(ctx["redis"]))


async def publish_evaluation_outbox(ctx: dict[str, Any]) -> int:
    return await publish_pending_evaluation_jobs(ArqEvaluationQueue(ctx["redis"]))


async def recover_operation_leases(ctx: dict[str, Any]) -> int:
    return await recover_expired_operations()


async def recover_run_job_leases(ctx: dict[str, Any]) -> int:
    return await recover_expired_run_jobs()


async def recover_unclaimed_deliveries(ctx: dict[str, Any]) -> int:
    return await recover_stale_delivered_jobs()


async def recover_evaluation_deliveries(ctx: dict[str, Any]) -> int:
    return await recover_stale_evaluation_delivery()


async def recover_evaluation_claims(ctx: dict[str, Any]) -> int:
    return await recover_expired_evaluation_claims()


class OutboxPublisherSettings:
    queue_name = PUBLISHER_QUEUE
    redis_settings = RedisSettings.from_dsn(Settings().redis_url)
    cron_jobs = [
        cron(
            publish_document_outbox,
            second={0, 10, 20, 30, 40, 50},
            unique=True,
        ),
        cron(reconcile_document_retries, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
        cron(publish_operation_outbox, second={1, 6, 11, 16, 21, 26, 31, 36, 41, 46, 51, 56}),
        cron(publish_run_event_outbox, second={2, 7, 12, 17, 22, 27, 32, 37, 42, 47, 52, 57}),
        cron(publish_run_job_outbox, second={3, 8, 13, 18, 23, 28, 33, 38, 43, 48, 53, 58}),
        cron(publish_evaluation_outbox, second={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
        cron(recover_operation_leases, second={4, 14, 24, 34, 44, 54}, unique=True),
        cron(recover_run_job_leases, second={9, 19, 29, 39, 49, 59}, unique=True),
        cron(recover_unclaimed_deliveries, second={7, 17, 27, 37, 47, 57}, unique=True),
        cron(recover_evaluation_deliveries, second={8, 18, 28, 38, 48, 58}, unique=True),
        cron(recover_evaluation_claims, second={9, 29, 49}, unique=True),
    ]
