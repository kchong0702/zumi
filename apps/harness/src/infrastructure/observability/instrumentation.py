from openinference.instrumentation import TraceConfig
from openinference.instrumentation.langchain import LangChainInstrumentor

from src.core.config import Settings

_instrumentor = LangChainInstrumentor()


def instrument(settings: Settings) -> None:
    """Traces LangChain and LangGraph runs through OpenInference when tracing is
    enabled and langchain is listed; a second call does nothing"""
    observability = settings.observability
    if (
        _instrumentor.is_instrumented_by_opentelemetry
        or not observability.tracing_enabled
        or "langchain" not in observability.instrumentations
    ):
        return
    redaction = observability.trace_redaction
    _instrumentor.instrument(config=TraceConfig(**redaction.model_dump()))


def uninstrument() -> None:
    """Stops tracing LangChain and LangGraph runs"""
    if _instrumentor.is_instrumented_by_opentelemetry:
        _instrumentor.uninstrument()
