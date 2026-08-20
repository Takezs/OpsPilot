from arq.connections import RedisSettings

from opspilot.config import Settings
from opspilot.knowledge.tasks import index_document


class WorkerSettings:
    functions = [index_document]
    redis_settings = RedisSettings.from_dsn(Settings().redis_url)
