from typing import Any

from arq import cron
from arq.connections import RedisSettings

from opspilot.config import Settings
from opspilot.knowledge.outbox import publish_pending_document_jobs
from opspilot.knowledge.router import ArqDocumentQueue


async def publish_document_outbox(ctx: dict[str, Any]) -> int:
    return await publish_pending_document_jobs(ArqDocumentQueue(ctx["redis"]))


class OutboxPublisherSettings:
    redis_settings = RedisSettings.from_dsn(Settings().redis_url)
    cron_jobs = [
        cron(
            publish_document_outbox,
            second={0, 10, 20, 30, 40, 50},
            unique=True,
        )
    ]
