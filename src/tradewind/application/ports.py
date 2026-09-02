"""Application ports: interfaces the domain-facing services depend on,
implemented by adapters (GUIDELINES §8: dependencies flow inward).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from tradewind.domain.models import BackendName, SessionRow


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
