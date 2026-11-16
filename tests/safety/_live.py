"""Shared helpers for the live safety suite (not a test module).

`turn` streams one real turn and records a failure instead of raising it,
so a control run can inspect it. `auth_failure` decides whether a failed
turn failed on authentication; only word-bounded, auth-specific phrases
count, so "OAuth", "max tokens" or "invalid_request_error" are not
evidence. `redact` keeps deliberately invalid credentials out of every
message.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterable
from dataclasses import dataclass

from tradewind.application.client import Session
from tradewind.domain.events import (
    Event,
    ItemCompleted,
    PermissionRequested,
    TurnCompleted,
    TurnFailed,
)
from tradewind.domain.models import CustomizationDecision, TurnResult

# `\b` cannot stand before "/", so "/login" has its own branch.
AUTH_FAILURE = re.compile(
    r"\b(?:401|unauthori[sz]ed|authenticat\w*|api[ _-]?key"
    r"|invalid[_ ]?(?:api[_ ]?key|token)|access token)\b"
    r"|/login\b",
    re.IGNORECASE,
)
_MASKED = re.compile(r"\S*\*{3,}\S*")


@dataclass
class Outcome:
    events: list[Event]
    result: TurnResult | None
    error: str | None

    def describe(self) -> str:
        lines = [f"result: {self.result!r}", f"error: {self.error!r}"]
        lines += [
            repr(e)[:400]
            for e in self.events
            if isinstance(e, PermissionRequested | TurnFailed | ItemCompleted)
        ]
        return "\n".join(lines)


async def turn(
    session: Session, prompt: str, customizations: CustomizationDecision | None = None
) -> Outcome:
    """One streamed turn. A failure is recorded, not raised. Prints the
    engine version of each completed turn."""
    events: list[Event] = []
    error: str | None = None
    try:
        # Closed at once, also on a cancel, so the engine process ends.
        async with contextlib.aclosing(
            session.stream(prompt, customizations=customizations)
        ) as stream:
            async for event in stream:
                events.append(event)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    result: TurnResult | None = None
    for event in events:
        if isinstance(event, TurnCompleted):
            result = event.result
            print(f"turn engine_version: {result.engine_version}")
        elif isinstance(event, TurnFailed):
            error = event.error
    return Outcome(events=events, result=result, error=error)


def redact(text: str, values: Iterable[str]) -> str:
    """Drop each value and any masked form of a credential (`sk-ab***wxyz`)."""
    for value in values:
        text = text.replace(value, "<redacted>")
    return _MASKED.sub("<masked>", text)


def auth_failure(outcome: Outcome) -> str | None:
    """The failure text that names an auth problem, or None. Only a failed
    turn counts: an error, or a result that did not complete."""
    texts: list[str] = []
    if outcome.error:
        texts.append(outcome.error)
    if outcome.result is not None and outcome.result.status != "completed":
        texts.append(outcome.result.final_text or "")
    for text in texts:
        if AUTH_FAILURE.search(text):
            return text
    return None
