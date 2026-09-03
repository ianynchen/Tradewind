"""Resume planner: decides how a turn reconstructs a session's prior
context when it resumes (FR-10.2), independent of whatever backend/profile
the session currently resolves to (task-9 brief).

Wired since Phase 4 (P-7 closed): `TurnRunner.execute` consults
`plan()` every turn — `"native"` resumes by id, `"replay"` suppresses a
stale/lost native id so the adapter rebuilds (langchain) or injects a
rendered mirror transcript into a fresh native session (SDK backends),
and `"fresh"` starts clean. Reactive degrades (a resume error the adapter
classifies as native-store loss) reach the same REPLAY machinery
in-adapter without re-consulting the planner.

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

    def plan(self, session: SessionRow, *, backend_name: str, probe_ok: bool | None) -> ResumePlan:
        """Route the turn's context source (FR-6.1, wired since Phase 4 --
        P-7's "no callers" note is closed):

        - `"native"`: same backend, native id present, and the probe did
          not say the native store is gone (`probe_ok` True, or None when
          no probe ran).
        - `"replay"`: a native id exists but is unusable -- the session
          was recorded under a DIFFERENT backend (cross-backend
          continuation, FR-10.2) or the probe found the native store
          lost. The runner suppresses the stale id and the adapter
          rebuilds/injects from the mirror; reactive in-adapter degrades
          (a resume error classified as loss) reach the same path without
          re-consulting this planner.
        - `"fresh"`: no native id recorded -- the first native turn, or a
          mirror-only backend's normal state.

        """
        if session.native_session_id is None:
            return "fresh"
        if session.backend != backend_name or probe_ok is False:
            return "replay"
        return "native"

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
