from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

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
