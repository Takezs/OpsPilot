"""Unit tests for recursive payload sanitization and length limits."""

import pytest

from opspilot.runs.sanitize import (
    MAX_PAYLOAD_BYTES,
    MAX_STRING_LENGTH,
    REDACTED,
    REDACTED_EMAIL,
    REDACTED_PHONE,
    TRUNCATION_MARKER,
    PayloadInvalidError,
    PayloadTooLargeError,
    sanitize_payload,
)


def test_redacts_authorization_and_api_key_values() -> None:
    payload = {
        "headers": {"Authorization": "Bearer secret-token", "X-Api-Key": "sk-123"},
        "api_key": "nested-key",
        "access_token": "jwt-abc",
    }
    sanitized = sanitize_payload(payload)
    assert sanitized["headers"]["Authorization"] == REDACTED
    assert sanitized["headers"]["X-Api-Key"] == REDACTED
    assert sanitized["api_key"] == REDACTED
    assert sanitized["access_token"] == REDACTED


def test_redacts_combination_key_variants() -> None:
    payload = {
        "client_secret": "cs-1",
        "private_key": "pk-2",
        "session_token": "st-3",
        "id_token": "id-4",
        "proxy_authorization": "Basic dXNlcjpwYXNz",
    }
    sanitized = sanitize_payload(payload)
    assert sanitized["client_secret"] == REDACTED
    assert sanitized["private_key"] == REDACTED
    assert sanitized["session_token"] == REDACTED
    assert sanitized["id_token"] == REDACTED
    assert sanitized["proxy_authorization"] == REDACTED


def test_does_not_redact_non_secret_suffix_fields() -> None:
    payload = {"token_count": 42, "access_level": 1}
    sanitized = sanitize_payload(payload)
    assert sanitized["token_count"] == 42
    assert sanitized["access_level"] == 1


def test_redacts_email_and_phone_in_values() -> None:
    payload = {"note": "contact user@example.com or 13812345678"}
    sanitized = sanitize_payload(payload)
    assert "user@example.com" not in sanitized["note"]
    assert "13812345678" not in sanitized["note"]
    assert REDACTED_EMAIL in sanitized["note"]
    assert REDACTED_PHONE in sanitized["note"]


def test_redacts_bearer_token_in_arbitrary_value() -> None:
    payload = {"raw": "token is Bearer abc.def.ghi"}
    sanitized = sanitize_payload(payload)
    assert "abc.def.ghi" not in sanitized["raw"]
    assert REDACTED in sanitized["raw"]


def test_recurses_into_nested_lists_and_dicts() -> None:
    payload = {
        "outer": [
            {"inner": {"password": "hunter2", "email": "a@b.com"}},
            ["13812345678"],
        ]
    }
    sanitized = sanitize_payload(payload)
    assert sanitized["outer"][0]["inner"]["password"] == REDACTED
    assert "a@b.com" not in sanitized["outer"][0]["inner"]["email"]
    assert "13812345678" not in sanitized["outer"][1][0]


def test_truncates_overlong_strings() -> None:
    long_value = "x" * (MAX_STRING_LENGTH + 500)
    sanitized = sanitize_payload({"text": long_value})
    assert len(sanitized["text"]) == MAX_STRING_LENGTH
    assert sanitized["text"].endswith(TRUNCATION_MARKER)


def test_rejects_oversized_payload() -> None:
    # Many strings each under MAX_STRING_LENGTH so truncation cannot shrink them;
    # the combined serialized size exceeds MAX_PAYLOAD_BYTES.
    payload = {"items": ["a" * 500 for _ in range(MAX_PAYLOAD_BYTES // 250 + 1)]}
    with pytest.raises(PayloadTooLargeError):
        sanitize_payload(payload)


def test_rejects_nan_and_infinity() -> None:
    with pytest.raises(PayloadInvalidError):
        sanitize_payload({"score": float("nan")})
    with pytest.raises(PayloadInvalidError):
        sanitize_payload({"ratio": float("inf")})
    with pytest.raises(PayloadInvalidError):
        sanitize_payload({"nested": {"delta": float("-inf")}})
