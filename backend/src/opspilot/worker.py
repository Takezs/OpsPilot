from arq import func
from arq.connections import RedisSettings

from opspilot.config import Settings
from opspilot.execution.executor import drain_detached_provider_tasks
from opspilot.knowledge.tasks import (
    DOCUMENT_INDEX_JOB_TIMEOUT,
    DOCUMENT_INDEX_MAX_TRIES,
    index_document,
)


async def shutdown_worker(ctx: dict[str, object]) -> None:
    """Collect cancellation-resistant provider tasks before ARQ exits."""
    await drain_detached_provider_tasks()


class WorkerSettings:
    functions = [
        func(
            index_document,
            max_tries=DOCUMENT_INDEX_MAX_TRIES,
            timeout=DOCUMENT_INDEX_JOB_TIMEOUT,
        )
    ]
    on_shutdown = shutdown_worker
    redis_settings = RedisSettings.from_dsn(Settings().redis_url)
