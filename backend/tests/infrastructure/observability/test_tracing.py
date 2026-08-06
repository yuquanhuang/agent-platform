"""OpenTelemetry provider configuration tests."""

from packages.infrastructure.observability.tracing import build_tracer_provider


def test_tracer_provider_can_run_without_external_exporter() -> None:
    provider = build_tracer_provider(
        service_name="api-test",
        environment="test",
        endpoint=None,
    )

    tracer = provider.get_tracer("agent-platform-test")
    with tracer.start_as_current_span("test") as span:
        assert span.get_span_context().is_valid

    provider.shutdown()
