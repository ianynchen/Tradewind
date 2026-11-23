"""Tradewind's own fetch tool, against a local HTTP server (no network)."""

from __future__ import annotations

import threading
import time
import urllib.error
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tradewind.application import web_fetch
from tradewind.application.web_fetch import FETCH_MAX_BYTES, fetch_tool

pytestmark = pytest.mark.integration


class _Server:
    def __init__(self) -> None:
        self.requests: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.requests.append(self.path)
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", f"{outer.base}/secret")
                    self.end_headers()
                    return
                if self.path == "/drip":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    try:
                        for _ in range(100):
                            self.wfile.write(b"x")
                            self.wfile.flush()
                            time.sleep(0.1)
                    except OSError:
                        pass
                    return
                if self.path == "/notmod":
                    self.send_response(304)
                    self.end_headers()
                    return
                ctype, body = {
                    "/charset": ("text/plain; charset=foo", b"caf\xc3\xa9"),
                    "/blocks": ("text/html", b"<ul><li>a</li><li>b</li></ul>"),
                    "/page": ("text/html; charset=utf-8", b"<script>x()</script><p>Hello</p>"),
                    "/big": ("text/plain", b"a" * (FETCH_MAX_BYTES + 10)),
                    "/png": ("image/png", b"\x89PNG"),
                }[self.path]
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture
def server() -> Iterator[_Server]:
    srv = _Server()
    yield srv
    srv.httpd.shutdown()
    srv.httpd.server_close()


async def _fetch(url: str) -> str:
    return str(await fetch_tool().handler(url=url))


async def test_fetch_returns_the_page_text(server: _Server) -> None:
    text = await _fetch(f"{server.base}/page")
    assert "Hello" in text
    assert "x()" not in text


async def test_a_redirect_is_not_followed(server: _Server) -> None:
    text = await _fetch(f"{server.base}/redirect")
    assert text == f"Redirect 302 to {server.base}/secret; not followed."
    assert server.requests == ["/redirect"]


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "example.com"])
async def test_other_schemes_are_refused(server: _Server, url: str) -> None:
    assert await _fetch(url) == "Only http and https URLs can be fetched."
    assert server.requests == []


async def test_a_large_page_is_cut(server: _Server) -> None:
    assert (await _fetch(f"{server.base}/big")).endswith("[truncated]")


async def test_a_binary_page_is_not_returned(server: _Server) -> None:
    assert await _fetch(f"{server.base}/png") == "Not a text page (image/png)."


async def test_a_slow_drip_stops_at_the_total_deadline(
    server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(web_fetch, "FETCH_TIMEOUT_S", 0.5)
    start = time.monotonic()
    text = await _fetch(f"{server.base}/drip")
    assert time.monotonic() - start < 3
    assert text.startswith("x") and text.endswith("[truncated]")


async def test_an_unknown_charset_falls_back_to_utf8(server: _Server) -> None:
    assert await _fetch(f"{server.base}/charset") == "café"


async def test_block_tags_separate_text(server: _Server) -> None:
    assert (await _fetch(f"{server.base}/blocks")).split() == ["a", "b"]
    assert "ab" not in await _fetch(f"{server.base}/blocks")


async def test_a_3xx_without_location_is_an_error_not_a_redirect(server: _Server) -> None:
    with pytest.raises(urllib.error.HTTPError):
        await _fetch(f"{server.base}/notmod")
