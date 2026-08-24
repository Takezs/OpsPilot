from opspilot.knowledge.tasks import DOCUMENT_INDEX_JOB_TIMEOUT, DOCUMENT_INDEX_MAX_TRIES
from opspilot.worker import WorkerSettings


def test_worker_registration_matches_business_retry_constants() -> None:
    registered = WorkerSettings.functions[0]
    assert registered.max_tries == DOCUMENT_INDEX_MAX_TRIES
    assert registered.timeout_s == DOCUMENT_INDEX_JOB_TIMEOUT
