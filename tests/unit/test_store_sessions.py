"""Tests for tradewind.adapters.sqlite_store.SqliteSessionStore: schema,
migrations, and the session intent verbs (task-3 brief).
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from tradewind.adapters.sqlite_store import SqliteSessionStore
from tradewind.domain.errors import SessionExists, SessionNotFound
from tradewind.domain.models import SessionRow


def _row(session_id: str = "sess-1", **overrides: object) -> SessionRow:
    base = SessionRow(
        session_id=session_id,
        backend="claude",
        profile="default",
        options_snapshot={"system_prompt": "be helpful"},
    )
    return replace(base, **overrides) if overrides else base


def _user_version(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute("PRAGMA user_version")
        row = cur.fetchone()
        return int(row[0])
    finally:
        conn.close()


# --- migrate: schema lands whole, idempotent (Step 1) ---


def test_migrate_on_fresh_file_sets_current_user_version(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    store = SqliteSessionStore(db_path)
    store.migrate()
    # v2 (FR-5.9): the meta table records the mirror's content-shape version.
    assert _user_version(db_path) == 2


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    store = SqliteSessionStore(db_path)
    store.migrate()
    store.migrate()  # must not raise, must not touch existing data
    assert _user_version(db_path) == 2


# --- create_session: duplicate id raises SessionExists (Step 1) ---


def test_create_session_returns_the_inserted_row(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    store.migrate()
    row = _row()
    result = store.create_session(row)
    assert result == row


def test_create_session_duplicate_id_raises_session_exists(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    store.migrate()
    store.create_session(_row())
    with pytest.raises(SessionExists):
        store.create_session(_row())


def test_get_session_round_trips_all_fields(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    store.migrate()
    row = _row(
        native_session_id="native-abc",
        parent_session_id=None,
        spawn_kind=None,
        spawned_by_message_id=None,
        title="my session",
        cwd="/work",
        system_prompt="be helpful",
        status="active",
        native_meta={"thread": "t1"},
        native_history=[],
        options_snapshot={"tier": "fast", "tools": []},
    )
    store.create_session(row)
    fetched = store.get_session(row.session_id)
    assert fetched == row


# --- get_session: unknown id -> None (Step 1) ---


def test_get_session_unknown_id_returns_none(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    store.migrate()
    assert store.get_session("does-not-exist") is None


# --- ensure_session: get-or-create in one transaction (Step 1) ---


def test_ensure_session_creates_when_absent(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    store.migrate()
    row = _row()
    result = store.ensure_session(row)
    assert result == row
    assert store.get_session(row.session_id) == row


def test_ensure_session_returns_existing_row_unchanged(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    store.migrate()
    original = _row(title="original title")
    store.create_session(original)

    conflicting = _row(title="a different title entirely")
    result = store.ensure_session(conflicting)

    assert result == original
    assert store.get_session(original.session_id) == original


# --- update_options ---


def test_update_options_replaces_snapshot(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    store.migrate()
    row = _row()
    store.create_session(row)

    store.update_options(row.session_id, {"tier": "slow"})

    fetched = store.get_session(row.session_id)
    assert fetched is not None
    assert fetched.options_snapshot == {"tier": "slow"}


def test_update_options_unknown_session_raises_session_not_found(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    store.migrate()
    with pytest.raises(SessionNotFound):
        store.update_options("does-not-exist", {"tier": "slow"})


# --- rehome_native: appends old pair, sets new pair (Step 1) ---


def test_rehome_native_appends_old_pair_and_sets_new_pair(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    store.migrate()
    row = _row(backend="claude", native_session_id="native-old")
    store.create_session(row)

    store.rehome_native(row.session_id, "codex", "native-new")

    fetched = store.get_session(row.session_id)
    assert fetched is not None
    assert fetched.backend == "codex"
    assert fetched.native_session_id == "native-new"
    assert fetched.native_history == [{"backend": "claude", "native_session_id": "native-old"}]


def test_rehome_native_unknown_session_raises_session_not_found(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    store.migrate()
    with pytest.raises(SessionNotFound):
        store.rehome_native("does-not-exist", "codex", "native-new")


# --- content-shape versioning (FR-5.9) ---


def test_fresh_store_records_the_current_content_shape_version(tmp_path: Path) -> None:
    from tradewind.domain.models import CONTENT_SHAPE_VERSION

    db_path = tmp_path / "sessions.db"
    SqliteSessionStore(db_path).migrate()

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'content_shape_version'").fetchone()
    finally:
        conn.close()
    assert row is not None and int(row[0]) == CONTENT_SHAPE_VERSION


def test_v1_store_migrates_forward_to_v2_with_meta(tmp_path: Path) -> None:
    # Simulate a store written before FR-5.9: v1 tables, user_version 1,
    # no meta table. migrate() must carry it forward without touching data.
    db_path = tmp_path / "sessions.db"
    store = SqliteSessionStore(db_path)
    store.migrate()
    store.create_session(_row("sess-1"))
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("DROP TABLE meta")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()

    reopened = SqliteSessionStore(db_path)
    reopened.migrate()

    assert _user_version(db_path) == 2
    assert reopened.get_session("sess-1") is not None


def test_store_written_by_a_newer_tradewind_is_refused(tmp_path: Path) -> None:
    # Refuse-loudly rule: a recorded shape version above this library's
    # constant means a newer writer owns the data -- never risk corrupting
    # shapes this version cannot read.
    from tradewind.domain.errors import ConfigError

    db_path = tmp_path / "sessions.db"
    SqliteSessionStore(db_path).migrate()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE meta SET value = '99' WHERE key = 'content_shape_version'")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ConfigError, match="newer"):
        SqliteSessionStore(db_path).migrate()
