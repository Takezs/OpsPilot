import secrets

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from opspilot.demo_control import CONTROL_HEADER, control_proof
from opspilot.observability.tracing import configure_tracing, traced_stage


def test_agent_trace_records_only_safe_structured_attributes() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    configure_tracing(provider)
    prompt = "full prompt alice@example.com Bearer secret"

    with traced_stage("agent.run", "run-1", {"prompt": prompt}):
        for stage in (
            "retrieval",
            "rerank",
            "llm",
            "tool.policy",
            "tool.execute",
            "tool.reconcile",
        ):
            with traced_stage(stage, "run-1", {"decision": "allowed"}):
                pass

    spans = exporter.get_finished_spans()
    assert {span.name for span in spans} == {
        "agent.run",
        "retrieval",
        "rerank",
        "llm",
        "tool.policy",
        "tool.execute",
        "tool.reconcile",
    }
    serialized = repr([dict(span.attributes) for span in spans])
    assert prompt not in serialized
    assert "alice@example.com" not in serialized
    assert "Bearer secret" not in serialized
    assert all(span.attributes["run_id"] == "run-1" for span in spans)


def test_trace_export_failure_never_changes_business_result() -> None:
    class BrokenProvider:
        def get_tracer(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("telemetry unavailable")

    configure_tracing(BrokenProvider())  # type: ignore[arg-type]
    with traced_stage("agent.run", "run-2", {}):
        result = "committed"
    assert result == "committed"


def test_trace_records_only_sanitized_exception_and_preserves_business_error() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    configure_tracing(provider)
    raw = "Bearer raw api_key=secret alice@example.com 13800138000 full prompt"

    try:
        with traced_stage("llm", "run-error", {}):
            raise RuntimeError(raw)
    except RuntimeError as error:
        assert str(error) == raw

    spans = exporter.get_finished_spans()
    rendered = repr(
        [
            {
                "attributes": dict(span.attributes),
                "events": [dict(event.attributes or {}) for event in span.events],
            }
            for span in spans
        ]
    )
    assert raw not in rendered
    assert "alice@example.com" not in rendered
    assert "13800138000" not in rendered
    assert spans[0].attributes["exception.type"] == "RuntimeError"
    assert str(spans[0].attributes["exception.summary"]).startswith("sha256:")


def test_exported_span_never_contains_demo_control_credentials() -> None:
    secret = secrets.token_bytes(32)
    signature = control_proof(secret, "POST", "reset", "E2E-" + "c" * 32)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    configure_tracing(provider)
    try:
        try:
            with traced_stage(
                "tool.control",
                "canary-run",
                {
                    "control_secret": secret.hex(),
                    CONTROL_HEADER: signature,
                    "authorization": "Bearer trace-control-canary",
                    "contact": "canary@example.com 13800138000",
                },
            ):
                raise RuntimeError(signature)
        except RuntimeError:
            pass
        provider.force_flush()
        rendered = "".join(span.to_json() for span in exporter.get_finished_spans())
        assert len(exporter.get_finished_spans()) == 1
        assert not any(
            value in rendered
            for value in (
                secret.hex(),
                signature,
                "trace-control-canary",
                "canary@example.com",
                "13800138000",
            )
        )
    finally:
        configure_tracing(None)
