"""Tests for tradewind.adapters.sqlite_store.SqliteSessionStore: schema,
migrations, and the session intent verbs (task-3 brief).
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from tradewind.adapters.sqlite_store import SqliteSessionStore
from tradewind.domain.errors import ConfigError, SessionExists, SessionNotFound
from tradewind.domain.models import SessionRow


def _row(session_id: str = "sess-1", **overrides: object) -> SessionRow:
    base = SessionRow(
        session_id=session_id,
        backend="claude",
        profile="default",
        options_snapshot={"system_prompt": "be helpful"},
    )
    return replace(base, **overrides) if overrides else base


def _applied_migration_ids(db_path: Path) -> list[str]:
    """The ids yoyo recorded in tradewind's OWN bookkeeping table."""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT migration_id FROM _tradewind_yoyo_migrations ORDER BY migration_id"
        ).fetchall()
        return [str(row[0]) for row in rows]
    finally:
        conn.close()


def _domain_schema(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Every table/index tradewind itself defines, with its DDL -- yoyo's
    bookkeeping and sqlite internals excluded, so a file store and an
    in-memory store are comparable."""
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type IN ('table', 'index') "
        "AND name NOT LIKE '%yoyo%' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [(str(name), str(sql)) for name, sql in rows]


# --- migrate: schema lands whole, idempotent, and upgrades old stores ---


def test_migrate_on_fresh_file_records_every_migration_as_applied(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    store = SqliteSessionStore(db_path)

    store.migrate()

    # Ids are namespaced `tradewind` so a co-embedded library's own
    # `0001_schema` can never be mistaken for ours (the failure that would
    # otherwise be SILENT: yoyo skips an id it believes is applied).
    assert _applied_migration_ids(db_path) == [
        "0001_tradewind_schema",
        "0002_tradewind_meta",
    ]


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    store = SqliteSessionStore(db_path)
    store.migrate()
    store.migrate()  # must not raise, must not touch existing data
    assert _applied_migration_ids(db_path) == [
        "0001_tradewind_schema",
        "0002_tradewind_meta",
    ]


def test_migrate_upgrades_a_store_left_at_an_older_migration(tmp_path: Path) -> None:
    """The scenario migrations exist for, and the one the old
    hand-stepped `user_version` code had no test for: a store written by an
    EARLIER tradewind that only knew `0001` must reach the current schema.
    """
    from yoyo import get_backend, read_migrations

    from tradewind.adapters.sqlite_store import _MIGRATION_TABLE, _MIGRATIONS_DIR

    db_path = tmp_path / "sessions.db"
    backend = get_backend(f"sqlite:///{db_path}", migration_table=_MIGRATION_TABLE)
    migrations = read_migrations(str(_MIGRATIONS_DIR))
    with backend.lock():
        backend.apply_migrations(backend.to_apply(migrations)[:1])
    assert _applied_migration_ids(db_path) == ["0001_tradewind_schema"]

    SqliteSessionStore(db_path).migrate()

    assert _applied_migration_ids(db_path) == [
        "0001_tradewind_schema",
        "0002_tradewind_meta",
    ]
    fresh = SqliteSessionStore(tmp_path / "fresh.db")
    fresh.migrate()
    upgraded = SqliteSessionStore(db_path)
    assert _domain_schema(upgraded._conn) == _domain_schema(fresh._conn)


def test_in_memory_store_schema_matches_a_migrated_file_store(tmp_path: Path) -> None:
    """The two application paths (yoyo for files, direct `.sql` replay for
    `:memory:`, which yoyo cannot reach because an in-memory database is
    private to its connection) must produce an identical schema -- they
    read the same files, and this pins it."""
    file_store = SqliteSessionStore(tmp_path / "sessions.db")
    file_store.migrate()
    memory_store = SqliteSessionStore(Path(":memory:"))
    memory_store.migrate()

    assert _domain_schema(memory_store._conn) == _domain_schema(file_store._conn)


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


def test_older_store_migrates_forward_to_meta_without_touching_data(tmp_path: Path) -> None:
    # Simulate a store written before FR-5.9: the schema migration applied,
    # the meta migration not (expressed in yoyo's bookkeeping since the
    # 2026-09-03 adoption -- `PRAGMA user_version` is no longer
    # authoritative). migrate() must carry it forward AND leave rows alone.
    db_path = tmp_path / "sessions.db"
    store = SqliteSessionStore(db_path)
    store.migrate()
    store.create_session(_row("sess-1"))
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("DROP TABLE meta")
        conn.execute("DELETE FROM _tradewind_yoyo_migrations WHERE migration_id LIKE '0002%'")
        conn.commit()
    finally:
        conn.close()

    reopened = SqliteSessionStore(db_path)
    reopened.migrate()

    assert _applied_migration_ids(db_path) == [
        "0001_tradewind_schema",
        "0002_tradewind_meta",
    ]
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


def test_content_shape_version_older_than_supported_is_refused(tmp_path: Path) -> None:
    """The direction that used to fall through SILENTLY: a store holding
    older content shapes must not be read as if it held current ones just
    because the TABLE schema is up to date (FR-5.9; the two axes are
    orthogonal)."""
    db_path = tmp_path / "sessions.db"
    SqliteSessionStore(db_path).migrate()
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE meta SET value = '0' WHERE key = 'content_shape_version'")
    conn.commit()
    conn.close()

    with pytest.raises(ConfigError, match="predates"):
        SqliteSessionStore(db_path).migrate()
