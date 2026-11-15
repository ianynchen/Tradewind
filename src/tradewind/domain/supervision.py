"""Limit arithmetic for the work supervisor (05-turn-governance §3.2).

Pure functions over plain values: no clock, no I/O. All times are seconds
on the supervisor's *active* clock -- a monotonic clock that stops while a
broker question is open -- so a pause never needs special arithmetic here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

LimitName = Literal["model_silence", "work_silence", "hard_cap"]
WorkKind = Literal["tool", "background", "subagent"]


@dataclass(frozen=True)
class Limits:
    """The limits of one turn, in seconds (all positive).

    `model_silence_s` applies while no work is open, `work_silence_s`
    while work is open, `hard_cap_s` to the total active time; `None`
    means no hard cap."""

    model_silence_s: float
    work_silence_s: float
    hard_cap_s: float | None


@dataclass(frozen=True)
class WorkItem:
    """One open piece of engine work a backend reported through
    `ctx.work.started(...)`. `has_end_signal=False` marks work the engine
    never reports as ended (e.g. `agy` subagents, §3.3)."""

    id: str
    kind: WorkKind
    label: str
    has_end_signal: bool = True


@dataclass(frozen=True)
class SupervisorState:
    """A snapshot of what the supervisor knows, on the active clock.

    `last_model_at`/`last_work_at` are the latest model and work signals
    (the turn start until the first one). `reports_work=False` means the
    backend sends no signals at all, so only the hard cap can apply."""

    started_at: float
    last_model_at: float
    last_work_at: float
    has_open_work: bool
    reports_work: bool
    pending_approvals: int


def next_deadline(
    now: float, state: SupervisorState, limits: Limits
) -> tuple[float | None, LimitName | None]:
    """The time the next limit fires and its name, or `(None, None)`.

    Returns `(None, None)` while a broker question is open (all limits
    pause, §3.2) or when no limit applies. A deadline already passed is
    returned as `now`: the limit fires at once. When two limits fall at
    the same time, the silence limit is named before the hard cap.
    """
    if state.pending_approvals > 0:
        return None, None
    candidates: list[tuple[float, LimitName]] = []
    if state.reports_work:
        if state.has_open_work:
            candidates.append((state.last_work_at + limits.work_silence_s, "work_silence"))
        else:
            candidates.append((state.last_model_at + limits.model_silence_s, "model_silence"))
    if limits.hard_cap_s is not None:
        candidates.append((state.started_at + limits.hard_cap_s, "hard_cap"))
    if not candidates:
        return None, None
    deadline, name = min(candidates, key=lambda candidate: candidate[0])
    return max(deadline, now), name
