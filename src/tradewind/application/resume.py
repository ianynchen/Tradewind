"""Resume planner: decides how a turn reconstructs a session's prior
context when it resumes (FR-10.2), independent of whatever backend/profile
the session currently resolves to (task-9 brief).

Stage-1 scope: `langchain` is the only registered backend and it has
`capabilities().supports_native_resume=False` — every turn already rebuilds
its messages from the mirror unconditionally (`ctx.load_history()`,
component spec "History" decision), so `plan()` always answers `"replay"`
and `TurnRunner` does not consult it yet (there is nothing for it to
branch on). `"native"` (prefer a live native transcript over the mirror)
and `"fresh"` (neither is usable) become reachable once a backend with
`supports_native_resume=True` lands (claude, later tasks) and this
planner starts inspecting `session.backend`/`session.native_session_id`.
"""

from __future__ import annotations

from typing import Literal

from tradewind.domain.models import SessionRow

ResumePlan = Literal["native", "replay", "fresh"]


class ResumePlanner:
    """Decides how a turn should reconstruct `session`'s prior context."""

    def plan(self, session: SessionRow) -> ResumePlan:
        """Always `"replay"` for now (see module docstring).

        `session` is accepted (not discarded via a `noqa`) because a later
        backend's plan legitimately depends on it (`native_session_id`
        presence, backend match) once a `supports_native_resume=True`
        adapter exists to make `"native"`/`"fresh"` reachable.
        """
        del session
        return "replay"
