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


def test_plan_routing_table() -> None:
    # FR-6.1, wired since Phase 4 (P-7 closed): the full routing table.
    planner = ResumePlanner()
    # No native id recorded: fresh (a mirror-only backend's normal state,
    # or a native backend's first turn).
    assert planner.plan(_row(), backend_name="langchain", probe_ok=None) == "fresh"
    # Same backend, id present, no probe ran or probe passed: native.
    row = _row(native_session_id="native-123")
    assert planner.plan(row, backend_name=row.backend, probe_ok=None) == "native"
    assert planner.plan(row, backend_name=row.backend, probe_ok=True) == "native"
    # Probe found the native store gone: replay (degrade, never data loss).
    assert planner.plan(row, backend_name=row.backend, probe_ok=False) == "replay"
    # Cross-backend continuation (FR-10.2): replay regardless of probe.
    assert (
        planner.plan(
            row, backend_name="claude" if row.backend != "claude" else "codex", probe_ok=None
        )
        == "replay"
    )
