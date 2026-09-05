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


def test_uvicorn_access_query_credentials_are_redacted_without_breaking_formatter() -> None:
    secrets = (
        "tok-secret",
        "access-secret",
        "api-secret",
        "plain-key",
        "password-value",
        "authorization-value",
    )
    target = (
        "/search?token=tok-secret&token_count=42&ACCESS_TOKEN=access-secret"
        "&api%5Fkey=api-secret&key=plain-key&Password=password-value"
        "&authorization=authorization-value"
    )
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (("127.0.0.1", 43210), "GET", target, "1.1", 200),
        None,
    )

    assert SafeLogFilter().filter(record)
    rendered = AccessFormatter(
        "%(levelprefix)s %(client_addr)s - %(request_line)s %(status_code)s"
    ).format(record)

    assert "GET /search?" in rendered
    assert "token_count=42" in rendered
    assert rendered.count("[REDACTED]") == 6
    assert all(secret not in rendered for secret in secrets)


@pytest.mark.parametrize(
    "target",
    [
        "/x?foo=1;token=semicolon-secret",
        "/x?foo=1&bar=2;API_KEY=mixed-secret&token=repeat-one;token=repeat-two",
        "/x?password=;token=no-value;Authorization=Bearer%20encoded-secret",
        "/x?foo=ok;to%6Ben=encoded-key-secret#token=fragment-secret",
        "/x?broken;SECRET=malformed-secret&token_count=17",
    ],
)
def test_uvicorn_access_query_credentials_fail_closed_across_delimiters(target: str) -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (("127.0.0.1", 43210), "GET", target, "1.1", 200),
        None,
    )

    assert SafeLogFilter().filter(record)
    rendered = AccessFormatter(
        "%(levelprefix)s %(client_addr)s - %(request_line)s %(status_code)s"
    ).format(record)

    for secret in (
        "semicolon-secret",
        "mixed-secret",
        "repeat-one",
        "repeat-two",
        "encoded-secret",
        "encoded-key-secret",
        "fragment-secret",
        "malformed-secret",
    ):
        assert secret not in rendered
    assert "token_count=17" in rendered if "token_count" in target else True


@pytest.mark.parametrize(
    "message",
    [
        'payload={"token":"abc\\"tail-secret"}',
        'payload={"password":"first-line\nsecond-line-secret"}',
        "payload={'api_key': 'abc\\'tail-secret'}",
        'payload={"nested":{"Authorization":"Bearer multiline\r\ntail-secret"}}',
        'payload={"ＴＯＫＥＮ":"unicode-secret", "token":"unterminated-secret',
    ],
)
def test_quoted_credentials_with_escapes_multiline_and_malformed_values_fail_closed(
    message: str,
) -> None:
    record = logging.LogRecord("opspilot.safe", logging.INFO, __file__, 1, message, (), None)
    assert SafeLogFilter().filter(record)
    rendered = record.getMessage()
    assert "tail-secret" not in rendered
    assert "second-line-secret" not in rendered
    assert "unicode-secret" not in rendered
    assert "unterminated-secret" not in rendered
    assert len(rendered) <= 1000


def test_malformed_long_credential_logging_is_bounded() -> None:
    secret = "s" * 100_000
    record = logging.LogRecord(
        "opspilot.safe", logging.INFO, __file__, 1, '{"token":"' + secret, (), None
    )
    assert SafeLogFilter().filter(record)
    assert secret not in record.getMessage()
    assert len(record.getMessage()) <= 1000


@pytest.mark.parametrize(
    ("message", "args"),
    [
        ("payload=%s", ({"api_key": "alpha", "nested": [{"password": "bravo"}]},)),
        ("payload=%(payload)s", ({"payload": {"authorization": "charlie"}},)),
        ('payload={"secret": "delta", "token": "echo"}', ()),
        ("payload={'api_key': 'foxtrot', 'nested': {'password': 'golf'}}", ()),
    ],
)
def test_quoted_and_structured_mapping_credentials_are_redacted(
    message: str, args: tuple[object, ...]
) -> None:
    record = logging.LogRecord("opspilot.safe", logging.INFO, __file__, 1, message, args, None)
    assert SafeLogFilter().filter(record)
    rendered = record.getMessage()
    for secret in ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf"):
        assert secret not in rendered
    assert len(rendered) <= 1000


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
