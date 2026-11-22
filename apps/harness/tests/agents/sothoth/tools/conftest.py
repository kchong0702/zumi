import threading
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


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
