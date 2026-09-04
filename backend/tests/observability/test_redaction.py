import logging

import pytest
from uvicorn.logging import AccessFormatter

from opspilot.observability.redaction import SafeLogFilter, install_safe_logging, safe_attributes


def test_trace_and_log_attributes_reuse_journal_redaction(caplog: pytest.LogCaptureFixture) -> None:
    secret = "Bearer super-secret-token"
    prompt = "refund alice@example.com phone 13800138000 " + "x" * 2000
    attributes = safe_attributes({"authorization": secret, "prompt": prompt, "run_id": "run-1"})
    rendered = repr(attributes)
    assert secret not in rendered
    assert "alice@example.com" not in rendered
    assert "13800138000" not in rendered
    assert prompt not in rendered
    assert attributes["run_id"] == "run-1"

    logger = logging.getLogger("opspilot.test.safe")
    logger.addFilter(SafeLogFilter())
    with caplog.at_level(logging.INFO, logger=logger.name):
        logger.info("Authorization=%s email=%s", secret, "alice@example.com")
    assert secret not in caplog.text
    assert "alice@example.com" not in caplog.text


def test_safe_log_filter_preserves_uvicorn_access_formatter_arguments() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (("127.0.0.1", 43210), "GET", "/health", "1.1", 200),
        None,
    )

    assert SafeLogFilter().filter(record)
    rendered = AccessFormatter(
        "%(levelprefix)s %(client_addr)s - %(request_line)s %(status_code)s"
    ).format(record)

    assert "GET /health HTTP/1.1" in rendered
    assert "200" in rendered


@pytest.mark.parametrize("key", ["api_key", "password", "secret", "authorization", "cer", "token"])
def test_formatted_credential_values_are_redacted(key: str) -> None:
    record = logging.LogRecord(
        "opspilot.safe", logging.INFO, __file__, 1, f"{key}=%s", ("raw-value",), None
    )
    assert SafeLogFilter().filter(record)
    assert "raw-value" not in record.getMessage()


def test_mapping_args_and_handlers_added_after_install_are_redacted() -> None:
    logger = logging.getLogger("opspilot.late-handler")
    logger.handlers.clear()
    install_safe_logging()
    handler = logging.StreamHandler()
    logger.addHandler(handler)
    install_safe_logging()
    install_safe_logging()
    filters = [item for item in handler.filters if isinstance(item, SafeLogFilter)]
    assert len(filters) == 1
    record = logging.LogRecord(
        logger.name,
        logging.INFO,
        __file__,
        1,
        "api_key=%(value)s",
        ({"value": "raw"},),
        None,
    )
    assert filters[0].filter(record)
    assert "raw" not in record.getMessage()
