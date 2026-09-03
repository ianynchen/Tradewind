"""Tests for tradewind.adapters.sqlite_store.SqliteSessionStore: turns,
mirror-writer message log, and the single-flight invariant (task-4 brief).
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from tradewind.adapters.sqlite_store import SqliteSessionStore
from tradewind.domain.errors import SessionNotFound, TurnInProgress
from tradewind.domain.models import NormalizedMessage, SessionRow


def _row(session_id: str = "sess-1", **overrides: object) -> SessionRow:
    base = SessionRow(
        session_id=session_id,
        backend="claude",
        profile="default",
        options_snapshot={"system_prompt": "be helpful"},
    )
    return replace(base, **overrides) if overrides else base


def _store_with_session(tmp_path: Path, session_id: str = "sess-1") -> SqliteSessionStore:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    store.migrate()
    store.create_session(_row(session_id))
    return store


def _message(**overrides: object) -> NormalizedMessage:
    base = NormalizedMessage(role="user", kind="text", content={"text": "hi"})
    return replace(base, **overrides) if overrides else base


# --- begin_turn: single-flight invariant (I-5) (Step 1) ---


def test_begin_turn_twice_without_finalize_raises_turn_in_progress(tmp_path: Path) -> None:
    store = _store_with_session(tmp_path)
    store.begin_turn("sess-1", "turn-1", None)
    with pytest.raises(TurnInProgress):
        store.begin_turn("sess-1", "turn-2", None)


def test_begin_turn_unknown_session_raises_session_not_found(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    store.migrate()
    with pytest.raises(SessionNotFound):
        store.begin_turn("does-not-exist", "turn-1", None)


def test_begin_turn_after_finalize_succeeds(tmp_path: Path) -> None:
    store = _store_with_session(tmp_path)
    store.begin_turn("sess-1", "turn-1", None)
    store.finalize_turn(
        "turn-1", status="completed", final_text="done", usage=None, cost_usd=None, error=None
    )
    store.begin_turn("sess-1", "turn-2", None)  # must not raise


# --- append_message: seq assigned per session across turns (Step 1) ---


def test_append_message_assigns_increasing_seq_per_session(tmp_path: Path) -> None:
    store = _store_with_session(tmp_path)
    store.begin_turn("sess-1", "turn-1", None)

    seq1 = store.append_message("sess-1", "turn-1", _message())
    seq2 = store.append_message("sess-1", "turn-1", _message())

    store.finalize_turn(
        "turn-1", status="completed", final_text=None, usage=None, cost_usd=None, error=None
    )
    store.begin_turn("sess-1", "turn-2", None)
    seq3 = store.append_message("sess-1", "turn-2", _message())

    assert (seq1, seq2, seq3) == (1, 2, 3)


def test_append_message_mismatched_turn_session_raises_value_error(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    store.migrate()
    store.create_session(_row("sess-1"))
    store.create_session(_row("sess-2"))
    store.begin_turn("sess-1", "turn-1", None)

    with pytest.raises(ValueError):
        store.append_message("sess-2", "turn-1", _message())


# --- finalize_turn: persists status/usage/cost/final_text (Step 1) ---


def test_finalize_turn_persists_status_usage_cost_final_text(tmp_path: Path) -> None:
    store = _store_with_session(tmp_path)
    store.begin_turn("sess-1", "turn-1", None)

    store.finalize_turn(
        "turn-1",
        status="completed",
        final_text="the answer",
        usage={"input_tokens": 10, "output_tokens": 5},
        cost_usd=0.002,
        error=None,
    )

    conn = sqlite3.connect(str(tmp_path / "sessions.db"))
    try:
        row = conn.execute(
            "SELECT status, final_text, usage_json, cost_usd, completed_at "
            "FROM turns WHERE turn_id = ?",
            ("turn-1",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    status, final_text, usage_json, cost_usd, completed_at = row
    assert status == "completed"
    assert final_text == "the answer"
    assert usage_json == '{"input_tokens": 10, "output_tokens": 5}'
    assert cost_usd == 0.002
    assert completed_at is not None


def test_finalize_turn_unknown_turn_id_raises_value_error(tmp_path: Path) -> None:
    store = _store_with_session(tmp_path)
    with pytest.raises(ValueError):
        store.finalize_turn(
            "does-not-exist",
            status="completed",
            final_text=None,
            usage=None,
            cost_usd=None,
            error=None,
        )


# --- sweep_stale_turns: crash recovery (Step 1) ---


def test_sweep_stale_turns_flips_in_progress_to_failed_and_returns_count(tmp_path: Path) -> None:
    store = _store_with_session(tmp_path)
    store.begin_turn("sess-1", "turn-1", None)

    swept = store.sweep_stale_turns("sess-1")

    assert swept == 1
    conn = sqlite3.connect(str(tmp_path / "sessions.db"))
    try:
        row = conn.execute(
            "SELECT status, error_json FROM turns WHERE turn_id = ?", ("turn-1",)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "failed"
    assert row[1] is not None


def test_sweep_stale_turns_ignores_completed_turns(tmp_path: Path) -> None:
    store = _store_with_session(tmp_path)
    store.begin_turn("sess-1", "turn-1", None)
    store.finalize_turn(
        "turn-1", status="completed", final_text=None, usage=None, cost_usd=None, error=None
    )

    assert store.sweep_stale_turns("sess-1") == 0


# --- crash simulation: mirror log survives process restart (Step 1) ---


def test_crash_simulation_history_readable_and_stale_turn_swept_after_restart(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "sessions.db"
    store = SqliteSessionStore(db_path)
    store.migrate()
    store.create_session(_row("sess-1"))
    store.begin_turn("sess-1", "turn-1", None)
    store.append_message("sess-1", "turn-1", _message(content={"text": "before crash"}))

    # Simulate a process crash: drop the store object without finalizing the
    # turn, then construct a brand-new store on the same file (I-5 recovery).
    del store
    restarted = SqliteSessionStore(db_path)
    restarted.migrate()

    conn = sqlite3.connect(str(db_path))
    try:
        messages = conn.execute(
            "SELECT role, content_json FROM messages WHERE session_id = ? ORDER BY seq",
            ("sess-1",),
        ).fetchall()
    finally:
        conn.close()
    assert len(messages) == 1
    assert messages[0][0] == "user"

    swept = restarted.sweep_stale_turns("sess-1")
    assert swept == 1

    fetched_turn_status = sqlite3.connect(str(db_path)).execute(
        "SELECT status FROM turns WHERE turn_id = ?", ("turn-1",)
    )
    assert fetched_turn_status.fetchone()[0] == "failed"


# --- turn_usages (FR-5.9 companion verb) ---


def test_turn_usages_returns_rows_ordered_by_turn_seq(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    store.migrate()
    store.create_session(_row("sess-1"))
    store.begin_turn("sess-1", "turn-1", None)
    store.finalize_turn(
        "turn-1",
        status="completed",
        final_text="one",
        usage={"input_tokens": 10, "output_tokens": 5, "service_tier": "std"},
        cost_usd=0.01,
        error=None,
    )
    store.begin_turn("sess-1", "turn-2", None)
    store.finalize_turn(
        "turn-2", status="failed", final_text=None, usage=None, cost_usd=None, error="boom"
    )

    usages = store.turn_usages("sess-1")

    assert [u.turn_id for u in usages] == ["turn-1", "turn-2"]
    assert usages[0].status == "completed"
    # Non-int usage values are dropped (TurnUsage.usage is dict[str, int]).
    assert usages[0].usage == {"input_tokens": 10, "output_tokens": 5}
    assert usages[0].cost_usd == 0.01
    assert usages[1].status == "failed"
    assert usages[1].usage == {}
    assert usages[1].cost_usd is None


def test_turn_usages_unknown_session_is_empty(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    store.migrate()

    assert store.turn_usages("no-such") == []
