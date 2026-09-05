"""Safe logging and trace attributes built on the Run Journal sanitizer."""

import hashlib
import logging
import re
import unicodedata
from collections.abc import Mapping
from typing import cast
from urllib.parse import unquote_plus

from opspilot.runs.sanitize import sanitize_value

_CONTENT_KEYS = frozenset({"prompt", "content", "request", "response", "provider_response"})
_QUERY_PARAMETER = re.compile(r"([?&])([^=&#]+)=([^&#]*)")
_QUERY_CREDENTIAL_KEYS = frozenset(
    {"token", "access_token", "api_key", "key", "secret", "password", "authorization"}
)
_MAX_KEY_LENGTH = 256
_MAX_KEY_NORMALIZATION_ROUNDS = 4
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_INVALID_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_RESIDUAL_BACKSLASH_ESCAPE = re.compile(r"\\(?:[uUxX]|[\\\"'])")
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _decode_backslash_escapes(value: str) -> str | None:
    """Decode the small escape grammar accepted in logged mapping keys."""
    output: list[str] = []
    index = 0
    simple = {
        "\\": "\\",
        '"': '"',
        "'": "'",
        "/": "/",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
    }
    while index < len(value):
        char = value[index]
        if char != "\\":
            output.append(char)
            index += 1
            continue
        if index + 1 >= len(value):
            return None
        escape = value[index + 1]
        if escape in simple:
            output.append(simple[escape])
            index += 2
        elif escape in {"u", "U", "x", "X"}:
            digits = 4 if escape.casefold() == "u" else 2
            encoded = value[index + 2 : index + 2 + digits]
            if len(encoded) != digits or any(item not in _HEX_DIGITS for item in encoded):
                return None
            output.append(chr(int(encoded, 16)))
            index += 2 + digits
        else:
            return None
        if len(output) > _MAX_KEY_LENGTH:
            return None
    decoded = "".join(output)
    return decoded if len(decoded) <= _MAX_KEY_LENGTH else None


def _is_credential_key(value: str, *, quote: str | None = None) -> bool:
    del quote  # Quoted and request-target keys share one normalization contract.
    if len(value) > _MAX_KEY_LENGTH:
        return True
    sensitive = _QUERY_CREDENTIAL_KEYS | {"cer"}
    for _ in range(_MAX_KEY_NORMALIZATION_ROUNDS):
        if len(value) > _MAX_KEY_LENGTH or _INVALID_PERCENT.search(value):
            return True
        percent_decoded = unquote_plus(value)
        decoded = _decode_backslash_escapes(percent_decoded)
        if decoded is None:
            return True
        normalized = unicodedata.normalize("NFKC", decoded).casefold().replace("-", "_")
        if len(normalized) > _MAX_KEY_LENGTH:
            return True
        if normalized in sensitive:
            return True
        if normalized == value:
            return bool(
                _PERCENT_ESCAPE.search(normalized)
                or _INVALID_PERCENT.search(normalized)
                or _RESIDUAL_BACKSLASH_ESCAPE.search(normalized)
            )
        value = normalized
    return bool(
        _PERCENT_ESCAPE.search(value)
        or _INVALID_PERCENT.search(value)
        or _RESIDUAL_BACKSLASH_ESCAPE.search(value)
    )


def _quoted_end(value: str, start: int, quote: str) -> int | None:
    escaped = False
    for index in range(start, len(value)):
        char = value[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == quote:
            return index + 1
    return None


def _redact_credential_assignments(value: str) -> str:
    """Redact credential assignments with bounded, escape-aware scanning.

    Malformed quoted values are hidden through the end of the record. This is
    intentionally lossy: logs are diagnostic output, never a reason to expose
    an ambiguously delimited credential.
    """
    output: list[str] = []
    cursor = 0
    index = 0
    length = len(value)
    while index < length:
        if index and (value[index - 1].isalnum() or value[index - 1] == "_"):
            index += 1
            continue
        if value[index] in {'"', "'"}:
            quote = value[index]
            key_end = _quoted_end(value, index + 1, quote)
            if key_end is None:
                break
            raw_key = value[index + 1 : key_end - 1]
            after_key = key_end
        else:
            quote = None
            after_key = index
            while after_key < length and (value[after_key].isalnum() or value[after_key] in "_-%"):
                after_key += 1
            if after_key == index:
                index += 1
                continue
            raw_key = value[index:after_key]
        separator = after_key
        while separator < length and value[separator].isspace():
            separator += 1
        if separator >= length or value[separator] not in ":=":
            index = max(index + 1, after_key)
            continue
        value_start = separator + 1
        while value_start < length and value[value_start].isspace():
            value_start += 1
        if not _is_credential_key(raw_key, quote=quote):
            index = max(index + 1, value_start)
            continue

        if value_start < length and value[value_start] in {'"', "'"}:
            value_end = _quoted_end(value, value_start + 1, value[value_start])
            if value_end is None:
                value_end = length
        else:
            value_end = value_start
            while value_end < length and value[value_end] not in ",;}]&#\r\n":
                value_end += 1
        output.append(value[cursor:value_start])
        output.append("[REDACTED]")
        cursor = value_end
        index = value_end
    output.append(value[cursor:])
    return "".join(output)


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
        if _is_credential_key(match.group(2)):
            return f"{match.group(1)}{match.group(2)}=[REDACTED]"
        return match.group(0)

    parsed = _QUERY_PARAMETER.sub(replace, value)
    return sanitize_value(_redact_credential_assignments(parsed))


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
            rendered = _redact_credential_assignments(rendered)
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
