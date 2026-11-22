from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

from src.core.config import Settings
from src.infrastructure.observability.otlp import (
    build_endpoint,
    build_resource,
    parse_headers,
)

_provider: MeterProvider | None = None


def configure_metrics(settings: Settings) -> None:
    """Exports metrics over OTLP when metrics are enabled; a second call does
    nothing until shutdown_metrics"""
    global _provider
    observability = settings.observability
    if _provider is not None or not observability.metrics_enabled:
        return
    _provider = MeterProvider(
        resource=build_resource(settings),
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(
                    endpoint=build_endpoint(observability, "metrics"),
                    headers=parse_headers(observability),
                ),
                export_interval_millis=observability.metrics_export_interval_ms,
            )
        ],
    )
    metrics.set_meter_provider(_provider)


def shutdown_metrics() -> None:
    """Flushes pending metrics and shuts down the meter provider, when one is
    configured"""
    global _provider
    if _provider is None:
        return
    _provider.shutdown()
    _provider = None
