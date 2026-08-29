from typing import Any

from arq import cron
from arq.connections import RedisSettings

from opspilot.config import Settings
from opspilot.jobs.outbox import (
    ArqOperationQueue,
    ArqRunQueue,
    publish_pending_operation_jobs,
    publish_pending_run_jobs,
)
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
from opspilot.runs.outbox import RedisRunEventNotifier, publish_pending_events


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


async def recover_operation_leases(ctx: dict[str, Any]) -> int:
    return await recover_expired_operations()


async def recover_run_job_leases(ctx: dict[str, Any]) -> int:
    return await recover_expired_run_jobs()


async def recover_unclaimed_deliveries(ctx: dict[str, Any]) -> int:
    return await recover_stale_delivered_jobs()


class OutboxPublisherSettings:
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
        cron(recover_operation_leases, second={4, 14, 24, 34, 44, 54}, unique=True),
        cron(recover_run_job_leases, second={9, 19, 29, 39, 49, 59}, unique=True),
        cron(recover_unclaimed_deliveries, second={7, 17, 27, 37, 47, 57}, unique=True),
    ]
