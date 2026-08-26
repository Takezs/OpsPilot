"""Structured provider failure classification for reliable execution."""

from enum import StrEnum

from opspilot.tools.types import ToolEffect, ToolResult


class ProviderFailureKind(StrEnum):
    CONNECTION_ERROR = "connection_error"
    RATE_LIMITED = "rate_limited"
    RETRYABLE_5XX = "retryable_5xx"
    READ_TIMEOUT = "read_timeout"
    DISCONNECTED_AFTER_SEND = "disconnected_after_send"
    UNKNOWN_5XX = "unknown_5xx"
    PERMANENT = "permanent"


class FailureDisposition(StrEnum):
    RETRY = "retry"
    FAIL = "fail"
    OUTCOME_UNKNOWN = "outcome_unknown"


_READ_ONLY_RETRYABLE = frozenset(
    {
        ProviderFailureKind.CONNECTION_ERROR,
        ProviderFailureKind.RATE_LIMITED,
        ProviderFailureKind.RETRYABLE_5XX,
    }
)


def classify_failure(effect: ToolEffect, result: ToolResult) -> FailureDisposition:
    """Classify without inspecting untrusted error text."""
    if result.ok:
        raise ValueError("successful ToolResult has no failure disposition")
    if effect is ToolEffect.SIDE_EFFECT:
        if result.provider_not_called is True:
            return FailureDisposition.RETRY
        return FailureDisposition.OUTCOME_UNKNOWN
    if result.failure_kind in _READ_ONLY_RETRYABLE:
        return FailureDisposition.RETRY
    return FailureDisposition.FAIL
