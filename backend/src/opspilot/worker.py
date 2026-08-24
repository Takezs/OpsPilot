from arq import func
from arq.connections import RedisSettings

from opspilot.config import Settings
from opspilot.knowledge.tasks import (
    DOCUMENT_INDEX_JOB_TIMEOUT,
    DOCUMENT_INDEX_MAX_TRIES,
    index_document,
)


class WorkerSettings:
    functions = [
        func(
            index_document,
            max_tries=DOCUMENT_INDEX_MAX_TRIES,
            timeout=DOCUMENT_INDEX_JOB_TIMEOUT,
        )
    ]
    redis_settings = RedisSettings.from_dsn(Settings().redis_url)
