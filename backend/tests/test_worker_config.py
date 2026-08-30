from opspilot.knowledge.tasks import DOCUMENT_INDEX_JOB_TIMEOUT, DOCUMENT_INDEX_MAX_TRIES
from opspilot.worker import RUN_MESSAGE_JOB_TIMEOUT, WorkerSettings


def test_worker_registration_matches_business_retry_constants() -> None:
    registered = WorkerSettings.functions[0]
    assert registered.max_tries == DOCUMENT_INDEX_MAX_TRIES
    assert registered.timeout_s == DOCUMENT_INDEX_JOB_TIMEOUT


def test_run_message_watchdog_covers_bounded_agent_and_generation_budget() -> None:
    registered = next(
        item for item in WorkerSettings.functions if item.name == "process_run_message"
    )

    assert RUN_MESSAGE_JOB_TIMEOUT >= 8 * 60 + 3 * 30
    assert registered.timeout_s == RUN_MESSAGE_JOB_TIMEOUT
