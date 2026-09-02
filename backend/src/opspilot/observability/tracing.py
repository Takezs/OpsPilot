"""Fail-safe OpenTelemetry spans containing only sanitized structured metadata."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import TracerProvider

from opspilot.observability.redaction import safe_attributes

_provider: TracerProvider | None = None


def configure_tracing(provider: TracerProvider | None) -> None:
    global _provider
    _provider = provider


@contextmanager
def traced_stage(
    name: str, run_id: str, attributes: Mapping[str, object] | None = None
) -> Iterator[None]:
    """Create a span when available; telemetry failures never affect business work."""
    try:
        tracer = (_provider or trace.get_tracer_provider()).get_tracer("opspilot")
        span_context: Any = tracer.start_as_current_span(name)
    except BaseException:
        yield
        return
    try:
        span = span_context.__enter__()
        try:
            span.set_attribute("run_id", run_id)
            for key, value in safe_attributes(attributes or {}).items():
                if isinstance(value, (str, bool, int, float)):
                    span.set_attribute(key, value)
        except BaseException:
            pass
    except BaseException:
        yield
        return
    try:
        yield
    except BaseException as business_error:
        try:
            span_context.__exit__(
                type(business_error), business_error, business_error.__traceback__
            )
        except BaseException:
            pass
        raise
    else:
        try:
            span_context.__exit__(None, None, None)
        except BaseException:
            pass
