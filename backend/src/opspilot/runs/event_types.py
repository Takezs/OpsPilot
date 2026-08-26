"""Strict event-type contract shared by journal writes and SSE encoding."""

import re

_EVENT_TYPE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z", re.ASCII)


class EventTypeInvalidError(ValueError):
    """Raised when an event type cannot be represented as one safe SSE token."""


def validate_event_type(value: str) -> str:
    if not isinstance(value, str) or _EVENT_TYPE.fullmatch(value) is None:
        raise EventTypeInvalidError("event_type must match ASCII token [A-Za-z0-9_.-]{1,64}")
    return value
