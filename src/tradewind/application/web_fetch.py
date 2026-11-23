"""Tradewind's own fetch tool: one read-only page fetch, served on every
backend that can serve Tradewind's tools (docs/components/07-web-access.md
section 5). Standard library only; the blocking call runs in a worker thread.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from html.parser import HTMLParser

import anyio

from tradewind.domain.models import Tool

FETCH_TOOL_NAME = "tradewind_fetch_url"
FETCH_TIMEOUT_S = 20
FETCH_MAX_BYTES = 1_000_000

_REFUSED = "Only http and https URLs can be fetched."


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None  # the 3xx then surfaces as an HTTPError


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, _attrs: object) -> None:
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return re.sub(r"\n\s*\n+", "\n\n", "".join(parser.parts)).strip()


def _fetch_blocking(url: str) -> str:
    if not url.lower().startswith(("http://", "https://")):
        return _REFUSED
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(url, timeout=FETCH_TIMEOUT_S) as resp:
            ctype = resp.headers.get_content_type()
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read(FETCH_MAX_BYTES + 1)
    except urllib.error.HTTPError as err:
        if 300 <= err.code < 400:
            return f"Redirect {err.code} to {err.headers.get('Location', '')}; not followed."
        raise
    if not ctype.startswith("text/"):
        return f"Not a text page ({ctype})."
    cut = len(raw) > FETCH_MAX_BYTES
    text = raw[:FETCH_MAX_BYTES].decode(charset, errors="replace")
    if ctype == "text/html":
        text = _html_to_text(text)
    return f"{text}\n[truncated]" if cut else text


async def fetch_url(url: str) -> str:
    return await anyio.to_thread.run_sync(_fetch_blocking, url)


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
