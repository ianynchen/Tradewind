"""Domain events: frozen dataclasses forming the normalized event stream.

Taxonomy is DRAFT until Task 11 / P-1.
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
class ThinkingDelta:
    text: str


@dataclass(frozen=True)
class ItemCompleted:
    message: NormalizedMessage


@dataclass(frozen=True)
class PermissionRequested:
    tool_name: str
    tool_input: dict[str, Any]
    verdict: Verdict


@dataclass(frozen=True)
class TurnCompleted:
    result: TurnResult


@dataclass(frozen=True)
class TurnFailed:
    turn_id: str
    error: str


Event = (
    TurnStarted
    | TextDelta
    | ThinkingDelta
    | ItemCompleted
    | PermissionRequested
    | TurnCompleted
    | TurnFailed
)
