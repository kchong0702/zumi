import logging
import sys
from functools import partial

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from src.core.config import Settings

_MAX_FIELD_CHARS = 1000


def _add_service(
    service: str, env: str, _: WrappedLogger, __: str, event_dict: EventDict
) -> EventDict:
    """Names the service and environment on every log line"""
    event_dict.setdefault("service", service)
    event_dict.setdefault("env", env)
    return event_dict


def _truncated(value: object) -> object:
    """value with every string longer than _MAX_FIELD_CHARS replaced by its length"""
    if isinstance(value, str) and len(value) > _MAX_FIELD_CHARS:
        return f"<TRUNCATED_LARGE_STRING | len: {len(value)}>"
    if isinstance(value, dict):
        return {key: _truncated(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncated(item) for item in value]
    return value


def _truncate_long_fields(
    _: WrappedLogger, __: str, event_dict: EventDict
) -> EventDict:
    """Cuts long fields, so a payload never floods the logs"""
    return {key: _truncated(value) for key, value in event_dict.items()}


def configure_logging(settings: Settings) -> None:
    """Sends structlog and standard library logs to stderr as JSON or console
    text, at the configured level"""
    observability = settings.observability
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        partial(_add_service, observability.service_name, settings.app.env),
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        _truncate_long_fields,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer()
                if observability.log_format == "json"
                else structlog.dev.ConsoleRenderer(colors=True),
            ],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(observability.log_level)
