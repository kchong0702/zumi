import io
import json
import logging
from collections.abc import Callable, Iterator

import pytest
import structlog

from src.core.config import Settings
from src.infrastructure.observability.logging import configure_logging


@pytest.fixture
def json_logs(make_settings: Callable[..., Settings]) -> Iterator[io.StringIO]:
    """JSON logs written to a buffer, with global logging restored after"""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    buffer = io.StringIO()
    configure_logging(make_settings(log_format="json"))
    root.handlers[-1].setStream(buffer)
    yield buffer
    root.handlers[:] = handlers
    root.setLevel(level)
    structlog.reset_defaults()


def test_logs_name_the_service_and_cut_long_fields_but_keep_tracebacks(
    json_logs: io.StringIO,
):
    try:
        raise ValueError("boom " + "y" * 1000)
    except ValueError:
        structlog.stdlib.get_logger("test").exception(
            "failed", payload="x" * 1001, short="ok"
        )

    line = json.loads(json_logs.getvalue().strip().splitlines()[-1])

    assert (line["service"], line["env"], line["level"]) == ("harness", "dev", "error")
    assert line["payload"] == "<TRUNCATED_LARGE_STRING | len: 1001>"
    assert line["short"] == "ok"
    assert "ValueError: boom yyy" in line["exception"]
    assert len(line["exception"]) > 1000


def test_standard_library_logs_come_out_as_json_too(json_logs: io.StringIO):
    logging.getLogger("stdlib").warning("listening on %s", "8300")

    line = json.loads(json_logs.getvalue().strip().splitlines()[-1])

    assert (line["event"], line["logger"]) == ("listening on 8300", "stdlib")
