"""Recursive payload sanitization and length limits for the Run Journal.

The design spec requires that Journal, Attempt, log and Trace payloads be
sanitized and length-limited before they are persisted: API keys, Authorization
headers, full emails, phone numbers and other complete PII must never reach
``run_events.payload``. This module centralizes both concerns at the journal
write boundary so callers cannot forget to sanitize.
"""

import json
import re
from typing import Any, cast

# Whole-value redaction markers.
REDACTED = "[REDACTED]"
REDACTED_EMAIL = "[REDACTED_EMAIL]"
REDACTED_PHONE = "[REDACTED_PHONE]"

# Strings longer than this are truncated so no single scalar can balloon.
MAX_STRING_LENGTH = 1000
TRUNCATION_MARKER = "…"

# Overall payload cap, measured on the serialized JSON byte length. Oversized
# payloads are rejected (fail-closed) rather than silently truncated: there is no
# safe way to cut an arbitrary nested structure down to a byte budget without
# corrupting it.
MAX_PAYLOAD_BYTES = 64 * 1024  # 64 KiB

# Keys that mark a whole value (and any nested subtree) as sensitive. Matching
# is case-insensitive and ignores separators, so ``api_key``, ``API-Key`` and
# ``access_token`` all resolve to the same normalized marker.
SENSITIVE_KEY_MARKERS = frozenset(
    {
        "authorization",
        "auth",
        "apikey",
        "apikeys",
        "token",
        "accesstoken",
        "refreshtoken",
        "secret",
        "apisecret",
        "password",
        "passwd",
        "credential",
        "credentials",
        "cookie",
        "setcookie",
        "bearer",
        "xapikey",
    }
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Chinese mobile numbers (11 digits starting 1[3-9]) with digit boundaries so a
# substring inside a longer digit run is not partially matched.
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
# Authorization header values commonly begin with a scheme such as "Bearer " or
# "Basic "; redact the scheme plus credential even when the key is not sensitive.
_AUTH_SCHEME_RE = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)


class PayloadTooLargeError(ValueError):
    """Raised when a sanitized payload exceeds ``MAX_PAYLOAD_BYTES``."""


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _truncate(value: str) -> str:
    if len(value) <= MAX_STRING_LENGTH:
        return value
    return value[: MAX_STRING_LENGTH - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def _sanitize_string(value: str) -> str:
    value = _AUTH_SCHEME_RE.sub(REDACTED, value)
    value = _EMAIL_RE.sub(REDACTED_EMAIL, value)
    value = _PHONE_RE.sub(REDACTED_PHONE, value)
    return _truncate(value)


def sanitize_value(value: Any) -> Any:
    """Recursively redact and truncate a payload value in place of the caller's.

    Sensitive keys redact their entire subtree; emails, phone numbers and
    Authorization scheme credentials are redacted wherever they appear in a
    string; every string is bounded to ``MAX_STRING_LENGTH``.
    """
    if isinstance(value, dict):
        return {
            key: REDACTED if _normalize_key(key) in SENSITIVE_KEY_MARKERS else sanitize_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_value(item) for item in value]
    if isinstance(value, str):
        return _sanitize_string(value)
    return value


def sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Sanitize a payload and enforce its overall byte limit.

    This is the single journal write boundary: redaction, truncation and the
    overall size cap all happen here, before the payload is persisted.
    """
    sanitized = sanitize_value(payload)
    size = len(json.dumps(sanitized, ensure_ascii=False).encode("utf-8"))
    if size > MAX_PAYLOAD_BYTES:
        raise PayloadTooLargeError(
            f"payload size {size} bytes exceeds limit {MAX_PAYLOAD_BYTES} bytes"
        )
    return cast(dict[str, Any], sanitized)
