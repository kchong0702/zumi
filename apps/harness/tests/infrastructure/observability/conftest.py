from collections.abc import Callable, Iterator, Sequence
from typing import override

import pytest
from opentelemetry import trace
from opentelemetry.metrics import _internal as metrics_api
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricExportResult,
    MetricsData,
)
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.util._once import Once

from src.core.config import Settings
from src.infrastructure.observability import instrumentation, metrics, tracing


class FakeSpanExporter(SpanExporter):
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.spans: list[ReadableSpan] = []

    @override
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    @override
    def shutdown(self) -> None:
        pass


class FakeMetricExporter(MetricExporter):
    def __init__(self, **kwargs: object) -> None:
        super().__init__()
        self.kwargs = kwargs
        self.batches: list[MetricsData] = []

    @override
    def export(
        self,
        metrics_data: MetricsData,
        timeout_millis: float = 10_000,
        **kwargs: object,
    ) -> MetricExportResult:
        self.batches.append(metrics_data)
        return MetricExportResult.SUCCESS

    @override
    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    @override
    def shutdown(self, timeout_millis: float = 30_000, **kwargs: object) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset_providers() -> Iterator[None]:
    """Puts OpenTelemetry's global providers back after each test"""
    yield
    instrumentation.uninstrument()
    tracing.shutdown_tracing()
    metrics.shutdown_metrics()
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE = Once()
    metrics_api._METER_PROVIDER = None
    metrics_api._METER_PROVIDER_SET_ONCE = Once()


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    """Settings from config.toml with these observability settings"""
    return lambda **observability: Settings(
        observability={
            "tracing_enabled": False,
            "metrics_enabled": False,
            "instrumentations": [],
            **observability,
        }
    )


@pytest.fixture
def span_exporters(monkeypatch: pytest.MonkeyPatch) -> list[FakeSpanExporter]:
    created: list[FakeSpanExporter] = []

    def exporter(**kwargs: object) -> FakeSpanExporter:
        created.append(FakeSpanExporter(**kwargs))
        return created[-1]

    monkeypatch.setattr(tracing, "OTLPSpanExporter", exporter)
    return created


@pytest.fixture
def metric_exporters(monkeypatch: pytest.MonkeyPatch) -> list[FakeMetricExporter]:
    created: list[FakeMetricExporter] = []

    def exporter(**kwargs: object) -> FakeMetricExporter:
        created.append(FakeMetricExporter(**kwargs))
        return created[-1]

    monkeypatch.setattr(metrics, "OTLPMetricExporter", exporter)
    return created
