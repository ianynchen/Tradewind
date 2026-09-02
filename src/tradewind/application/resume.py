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

`reconcile()` (task-11 brief, ARCHITECTURE §5.2) is the other half: before a
native-path turn, backfill the mirror with any native transcript activity
Tradewind's own mirror hasn't seen yet (e.g. a human resumed the same
session from the vendor CLI directly, per DR-3) — `TurnRunner.execute`
calls it once per turn, ahead of `ctx.load_history()`, so the backfilled
items are already part of what a backend's rebuilt context sees.
"""

from __future__ import annotations

from typing import Literal

import anyio

from tradewind.application.ports import Backend, SessionStorePort
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

    async def reconcile(
        self, session: SessionRow, backend: Backend, store: SessionStorePort
    ) -> int:
        """Backfill `session`'s mirror with native transcript items added
        since the mirror's own last-known native item (ARCHITECTURE §5.2).

        A no-op (returns 0 without touching `backend` or `store` beyond the
        capability check) when `backend.capabilities().supports_transcript_
        read` is False -- calling `read_native_transcript` on such a
        backend would only raise `Unsupported` (its own contract,
        ports.py), so this checks the flag itself rather than catching that.

        Idempotent: `store.import_native_items` dedupes on `native_id`, so
        calling this again with no new native activity in between always
        returns 0 (task-11 brief's own test shape).

        Returns:
            The number of items actually imported.
        """
        if not backend.capabilities().supports_transcript_read:
            return 0
        after_native_id = await anyio.to_thread.run_sync(store.last_native_id, session.session_id)
        items = await backend.read_native_transcript(session, after_native_id)
        if not items:
            return 0
        return await anyio.to_thread.run_sync(
            store.import_native_items, session.session_id, None, items
        )
