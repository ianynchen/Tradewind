"""Tests for tradewind.application.resume.ResumePlanner (task-9 brief)."""

from __future__ import annotations

from dataclasses import replace

from tradewind.application.resume import ResumePlanner
from tradewind.domain.models import SessionRow


def _row(**overrides: object) -> SessionRow:
    base = SessionRow(
        session_id="sess-1",
        backend="langchain",
        profile="default",
        options_snapshot={},
    )
    return replace(base, **overrides) if overrides else base


def test_plan_is_always_replay_for_a_backend_with_no_native_resume() -> None:
    # `langchain` is the only registered backend as of task-9 and its
    # `capabilities().supports_native_resume` is False -- there is no
    # native transcript to prefer over the mirror rebuild every turn
    # already does unconditionally.
    assert ResumePlanner().plan(_row()) == "replay"


def test_plan_is_replay_even_with_a_recorded_native_session_id() -> None:
    # A stale `native_session_id` from a prior backend (`rehome_native`)
    # does not change the answer while no registered backend can act on
    # native resume yet.
    assert ResumePlanner().plan(_row(native_session_id="native-123")) == "replay"
