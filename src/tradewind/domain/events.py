"""Domain events: frozen dataclasses forming the normalized event stream.

Taxonomy frozen at task 11 (ARCHITECTURE §3.2 "Event taxonomy (frozen)",
P-1 resolved): the six members of the `Event` union below, each emitted by
whichever adapter's own turn shape produces it -- see that subsection for
the one-line-per-event summary of what emits what, and why
`PermissionRequested` is deny-only.

`ThinkingDelta` (a streamed reasoning chunk) was removed here: nothing
reaches the code path that would emit it. `ClaudeBackend` has no
delta-streaming path at all (its whole surface is `ItemCompleted`,
including `kind="thinking"` -- the SDK hands thinking blocks over whole,
not incrementally); `LangchainBackend`'s would-be emission site
(`_delta_event`'s `"reasoning"` branch) is unreachable because nothing in
Tradewind's current request construction enables Anthropic extended
thinking on that path, so that branch never sees a `"reasoning"` streaming
chunk. Persisted `kind="thinking"` `ItemCompleted` items are unaffected --
only the live, in-flight delta signal for that content was ever removed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tradewind.domain.models import NormalizedMessage, TurnResult, Verdict


@dataclass(frozen=True)
class TurnStarted:
    turn_id: str


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ItemCompleted:
    message: NormalizedMessage


@dataclass(frozen=True)
class PermissionRequested:
    tool_name: str
    tool_input: dict[str, Any]
    verdict: Verdict
    # The broker's `Denial.reason` (FR-4.4), recorded on EVERY consulted
    # path -- including backends whose engines have no channel to show it
    # to the model (`supports_deny_reason=False`). Additive with a default:
    # existing constructions and equality assertions are untouched.
    reason: str | None = None


@dataclass(frozen=True)
class TurnCompleted:
    result: TurnResult


@dataclass(frozen=True)
class TurnFailed:
    turn_id: str
    error: str


Event = TurnStarted | TextDelta | ItemCompleted | PermissionRequested | TurnCompleted | TurnFailed
