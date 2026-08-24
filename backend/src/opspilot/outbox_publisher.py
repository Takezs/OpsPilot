from typing import Any

from arq import cron
from arq.connections import RedisSettings

from opspilot.config import Settings
from opspilot.knowledge.outbox import (
    publish_pending_document_jobs,
    reconcile_expired_retry_attempts,
)
from opspilot.knowledge.router import ArqDocumentQueue


async def publish_document_outbox(ctx: dict[str, Any]) -> int:
    return await publish_pending_document_jobs(ArqDocumentQueue(ctx["redis"]))


async def reconcile_document_retries(ctx: dict[str, Any]) -> int:
    return await reconcile_expired_retry_attempts()


class OutboxPublisherSettings:
    redis_settings = RedisSettings.from_dsn(Settings().redis_url)
    cron_jobs = [
        cron(
            publish_document_outbox,
            second={0, 10, 20, 30, 40, 50},
            unique=True,
        ),
        cron(reconcile_document_retries, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
    ]
