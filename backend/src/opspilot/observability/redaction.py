"""Safe logging and trace attributes built on the Run Journal sanitizer."""

import hashlib
import logging
import re
from collections.abc import Mapping

from opspilot.runs.sanitize import sanitize_value

_CONTENT_KEYS = frozenset({"prompt", "content", "request", "response", "provider_response"})
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|password|secret|authorization|cer|token)\b\s*[:=]\s*)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
)


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


def safe_exception_attributes(error: BaseException) -> dict[str, str]:
    """Describe an exception without exporting its message or traceback."""
    return {"exception.type": type(error).__name__, "exception.summary": _summary(error)}


class SafeLogFilter(logging.Filter):
    """Sanitize the fully rendered record before any handler persists it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if record.name == "uvicorn.access" and isinstance(record.args, tuple):
                record.args = tuple(sanitize_value(value) for value in record.args)
                return True
            rendered = record.getMessage()
            rendered = _CREDENTIAL_ASSIGNMENT.sub(r"\1[REDACTED]", rendered)
            record.msg = sanitize_value(rendered)
            record.args = ()
        except BaseException:
            record.msg = "[REDACTED_LOG_RECORD]"
            record.args = ()
        return True


def install_safe_logging() -> None:
    """Attach sanitization to configured handlers without breaking formatter arguments."""
    loggers = [logging.getLogger()]
    loggers.extend(
        logger
        for logger in logging.Logger.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    )
    for logger in loggers:
        for handler in logger.handlers:
            if not any(isinstance(item, SafeLogFilter) for item in handler.filters):
                handler.addFilter(SafeLogFilter())
