"""Helpers for the live (env-gated) conformance tests.

A live turn that does not complete must fail the test WITH the turn's own
error (e.g. a 401 from a bad key), not a bare `StopIteration` that looks
like a test bug. That error text comes from a real provider, so any API-key
shaped substring is redacted before it reaches test output.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from tradewind.domain.events import Event, TurnCompleted, TurnFailed
from tradewind.domain.models import TurnResult

# Anthropic API keys: `sk-ant-` followed by key characters.
_ANTHROPIC_KEY = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")


def redact_api_keys(text: str) -> str:
    """`text` with every Anthropic-key-shaped substring replaced."""
    return _ANTHROPIC_KEY.sub("sk-ant-[REDACTED]", text)


def completed_result(events: Sequence[Event]) -> TurnResult:
    """The turn's `TurnCompleted` result.

    Raises:
        AssertionError: the turn failed (message: its redacted error), or
            ended with neither terminal event.
    """
    for event in events:
        if isinstance(event, TurnCompleted):
            return event.result
    failures = [event for event in events if isinstance(event, TurnFailed)]
    if failures:
        raise AssertionError(f"live turn failed: {redact_api_keys(failures[-1].error)}")
    raise AssertionError("live turn ended with neither TurnCompleted nor TurnFailed")
