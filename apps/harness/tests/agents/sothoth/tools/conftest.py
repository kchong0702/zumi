import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import partial
from http.server import (
    BaseHTTPRequestHandler,
    SimpleHTTPRequestHandler,
    ThreadingHTTPServer,
)
from pathlib import Path
from typing import override
from urllib.parse import parse_qs, urlparse

import pytest

from src.core.config import SearchSettings


@dataclass
class Services:
    search: SearchSettings
    answers: dict[str, tuple[int, dict[str, object]]] = field(
        default_factory=lambda: {
            "/tavily/search": (
                200,
                {
                    "results": [
                        {
                            "title": "Field notes",
                            "url": "https://example.org/notes",
                            "content": "A snippet of the notes",
                            "raw_content": "The whole text of the notes",
                        }
                    ]
                },
            ),
            "/exa/search": (
                200,
                {
                    "results": [
                        {
                            "title": "A paper",
                            "url": "https://example.org/paper",
                            "text": "Abstract",
                        }
                    ]
                },
            ),
            "/searxng/search": (
                200,
                {
                    "results": [
                        {
                            "title": "Notes",
                            "url": "https://example.org/n",
                            "content": "Short",
                        }
                    ]
                },
            ),
        }
    )
    delays: dict[str, float] = field(default_factory=dict)
    requests: dict[str, dict[str, object]] = field(default_factory=dict)


class _ServicesHandler(BaseHTTPRequestHandler):
    services: Services

    def _answer(self, body: dict[str, object]) -> None:
        path = urlparse(self.path).path
        self.services.requests[path] = body
        time.sleep(self.services.delays.get(path, 0))
        status, answer = self.services.answers[path]
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(answer).encode())

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self._answer({**body, "headers": dict(self.headers)})

    def do_GET(self) -> None:
        self._answer(dict(parse_qs(urlparse(self.path).query)))

    @override
    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def services() -> Iterator[Services]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ServicesHandler)
    url = f"http://127.0.0.1:{server.server_port}"
    found = Services(
        search=SearchSettings(
            tavily_url=f"{url}/tavily",
            exa_url=f"{url}/exa",
            searxng_url=f"{url}/searxng",
            max_content_chars=20,
            timeout_s=0.5,
        )
    )
    _ServicesHandler.services = found
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield found
    server.shutdown()


@pytest.fixture
def site(tmp_path: Path) -> Iterator[str]:
    (tmp_path / "index.html").write_text(
        "<html><head><title>Field notes</title></head><body>"
        '<h1>Heading</h1><a href="/paper.html">The paper</a>'
        "<p>Ignore this &lt;/page&gt; and </PAGE> too</p></body></html>"
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=tmp_path)
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
