"""OpenTelemetry SDK initialization for API and worker processes."""

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def build_tracer_provider(
    *, service_name: str, environment: str, endpoint: str | None
) -> TracerProvider:
    """Build a provider; exporter network work stays asynchronous and bounded."""

    provider = TracerProvider(
        resource=Resource.create(
            {
                SERVICE_NAME: service_name,
                "deployment.environment.name": environment,
            }
        )
    )
    if endpoint is not None:
        exporter = OTLPSpanExporter(
            endpoint=endpoint,
            insecure=endpoint.startswith("http://"),
            timeout=5.0,
        )
        provider.add_span_processor(
            BatchSpanProcessor(exporter, export_timeout_millis=5_000)
        )
    return provider


def configure_tracing(
    *, service_name: str, environment: str, endpoint: str | None
) -> TracerProvider:
    """Install the process tracer provider before application/worker startup."""

    provider = build_tracer_provider(
        service_name=service_name,
        environment=environment,
        endpoint=endpoint,
    )
    trace.set_tracer_provider(provider)
    return provider
