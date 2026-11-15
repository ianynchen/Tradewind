"""Helpers for the live (env-gated) conformance tests.

A live turn that does not complete must fail the test WITH the turn's own
error (e.g. a 401 from a bad key), not a bare `StopIteration` that looks
like a test bug. That error text comes from a real provider, so any API-key
shaped substring is redacted before it reaches test output.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace

import pytest

from tradewind.domain.events import Event, TurnCompleted, TurnFailed
from tradewind.domain.models import TurnResult

# Anthropic API keys: `sk-ant-` followed by key characters.
_ANTHROPIC_KEY = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")


def redact_api_keys(text: str) -> str:
    """`text` with every Anthropic-key-shaped substring replaced."""
    return _ANTHROPIC_KEY.sub("sk-ant-[REDACTED]", text)


async def collect_events(stream: AsyncIterator[Event]) -> list[Event]:
    """Drain a live turn's event stream, redacting each `TurnFailed` error
    AS IT ARRIVES. No test frame then ever holds the unredacted text --
    which matters because `--full-trace` makes pytest print every frame's
    locals (the test's `events` included), ignoring `__tracebackhide__` and
    `pytrace=False`."""
    events: list[Event] = []
    async for event in stream:
        if isinstance(event, TurnFailed):
            event = replace(event, error=redact_api_keys(event.error))
        events.append(event)
    return events


def completed_result(events: Sequence[Event]) -> TurnResult:
    """The turn's `TurnCompleted` result.

    Fails the test (`pytest.fail`, so `pytest.fail.Exception`) when the turn
    failed -- message: its redacted error -- or ended with neither terminal
    event. `pytrace=False` and `__tracebackhide__` keep pytest from printing
    a traceback at all: its frames would show `events` (and the caller's
    locals under `--showlocals`) with the error. `--full-trace` overrides
    both, which is why live tests collect through `collect_events`, so the
    `events` any frame holds are already redacted.
    """
    __tracebackhide__ = True
    for event in events:
        if isinstance(event, TurnCompleted):
            return event.result
    failures = [event for event in events if isinstance(event, TurnFailed)]
    if failures:
        pytest.fail(f"live turn failed: {redact_api_keys(failures[-1].error)}", pytrace=False)
    pytest.fail("live turn ended with neither TurnCompleted nor TurnFailed", pytrace=False)
