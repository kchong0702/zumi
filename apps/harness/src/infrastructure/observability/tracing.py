from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from src.core.config import Settings
from src.infrastructure.observability.otlp import (
    build_endpoint,
    build_resource,
    parse_headers,
)

_provider: TracerProvider | None = None


def configure_tracing(settings: Settings) -> None:
    """Exports spans over OTLP when tracing is enabled; a second call does nothing
    until shutdown_tracing"""
    global _provider
    observability = settings.observability
    if _provider is not None or not observability.tracing_enabled:
        return
    _provider = TracerProvider(resource=build_resource(settings))
    _provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=build_endpoint(observability, "traces"),
                headers=parse_headers(observability),
            )
        )
    )
    trace.set_tracer_provider(_provider)


def shutdown_tracing() -> None:
    """Flushes and shuts down the tracer provider, when one is configured"""
    global _provider
    if _provider is None:
        return
    _provider.shutdown()
    _provider = None
