"""Recursive payload sanitization and length limits for the Run Journal.

The design spec requires that Journal, Attempt, log and Trace payloads be
sanitized and length-limited before they are persisted: API keys, Authorization
headers, full emails, phone numbers and other complete PII must never reach
``run_events.payload``. This module centralizes both concerns at the journal
write boundary so callers cannot forget to sanitize.

Validation is explicit and happens during the same recursive walk as redaction:
non-string keys, keys or values containing lone surrogates, unsupported value
types, NaN/Infinity, circular references and excessive nesting all raise
``PayloadInvalidError``, so a bad payload is rejected before the run's sequence
counter is ever touched.
"""

import json
import math
import re
from typing import Any, cast

# Whole-value redaction markers.
REDACTED = "[REDACTED]"
REDACTED_EMAIL = "[REDACTED_EMAIL]"
REDACTED_PHONE = "[REDACTED_PHONE]"

# Strings longer than this are truncated so no single scalar can balloon.
MAX_STRING_LENGTH = 1000
TRUNCATION_MARKER = "…"

# Overall payload cap, measured on the serialized JSON byte length.
MAX_PAYLOAD_BYTES = 64 * 1024  # 64 KiB

# Maximum container nesting depth, kept far below Python's recursion limit so a
# deeply nested payload fails with a clear error instead of a RecursionError.
MAX_DEPTH = 100

# Sensitive nouns that mark a key as sensitive wherever they appear as a token.
# ``token`` and ``key`` carry extra context rules and are handled separately.
STRONG_SENSITIVE_TOKENS = frozenset(
    {
        "secret",
        "secrets",
        "password",
        "passwd",
        "credential",
        "credentials",
        "cookie",
        "cookies",
        "authorization",
    }
)

# A ``token``/``tokens`` token is metadata (not a secret) when immediately
# followed by one of these quantity words: token_count, token_size, ...
TOKEN_METADATA_SUFFIXES = frozenset(
    {"count", "counts", "size", "length", "number", "total", "limit", "index", "position", "offset"}
)

# A ``token``/``tokens`` token is usage metadata (not a secret) when immediately
# followed by the word "usage" (token_usage_count) or immediately preceded by one
# of these usage qualifiers (prompt_tokens, completion_tokens, input_tokens,
# output_tokens, total_tokens).
TOKEN_USAGE_WORD = "usage"
TOKEN_USAGE_QUALIFIERS = frozenset({"prompt", "completion", "input", "output", "total"})

# A ``key``/``keys`` token is a credential only when immediately preceded by one
# of these qualifiers; otherwise it is structural metadata (partition_key,
# document_key, sort_key, cache_key, ...) and is left untouched. A bare ``key``
# with no qualifier is treated as structural, so callers naming a credential must
# qualify it (api_key, private_key, ...).
KEY_CREDENTIAL_QUALIFIERS = frozenset(
    {
        "api",
        "secret",
        "private",
        "public",
        "signing",
        "encryption",
        "access",
        "auth",
        "master",
        "client",
        "account",
        "session",
        "ssh",
        "pgp",
        "gpg",
        "jwt",
        "bearer",
        "refresh",
    }
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Chinese mobile numbers (11 digits starting 1[3-9]) with digit boundaries so a
# substring inside a longer digit run is not partially matched.
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
# Authorization header values commonly begin with a scheme such as "Bearer " or
# "Basic "; redact the scheme plus credential even when the key is not sensitive.
_AUTH_SCHEME_RE = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
# A lone surrogate (D800-DFFF) cannot be encoded as UTF-8 and is rejected by JSONB.
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
# Split a key on separators and camelCase boundaries: "x-api-key" -> x/api/key,
# "apiKey" -> api/key, "APIToken" -> api/token.
_TOKEN_SPLIT_RE = re.compile(
    r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=\d)(?=[A-Za-z])|(?<=[A-Za-z])(?=\d)"
    r"|[^A-Za-z0-9]+"
)


class PayloadError(ValueError):
    """Base class for payload validation failures at the journal boundary."""


class PayloadTooLargeError(PayloadError):
    """Raised when a sanitized payload exceeds ``MAX_PAYLOAD_BYTES``."""


class PayloadInvalidError(PayloadError):
    """Raised when a payload contains a value JSONB cannot represent."""


def _tokenize(key: str) -> list[str]:
    return [token.lower() for token in _TOKEN_SPLIT_RE.split(key) if token]


def _is_sensitive_key(key: str) -> bool:
    """Return True when a key names a credential rather than structural metadata.

    Keys are tokenized by snake/kebab/camelCase boundaries and matched by token
    so that ``client_secret_value``, ``api_key_value`` and
    ``authorization_header`` are caught while ``partition_key``, ``document_key``,
    ``token_count`` and token-usage metadata (``prompt_tokens``,
    ``token_usage_count``) are left alone.
    """
    tokens = _tokenize(key)
    for index, token in enumerate(tokens):
        if token in STRONG_SENSITIVE_TOKENS:
            return True
        if token in ("token", "tokens"):
            preceding = tokens[index - 1] if index > 0 else None
            following = tokens[index + 1] if index + 1 < len(tokens) else None
            if following in TOKEN_METADATA_SUFFIXES:
                continue  # token_count, token_size, token_total
            if following == TOKEN_USAGE_WORD:
                continue  # token_usage_count, token_usage_total
            if preceding in TOKEN_USAGE_QUALIFIERS:
                continue  # prompt_tokens, completion_tokens, total_tokens
            return True
        if token in ("key", "keys"):
            preceding = tokens[index - 1] if index > 0 else None
            if preceding in KEY_CREDENTIAL_QUALIFIERS:
                return True
    return False


def _truncate(value: str) -> str:
    if len(value) <= MAX_STRING_LENGTH:
        return value
    return value[: MAX_STRING_LENGTH - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def _sanitize_string(value: str) -> str:
    value = _AUTH_SCHEME_RE.sub(REDACTED, value)
    value = _EMAIL_RE.sub(REDACTED_EMAIL, value)
    value = _PHONE_RE.sub(REDACTED_PHONE, value)
    value = _truncate(value)
    if _SURROGATE_RE.search(value):
        raise PayloadInvalidError("payload contains a lone surrogate code point")
    return value


def sanitize_value(value: Any, seen: set[int] | None = None, depth: int = 0) -> Any:
    """Recursively validate, redact and truncate a payload value.

    ``seen`` and ``depth`` are internal recursion parameters: ``seen`` tracks the
    identity of containers on the current path to detect circular references,
    and ``depth`` bounds nesting below ``MAX_DEPTH``. Non-string keys, keys or
    values with lone surrogates, unsupported value types, NaN/Infinity, cycles
    and excessive nesting all raise ``PayloadInvalidError`` before the value
    reaches ``json.dumps``.
    """
    if seen is None:
        seen = set()
    if depth > MAX_DEPTH:
        raise PayloadInvalidError(f"payload nesting exceeds {MAX_DEPTH} levels")

    if isinstance(value, dict):
        marker = id(value)
        if marker in seen:
            raise PayloadInvalidError("payload contains a circular reference")
        seen.add(marker)
        try:
            result: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise PayloadInvalidError(
                        f"payload object key must be a string, got {type(key).__name__}"
                    )
                if _SURROGATE_RE.search(key):
                    raise PayloadInvalidError(
                        "payload object key contains a lone surrogate code point"
                    )
                result[key] = (
                    REDACTED if _is_sensitive_key(key) else sanitize_value(item, seen, depth + 1)
                )
            return result
        finally:
            seen.remove(marker)

    if isinstance(value, (list, tuple)):
        marker = id(value)
        if marker in seen:
            raise PayloadInvalidError("payload contains a circular reference")
        seen.add(marker)
        try:
            return [sanitize_value(item, seen, depth + 1) for item in value]
        finally:
            seen.remove(marker)

    if isinstance(value, str):
        return _sanitize_string(value)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise PayloadInvalidError("payload contains NaN or Infinity")
        return value
    raise PayloadInvalidError(f"unsupported payload value type: {type(value).__name__}")


def sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Sanitize a payload and enforce its overall byte limit.

    This is the single journal write boundary: redaction, truncation, JSONB
    serializability and the overall size cap all happen here, before the payload
    is persisted.
    """
    sanitized = sanitize_value(payload)
    try:
        serialized = json.dumps(sanitized, ensure_ascii=False, allow_nan=False)
        size = len(serialized.encode("utf-8"))
    except (TypeError, ValueError) as error:
        raise PayloadInvalidError(f"payload is not JSONB-serializable: {error}") from error
    if size > MAX_PAYLOAD_BYTES:
        raise PayloadTooLargeError(
            f"payload size {size} bytes exceeds limit {MAX_PAYLOAD_BYTES} bytes"
        )
    return cast(dict[str, Any], sanitized)
