"""Safe logging and trace attributes built on the Run Journal sanitizer."""

import hashlib
import logging
import re
from collections.abc import Mapping
from typing import cast
from urllib.parse import unquote_plus

from opspilot.runs.sanitize import sanitize_value

_CONTENT_KEYS = frozenset({"prompt", "content", "request", "response", "provider_response"})
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)((?<![A-Za-z0-9_])[\"']?"
    r"(?:api[_-]?key|password|secret|authorization|cer|token)"
    r"[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;}\]&]+)"
)
_QUERY_PARAMETER = re.compile(r"([?&])([^=&#]+)=([^&#]*)")
_QUERY_CREDENTIAL_KEYS = frozenset(
    {"token", "access_token", "api_key", "key", "secret", "password", "authorization"}
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


def _sanitize_request_target(value: object) -> object:
    if not isinstance(value, str) or "?" not in value:
        return sanitize_value(value)

    def replace(match: re.Match[str]) -> str:
        key = unquote_plus(match.group(2)).casefold().replace("-", "_")
        if key in _QUERY_CREDENTIAL_KEYS:
            return f"{match.group(1)}{match.group(2)}=[REDACTED]"
        return match.group(0)

    return sanitize_value(_QUERY_PARAMETER.sub(replace, value))


def _sanitize_args(
    args: tuple[object, ...] | Mapping[str, object] | None,
) -> tuple[object, ...] | Mapping[str, object] | None:
    if args is None:
        return None
    if isinstance(args, Mapping):
        return cast(dict[str, object], sanitize_value(dict(args)))
    return tuple(sanitize_value(value) for value in args)


class SafeLogFilter(logging.Filter):
    """Sanitize the fully rendered record before any handler persists it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if record.name == "uvicorn.access" and isinstance(record.args, tuple):
                record.args = tuple(
                    _sanitize_request_target(value) if index == 2 else sanitize_value(value)
                    for index, value in enumerate(record.args)
                )
                return True
            record.args = _sanitize_args(record.args)
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
