"""Tradewind's own fetch tool: one read-only page fetch, served on every
backend that can serve Tradewind's tools (docs/components/07-web-access.md
section 5). Standard library only; the blocking call runs in a worker thread.
"""

from __future__ import annotations

import codecs
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

import anyio
import certifi

from tradewind.domain.models import Tool

FETCH_TOOL_NAME = "tradewind_fetch_url"
FETCH_TIMEOUT_S = 20
FETCH_MAX_BYTES = 1_000_000

_REFUSED = "Only http and https URLs can be fetched."


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None  # the 3xx then surfaces as an HTTPError


_BLOCK_TAGS = frozenset(
    [
        "p",
        "div",
        "li",
        "br",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "tr",
        "table",
        "section",
        "article",
        "header",
        "footer",
        "pre",
        "blockquote",
    ]
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, _attrs: object) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return re.sub(r"\n\s*\n+", "\n\n", "".join(parser.parts)).strip()


def _decode(raw: bytes, charset: str) -> str:
    try:
        codecs.lookup(charset)
    except LookupError:
        charset = "utf-8"
    return raw.decode(charset, errors="replace")


def _is_text(ctype: str) -> bool:
    return (
        ctype.startswith("text/")
        or ctype in ("application/json", "application/xml")
        or ctype.endswith(("+json", "+xml"))
    )


def _fetch_blocking(url: str) -> str:
    if not url.lower().startswith(("http://", "https://")):
        return _REFUSED
    # The default trust store fails on macOS; certifi is the bundle httpx and the SDKs use.
    tls = ssl.create_default_context(cafile=certifi.where())
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=tls))
    deadline = time.monotonic() + FETCH_TIMEOUT_S
    try:
        with opener.open(url, timeout=FETCH_TIMEOUT_S) as resp:
            ctype = resp.headers.get_content_type()
            if not _is_text(ctype):
                return f"Not a text page ({ctype})."  # refused before the body is read
            charset = resp.headers.get_content_charset() or "utf-8"
            chunks: list[bytes] = []
            size = 0
            timed_out = False
            # The socket timeout is per operation; this loop makes it a total limit.
            while size <= FETCH_MAX_BYTES:
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                chunk = resp.read1(min(8192, FETCH_MAX_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            raw = b"".join(chunks)
    except urllib.error.HTTPError as err:
        try:
            location = err.headers.get("Location")
            if 300 <= err.code < 400 and location:
                return (
                    f"Redirect {err.code} to {urllib.parse.urljoin(url, location)}; not followed."
                )
        finally:
            err.close()
        raise
    cut = timed_out or len(raw) > FETCH_MAX_BYTES
    text = _decode(raw[:FETCH_MAX_BYTES], charset)
    if ctype in ("text/html", "application/xhtml+xml"):
        text = _html_to_text(text)
    return f"{text}\n[truncated]" if cut else text


async def fetch_url(url: str) -> str:
    return await anyio.to_thread.run_sync(_fetch_blocking, url, abandon_on_cancel=True)


def fetch_tool() -> Tool:
    return Tool(
        name=FETCH_TOOL_NAME,
        description="Fetch a web page over http or https and return its text. Redirects are not followed.",
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "The http(s) URL to fetch."}},
            "required": ["url"],
        },
        handler=fetch_url,
    )
