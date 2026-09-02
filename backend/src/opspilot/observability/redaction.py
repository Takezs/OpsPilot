"""Safe logging and trace attributes built on the Run Journal sanitizer."""

import hashlib
import logging
from collections.abc import Mapping

from opspilot.runs.sanitize import sanitize_value

_CONTENT_KEYS = frozenset({"prompt", "content", "request", "response", "provider_response"})


def _summary(value: object) -> str:
    rendered = str(value)
    digest = hashlib.sha256(rendered.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"sha256:{digest};length:{len(rendered)}"


def safe_attributes(values: Mapping[str, object]) -> dict[str, object]:
    """Return bounded attributes without raw prompts, provider bodies, secrets or PII."""
    result: dict[str, object] = {}
    for key, value in values.items():
        result[key] = _summary(value) if key.casefold() in _CONTENT_KEYS else sanitize_value(value)
    return result


class SafeLogFilter(logging.Filter):
    """Sanitize the fully rendered record before any handler persists it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = sanitize_value(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(sanitize_value(value) for value in record.args)
            elif isinstance(record.args, dict):
                record.args = sanitize_value(record.args)
        except BaseException:
            record.msg = "[REDACTED_LOG_RECORD]"
            record.args = ()
        return True


_logging_installed = False


def install_safe_logging() -> None:
    """Attach sanitization to configured handlers without breaking formatter arguments."""
    global _logging_installed
    if _logging_installed:
        return
    safe_filter = SafeLogFilter()
    loggers = [logging.getLogger()]
    loggers.extend(
        logger
        for logger in logging.Logger.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    )
    for logger in loggers:
        for handler in logger.handlers:
            handler.addFilter(safe_filter)
    _logging_installed = True
