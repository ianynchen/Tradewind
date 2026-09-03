"""Application ports: interfaces the domain-facing services depend on,
implemented by adapters (GUIDELINES §8: dependencies flow inward).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from tradewind.application.tool_host import ToolHost
from tradewind.domain.events import Event
from tradewind.domain.models import (
    BackendName,
    Capabilities,
    ModelSpec,
    NormalizedMessage,
    PermissionBroker,
    Profile,
    SessionRow,
    StoredMessage,
    TurnStatus,
    TurnUsage,
)

if TYPE_CHECKING:
    # Deferred to a TYPE_CHECKING-only import to break the runtime cycle:
    # `application.config` imports `SessionStorePort` from this module, so a
    # top-level import here of `NativeStoreConfig` (declared in config.py)
    # would import-loop. `Backend.__init__` is a plain ABC method (not a
    # pydantic model), so its annotation never needs runtime resolution —
    # `from __future__ import annotations` already makes it a lazily
    # evaluated string, and this guard keeps that string resolvable for
    # static type checkers without ever executing at import time.
    from tradewind.application.config import (
        CompactionSettings,
        NativeStoreConfig,
        RetrySettings,
    )


class SessionStorePort(ABC):
    """Storage boundary for session rows.

    Sync by design: the client layer wraps calls with `anyio.to_thread`
    rather than this port carrying async methods (task-3 brief).
    """

    @abstractmethod
    def migrate(self) -> None:
        """Apply pending schema migrations up to the current version.

        Idempotent: calling this repeatedly on the same store is a no-op
        once the store is already at the current version. Implementations
        also own their equivalent of the mirror CONTENT-shape check
        (FR-5.9): refuse a store recorded at a shape version newer than
        `tradewind.domain.models.CONTENT_SHAPE_VERSION`.
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
        turn's); an implementation must serialize concurrent callers against
        this read-then-write so two appends never collide on the same
        `seq` (`SqliteSessionStore` does this with its own single
        in-process `threading.Lock` around its one shared connection, not a
        database transaction). This contract assumes a single process owns
        write access to a given store -- a second OS process writing to the
        same store concurrently is not coordinated against.

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

    @abstractmethod
    def turn_usages(self, session_id: str) -> list[TurnUsage]:
        """Read back the session's per-turn accounting rows (FR-5.9
        companion), ordered by turn seq: turn id, terminal status, token
        usage as finalized, and the turn's own `cost_usd` (reported or
        computed -- FR-10.5; never includes summarizer spend, which lives
        on compaction records).

        Returns:
            The rows, `[]` when `session_id` is unknown (matching
            `history()`'s read semantics).
        """
        ...

    @abstractmethod
    def history(
        self,
        session_id: str,
        *,
        include_children: bool = False,
        include_raw: bool = False,
        after_seq: int | None = None,
        limit: int | None = None,
    ) -> list[StoredMessage]:
        """Read back a session's message log.

        `include_children=False` (default): flat read of `session_id`'s own
        messages only, ordered by `seq`; `after_seq`/`limit` apply here for
        cursor pagination.

        `include_children=True`: tree read — `session_id` plus every
        descendant session (transitively, via `parent_session_id`, per I-1),
        ordered `(session_id, seq)` (ARCHITECTURE §4.2). `after_seq` and
        `limit` are ignored in this mode (not yet supported).

        `include_raw=False` (default): `raw_json` is not selected at all
        (NFR-1) and every returned `StoredMessage.raw` is `None`, even when
        a row has raw content stored. `include_raw=True` selects and
        decodes it.
        """
        ...

    @abstractmethod
    def copy_history(
        self, src_session_id: str, dst_row: SessionRow, up_to_seq: int | None = None
    ) -> SessionRow:
        """Fork `src_session_id`'s message history into a new session.

        Creates `dst_row.session_id` as a session row, forcing
        `spawn_kind='fork'` and `parent_session_id=src_session_id`
        regardless of what `dst_row` carries for those two fields. Copies
        `src_session_id`'s messages with `seq <= up_to_seq` (all of them
        when `up_to_seq` is None), preserving order, into the new session
        with freshly assigned `seq` starting at 1 and `turn_id=None`.

        Returns:
            The created session row (reflecting the forced `spawn_kind` /
            `parent_session_id`).

        Failure modes:
            SessionNotFound: `src_session_id` does not exist.
            SessionExists: `dst_row.session_id` already exists.
        """
        ...

    @abstractmethod
    def import_native_items(
        self, session_id: str, turn_id: str | None, items: list[NormalizedMessage]
    ) -> int:
        """Bulk-insert `items` into `session_id`'s message log, deduping on
        `native_id` against messages already persisted *before* this call
        (idempotency across repeated calls, e.g. `ResumePlanner.reconcile()`
        re-running with an unchanged cursor) -- not within `items` itself:
        several items in one call may legitimately share one `native_id`
        (one native transcript entry producing several normalized items,
        e.g. a thinking block plus a tool_use block from one assistant
        turn) and all of them are inserted, not just the first.

        An item whose `native_id` already existed in the store before this
        call is skipped; items with `native_id=None` are always inserted.
        Inserted items are assigned `seq` after the session's current tail,
        preserving `items` order.

        Returns:
            The number of items actually inserted.

        Failure modes:
            SessionNotFound: `session_id` does not exist.
        """
        ...

    @abstractmethod
    def last_native_id(self, session_id: str) -> str | None:
        """The `native_id` of `session_id`'s highest-`seq` message that has
        one.

        Returns:
            That `native_id`, or None when no message in the session has a
            non-null `native_id` (including when `session_id` is unknown).
        """
        ...


@dataclass
class TurnContext:
    """Everything a `Backend.run()` needs for one turn, assembled by the
    turn runner (a later task) from `SessionOptions`/config — the backend
    itself never touches the store (task-8 brief).

    `load_history` is an async, LAZY closure over the store's `history()`
    verb, pre-bound to this call's `session_id`, its `history_scope`
    (FR-9.3), and `include_raw=False` (mirror reconstruction never needs
    raw provider payloads — NFR-1). The store is not touched until a
    backend actually awaits it — backends that resume natively never do,
    so their turns cost no history read at all. The returned list excludes
    this turn's own prompt (the backend appends `ctx.prompt` itself), and
    under scope "tree" each descendant session arrives folded into one
    wrapped `kind="text"` block at its spawn position.
    """

    session: SessionRow
    turn_id: str
    prompt: str
    model_spec: ModelSpec
    system_prompt: str | None
    output_schema: dict[str, object] | None
    tools: ToolHost
    broker: PermissionBroker
    load_history: Callable[[], Awaitable[list[StoredMessage]]]
    # Mirror-compaction settings for this turn (FR-5.8), from
    # `TradewindConfig.defaults.compaction`. Consumed only by
    # mirror-rebuilding backends (langchain); None disables automatic
    # compaction entirely (test contexts, and a safe default for direct
    # `TurnContext` construction).
    compaction: CompactionSettings | None = None
    # Retry policy for this turn (FR-6.6), from
    # `TradewindConfig.defaults.retry`; None disables retrying (test
    # contexts, and the safe default for direct construction). Consumed at
    # full strength only by `supports_turn_retry` backends; SDK adapters
    # apply it to their pre-turn connect/spawn step only.
    retry: RetrySettings | None = None
    # The session's last recorded turn accounting (FR-5.8 trigger upgrade,
    # via `SessionStorePort.turn_usages`): provider-reported tokens beat
    # the chars/4 estimate where available. Populated only for
    # mirror-rebuilding backends (the runner's `feeds_mirror_context`
    # gate), so native-resume turns still cost zero store reads.
    last_turn_usage: dict[str, int] | None = None
    last_turn_id: str | None = None
    # Per-call cap on tool-execution rounds (FR-6.5): None means uncapped
    # (today's behavior). A backend whose
    # `capabilities().supports_tool_round_cap` is False raises `Unsupported`
    # when this is set — same capability-mismatch handling as
    # `output_schema`. A capped turn that hits the cap ends with
    # `TurnCompleted` carrying `end_reason="max_tool_rounds"`, an honest
    # partial — never `failed`, never a fake clean finish.
    max_tool_rounds: int | None = None


class Backend(ABC):
    """One provider's turn-execution adapter (ARCHITECTURE §3.1). Subclasses
    own everything provider-specific — request shape, streaming, tool-loop
    mechanics — behind this one port; `Tradewind`/`Session` depend on this
    interface only, never on a concrete backend (GUIDELINES §8).

    `name` identifies which `BackendName` this class implements; a
    concrete subclass sets it as a class attribute.
    """

    name: ClassVar[BackendName]

    def __init__(self, profile: Profile, native_config: NativeStoreConfig) -> None:
        self.profile = profile
        self.native_config = native_config

    @abstractmethod
    def capabilities(self) -> Capabilities:
        """This backend's fixed capability flags (FR-8.1).

        Constant for the class — never varies per call or per session.
        Callers (and this backend itself) raise `Unsupported` where these
        flags deny a capability that was asked for (R-1).
        """
        ...

    @abstractmethod
    def run(self, ctx: TurnContext) -> AsyncIterator[Event]:
        """Execute one turn, yielding the normalized event stream.

        `TurnStarted` is always the first event. On a clean finish,
        `TurnCompleted` is the last event; on an unrecoverable error,
        `TurnFailed` is the last event instead. `interrupt()` called for
        `ctx.session.session_id` while this iterator is in flight ends it
        early with neither — no exception escapes, and the iterator simply
        stops (the turn runner, not this method, assigns the terminal
        `interrupted`/`cancelled` status).
        """
        ...

    @abstractmethod
    async def probe_native(self, session: SessionRow) -> bool:
        """Whether `session` has a live native (backend-side) transcript
        distinct from the mirror, that `read_native_transcript` could read.

        Backends with no native store of record
        (`capabilities().supports_native_resume` is False) always return
        False.
        """
        ...

    @abstractmethod
    async def read_native_transcript(
        self, session: SessionRow, after_native_id: str | None
    ) -> list[NormalizedMessage]:
        """Read `session`'s native transcript items after `after_native_id`
        (None reads from the start of the native transcript).

        Failure modes:
            Unsupported: `capabilities().supports_transcript_read` is False.
        """
        ...

    @abstractmethod
    async def interrupt(self, session_id: str) -> None:
        """Cancel the in-flight `run()` call for `session_id`, if any.

        A no-op when no turn is currently in flight for `session_id`.
        """
        ...
