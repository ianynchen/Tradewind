"""Tests for tradewind.application.resume.ResumePlanner.reconcile (task-11
brief): before a native-path turn, backfill the mirror with native
transcript items the mirror hasn't seen yet (ARCHITECTURE §5.2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

from tradewind.adapters.sqlite_store import SqliteSessionStore
from tradewind.application.ports import Backend, TurnContext
from tradewind.application.resume import ResumePlanner
from tradewind.domain.events import Event
from tradewind.domain.models import (
    BackendName,
    Capabilities,
    NormalizedMessage,
    Role,
    SessionRow,
)


def _store(tmp_path: Path) -> SqliteSessionStore:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    store.migrate()
    return store


def _row(**overrides: object) -> SessionRow:
    base = SessionRow(
        session_id="sess-1",
        backend="claude",
        profile="default",
        options_snapshot={},
        native_session_id="native-session-1",
    )
    return replace(base, **overrides) if overrides else base


class _FakeTranscriptBackend(Backend):
    """A `Backend` double whose `read_native_transcript` returns `items`
    after `after_native_id` (mirroring the real per-adapter contract:
    `native_transcript_items`'s own "drop up to and including the matching
    entry" semantics) -- `reconcile` itself is what's under test here, not
    a specific adapter's transcript-mapping logic (that's
    tests/unit/test_claude_mapping.py's job)."""

    name: ClassVar[BackendName] = "claude"

    def __init__(
        self, items: list[NormalizedMessage], *, supports_transcript_read: bool = True
    ) -> None:
        self._items = items
        self._supports_transcript_read = supports_transcript_read
        # Every `after_native_id` this double was actually called with, in
        # order -- lets a test assert reconcile() consulted `store.
        # last_native_id()` rather than some other cursor.
        self.read_calls: list[str | None] = []

    def capabilities(self) -> Capabilities:
        return Capabilities(
            supports_system_prompt=True,
            supports_structured_output=False,
            supports_interactive_permissions=True,
            supports_in_process_tools=True,
            supports_native_resume=True,
            supports_fork=True,
            supports_transcript_read=self._supports_transcript_read,
            supports_tool_round_cap=False,
            supports_turn_retry=False,
        )

    async def run(
        self,
        ctx: TurnContext,  # noqa: ARG002 -- unused here, see docstring
    ) -> AsyncIterator[Event]:  # pragma: no cover -- unused here
        raise NotImplementedError
        yield

    async def probe_native(self, session: SessionRow) -> bool:
        return session.native_session_id is not None

    async def read_native_transcript(
        self, session: SessionRow, after_native_id: str | None
    ) -> list[NormalizedMessage]:
        del session
        self.read_calls.append(after_native_id)
        if after_native_id is None:
            return list(self._items)
        index = next(
            (i for i, it in enumerate(self._items) if it.native_id == after_native_id), None
        )
        return list(self._items[index + 1 :]) if index is not None else list(self._items)

    async def interrupt(self, session_id: str) -> None:  # pragma: no cover -- unused here
        pass


def _msg(native_id: str, text: str, *, role: Role = "assistant") -> NormalizedMessage:
    return NormalizedMessage(role=role, kind="text", content={"text": text}, native_id=native_id)


# --- reconcile: imports the native tail after the mirror's last native_id ---


async def test_reconcile_imports_items_after_the_mirrors_last_native_id(tmp_path: Path) -> None:
    # Mirror already has one item at native-1 (N); the fake backend's full
    # transcript runs native-1..native-4, so reconcile should import exactly
    # the tail: native-2, native-3, native-4 (N+1..N+3, per the brief).
    store = _store(tmp_path)
    store.create_session(_row())
    store.begin_turn("sess-1", "turn-1", None)
    store.append_message("sess-1", "turn-1", _msg("native-1", "hi", role="user"))

    all_items = [
        _msg("native-1", "hi", role="user"),
        _msg("native-2", "a"),
        _msg("native-3", "b"),
        _msg("native-4", "c"),
    ]
    backend = _FakeTranscriptBackend(all_items)
    session = store.get_session("sess-1")
    assert session is not None

    imported = await ResumePlanner().reconcile(session, backend, store)

    assert imported == 3
    assert backend.read_calls == ["native-1"]
    history = store.history("sess-1")
    assert [m.native_id for m in history] == ["native-1", "native-2", "native-3", "native-4"]


async def test_reconcile_second_call_imports_nothing_new(tmp_path: Path) -> None:
    # Calling reconcile again with no new native activity in between must
    # be a no-op (task-11 brief's own test shape: "second call imports 0").
    store = _store(tmp_path)
    store.create_session(_row())
    store.begin_turn("sess-1", "turn-1", None)
    store.append_message("sess-1", "turn-1", _msg("native-1", "hi", role="user"))
    backend = _FakeTranscriptBackend([_msg("native-1", "hi", role="user"), _msg("native-2", "a")])
    planner = ResumePlanner()
    session = store.get_session("sess-1")
    assert session is not None

    first = await planner.reconcile(session, backend, store)
    second = await planner.reconcile(session, backend, store)

    assert first == 1
    assert second == 0
    assert backend.read_calls == ["native-1", "native-2"]


async def test_reconcile_skips_a_backend_without_transcript_read(tmp_path: Path) -> None:
    # `capabilities().supports_transcript_read=False` must short-circuit
    # before ever calling `read_native_transcript` (calling it anyway would
    # raise `Unsupported`, per that method's own contract, ports.py).
    store = _store(tmp_path)
    store.create_session(_row())
    backend = _FakeTranscriptBackend(
        [_msg("native-1", "hi", role="user")], supports_transcript_read=False
    )
    session = store.get_session("sess-1")
    assert session is not None

    imported = await ResumePlanner().reconcile(session, backend, store)

    assert imported == 0
    assert backend.read_calls == []


async def test_reconcile_with_no_prior_native_id_reads_from_the_start(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_session(_row())
    backend = _FakeTranscriptBackend([_msg("native-1", "hi", role="user"), _msg("native-2", "a")])
    session = store.get_session("sess-1")
    assert session is not None

    imported = await ResumePlanner().reconcile(session, backend, store)

    assert imported == 2
    assert backend.read_calls == [None]
