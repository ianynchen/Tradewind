"""Application ports: interfaces the domain-facing services depend on,
implemented by adapters (GUIDELINES §8: dependencies flow inward).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from tradewind.domain.models import BackendName, NormalizedMessage, SessionRow, TurnStatus


class SessionStorePort(ABC):
    """Storage boundary for session rows.

    Sync by design: the client layer wraps calls with `anyio.to_thread`
    rather than this port carrying async methods (task-3 brief).
    """

    @abstractmethod
    def migrate(self) -> None:
        """Apply pending schema migrations up to the current version.

        Idempotent: calling this repeatedly on the same store is a no-op
        once the store is already at the current version.
        """
        ...

    @abstractmethod
    def create_session(self, row: SessionRow) -> SessionRow:
        """Insert a new session row and return it.

        Failure modes:
            SessionExists: `row.session_id` already exists.
        """
        ...

    @abstractmethod
    def get_session(self, session_id: str) -> SessionRow | None:
        """Look up a session by id.

        Returns:
            The session row, or None when `session_id` is unknown.
        """
        ...

    @abstractmethod
    def ensure_session(self, row: SessionRow) -> SessionRow:
        """Get-or-create `row.session_id` in one transaction.

        Returns the existing row unchanged when `row.session_id` already
        exists (the passed-in `row` is otherwise ignored); inserts and
        returns `row` when it does not.
        """
        ...

    @abstractmethod
    def update_options(self, session_id: str, snapshot: dict[str, object]) -> None:
        """Replace the stored options snapshot for `session_id`.

        Failure modes:
            SessionNotFound: `session_id` does not exist.
        """
        ...

    @abstractmethod
    def rehome_native(
        self,
        session_id: str,
        backend: BackendName,
        native_session_id: str | None,
    ) -> None:
        """Re-home a session onto a new `(backend, native_session_id)` pair.

        The superseded pair is appended to `native_history` (append-only)
        before the new pair is set, so the old native transcript stays
        locatable (ARCHITECTURE §4).

        Failure modes:
            SessionNotFound: `session_id` does not exist.
        """
        ...

    @abstractmethod
    def begin_turn(self, session_id: str, turn_id: str, native_turn_id: str | None) -> None:
        """Open a new turn on `session_id`, enforcing the single-flight
        invariant (I-5: one in-flight turn per session).

        Inserts the turn row with `status = 'in_progress'` and `started_at`
        set to now; `seq` is `1 + max(seq)` over the session's existing
        turns.

        Failure modes:
            SessionNotFound: `session_id` does not exist.
            TurnInProgress: `session_id` already has a turn with
                `status = 'in_progress'`.
        """
        ...

    @abstractmethod
    def append_message(self, session_id: str, turn_id: str | None, msg: NormalizedMessage) -> int:
        """Mirror-write `msg` into the session's message log and return its
        assigned `seq`.

        `seq` is `1 + max(seq)` over the *session's* messages (not the
        turn's), assigned inside the write transaction so concurrent
        appends never collide.

        Failure modes:
            ValueError: `turn_id` names a turn that exists but belongs to a
                different session.
        """
        ...

    @abstractmethod
    def finalize_turn(
        self,
        turn_id: str,
        *,
        status: TurnStatus,
        final_text: str | None,
        usage: dict[str, object] | None,
        cost_usd: float | None,
        error: str | None,
    ) -> None:
        """Close `turn_id`, persisting its terminal `status`, `final_text`,
        `usage`, `cost_usd`, and `error`, and setting `completed_at` to now.

        Failure modes:
            ValueError: `turn_id` does not exist. (Not `SessionNotFound`:
                the turn, not the session, is the thing that's missing.)
        """
        ...

    @abstractmethod
    def sweep_stale_turns(self, session_id: str) -> int:
        """Flip every `in_progress` turn on `session_id` to `failed`
        (crash recovery: a process restart mid-turn leaves no writer to
        finalize it), recording a note in `error_json`.

        Returns:
            The number of turns swept.
        """
        ...
