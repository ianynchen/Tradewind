"""SQLite adapter for `SessionStorePort` (ARCHITECTURE §4: session schema).

Full schema (sessions/turns/messages, all indexes) lands in migration v1
so `PRAGMA user_version = 1` denotes a complete store. Session verbs
(`create_session`, `ensure_session`, `update_options`, `rehome_native`)
and turn/message verbs (`begin_turn`, `append_message`, `finalize_turn`,
`sweep_stale_turns`) both live here. One `sqlite3.Connection` is shared
(`check_same_thread=False`) and every method serializes through a single
`threading.Lock` so compound read-then-write sequences (e.g.
`ensure_session`, `rehome_native`, `begin_turn`) stay atomic under
concurrent callers.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from tradewind.application.ports import SessionStorePort
from tradewind.domain.errors import SessionExists, SessionNotFound, TurnInProgress
from tradewind.domain.models import (
    BackendName,
    NormalizedMessage,
    SessionRow,
    SpawnKind,
    TurnStatus,
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id            TEXT PRIMARY KEY,
  backend                TEXT NOT NULL,
  profile                TEXT NOT NULL,
  native_session_id      TEXT,
  parent_session_id      TEXT REFERENCES sessions(session_id),
  spawn_kind             TEXT,
  spawned_by_message_id  INTEGER,
  title                  TEXT,
  cwd                    TEXT,
  model                  TEXT,
  system_prompt          TEXT,
  options_json           TEXT NOT NULL,
  status                 TEXT NOT NULL DEFAULT 'active',
  created_at             TEXT NOT NULL,
  updated_at             TEXT NOT NULL,
  native_meta_json       TEXT,
  native_history_json    TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_native ON sessions(backend, native_session_id);

CREATE TABLE IF NOT EXISTS turns (
  turn_id        TEXT PRIMARY KEY,
  session_id     TEXT NOT NULL REFERENCES sessions(session_id),
  native_turn_id TEXT,
  seq            INTEGER NOT NULL,
  status         TEXT NOT NULL,
  final_text     TEXT,
  usage_json     TEXT,
  cost_usd       REAL,
  started_at     TEXT,
  completed_at   TEXT,
  error_json     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_native ON turns(session_id, native_turn_id);

CREATE TABLE IF NOT EXISTS messages (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id       TEXT NOT NULL REFERENCES sessions(session_id),
  turn_id          TEXT REFERENCES turns(turn_id),
  seq              INTEGER NOT NULL,
  role             TEXT NOT NULL,
  kind             TEXT NOT NULL,
  content_json     TEXT NOT NULL,
  native_id        TEXT,
  parent_native_id TEXT,
  agent_path       TEXT,
  model            TEXT,
  created_at       TEXT,
  raw_json         TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_session_seq ON messages(session_id, seq);
"""

_SESSION_COLUMNS = (
    "session_id, backend, profile, native_session_id, parent_session_id, "
    "spawn_kind, spawned_by_message_id, title, cwd, system_prompt, "
    "options_json, status, native_meta_json, native_history_json"
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_session_row(row: tuple[object, ...]) -> SessionRow:
    """Map a `_SESSION_COLUMNS`-ordered row tuple to a `SessionRow`."""
    native_meta_raw = cast("str | None", row[12])
    native_meta: dict[str, object] | None = None
    if native_meta_raw is not None:
        native_meta = cast("dict[str, object]", json.loads(native_meta_raw))
    return SessionRow(
        session_id=cast(str, row[0]),
        backend=cast(BackendName, row[1]),
        profile=cast(str, row[2]),
        options_snapshot=cast("dict[str, object]", json.loads(cast(str, row[10]))),
        native_session_id=cast("str | None", row[3]),
        parent_session_id=cast("str | None", row[4]),
        spawn_kind=cast("SpawnKind | None", row[5]),
        spawned_by_message_id=cast("int | None", row[6]),
        title=cast("str | None", row[7]),
        cwd=cast("str | None", row[8]),
        system_prompt=cast("str | None", row[9]),
        status=cast(str, row[11]),
        native_meta=native_meta,
        native_history=cast("list[dict[str, object]]", json.loads(cast(str, row[13]))),
    )


class SqliteSessionStore(SessionStorePort):
    """`SessionStorePort` backed by a single SQLite file."""

    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._lock = threading.Lock()

    def migrate(self) -> None:
        with self._lock:
            cur = self._conn.execute("PRAGMA user_version")
            version_row = cast("tuple[object, ...] | None", cur.fetchone())
            version = cast(int, version_row[0]) if version_row is not None else 0
            if version < 1:
                self._conn.executescript(_SCHEMA_SQL)
                self._conn.execute("PRAGMA user_version = 1")
                self._conn.commit()

    def _select_session_row(self, session_id: str) -> SessionRow | None:
        cur = self._conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        row = cast("tuple[object, ...] | None", cur.fetchone())
        if row is None:
            return None
        return _row_to_session_row(row)

    def _insert_session(self, row: SessionRow) -> None:
        options_snapshot = cast("dict[str, object]", row.options_snapshot)
        native_meta = cast("dict[str, object] | None", row.native_meta)
        native_history = cast("list[dict[str, object]]", row.native_history)
        now = _now_iso()
        self._conn.execute(
            f"INSERT INTO sessions ({_SESSION_COLUMNS}, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.session_id,
                row.backend,
                row.profile,
                row.native_session_id,
                row.parent_session_id,
                row.spawn_kind,
                row.spawned_by_message_id,
                row.title,
                row.cwd,
                row.system_prompt,
                json.dumps(options_snapshot),
                row.status,
                json.dumps(native_meta) if native_meta is not None else None,
                json.dumps(native_history),
                now,
                now,
            ),
        )

    def create_session(self, row: SessionRow) -> SessionRow:
        with self._lock:
            if self._select_session_row(row.session_id) is not None:
                raise SessionExists(row.session_id)
            self._insert_session(row)
            self._conn.commit()
        return row

    def get_session(self, session_id: str) -> SessionRow | None:
        with self._lock:
            return self._select_session_row(session_id)

    def ensure_session(self, row: SessionRow) -> SessionRow:
        with self._lock:
            existing = self._select_session_row(row.session_id)
            if existing is not None:
                return existing
            self._insert_session(row)
            self._conn.commit()
        return row

    def update_options(self, session_id: str, snapshot: dict[str, object]) -> None:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE sessions SET options_json = ?, updated_at = ? WHERE session_id = ?",
                (json.dumps(snapshot), _now_iso(), session_id),
            )
            if cur.rowcount == 0:
                self._conn.rollback()
                raise SessionNotFound(session_id)
            self._conn.commit()

    def rehome_native(
        self,
        session_id: str,
        backend: BackendName,
        native_session_id: str | None,
    ) -> None:
        with self._lock:
            existing = self._select_session_row(session_id)
            if existing is None:
                raise SessionNotFound(session_id)
            history = [
                *cast("list[dict[str, object]]", existing.native_history),
                {"backend": existing.backend, "native_session_id": existing.native_session_id},
            ]
            self._conn.execute(
                "UPDATE sessions SET backend = ?, native_session_id = ?, "
                "native_history_json = ?, updated_at = ? WHERE session_id = ?",
                (backend, native_session_id, json.dumps(history), _now_iso(), session_id),
            )
            self._conn.commit()

    def begin_turn(self, session_id: str, turn_id: str, native_turn_id: str | None) -> None:
        with self._lock:
            if self._select_session_row(session_id) is None:
                raise SessionNotFound(session_id)
            cur = self._conn.execute(
                "SELECT 1 FROM turns WHERE session_id = ? AND status = 'in_progress' LIMIT 1",
                (session_id,),
            )
            in_progress_row = cast("tuple[object, ...] | None", cur.fetchone())
            if in_progress_row is not None:
                raise TurnInProgress(session_id)
            cur = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM turns WHERE session_id = ?",
                (session_id,),
            )
            max_seq_row = cast("tuple[object, ...]", cur.fetchone())
            seq = cast(int, max_seq_row[0]) + 1
            self._conn.execute(
                "INSERT INTO turns (turn_id, session_id, native_turn_id, seq, status, started_at) "
                "VALUES (?, ?, ?, ?, 'in_progress', ?)",
                (turn_id, session_id, native_turn_id, seq, _now_iso()),
            )
            self._conn.commit()

    def append_message(self, session_id: str, turn_id: str | None, msg: NormalizedMessage) -> int:
        with self._lock:
            if turn_id is not None:
                cur = self._conn.execute(
                    "SELECT session_id FROM turns WHERE turn_id = ?", (turn_id,)
                )
                owner_row = cast("tuple[object, ...] | None", cur.fetchone())
                if owner_row is not None and owner_row[0] != session_id:
                    raise ValueError(
                        f"turn {turn_id!r} belongs to session {owner_row[0]!r}, not {session_id!r}"
                    )
            cur = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM messages WHERE session_id = ?",
                (session_id,),
            )
            max_seq_row = cast("tuple[object, ...]", cur.fetchone())
            seq = cast(int, max_seq_row[0]) + 1
            content = cast("dict[str, object]", msg.content)
            raw = cast("dict[str, object] | None", msg.raw)
            self._conn.execute(
                "INSERT INTO messages (session_id, turn_id, seq, role, kind, content_json, "
                "native_id, parent_native_id, agent_path, model, created_at, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    turn_id,
                    seq,
                    msg.role,
                    msg.kind,
                    json.dumps(content),
                    msg.native_id,
                    msg.parent_native_id,
                    msg.agent_path,
                    msg.model,
                    _now_iso(),
                    json.dumps(raw) if raw is not None else None,
                ),
            )
            self._conn.commit()
            return seq

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
        with self._lock:
            cur = self._conn.execute(
                "UPDATE turns SET status = ?, final_text = ?, usage_json = ?, cost_usd = ?, "
                "completed_at = ?, error_json = ? WHERE turn_id = ?",
                (
                    status,
                    final_text,
                    json.dumps(usage) if usage is not None else None,
                    cost_usd,
                    _now_iso(),
                    json.dumps(error) if error is not None else None,
                    turn_id,
                ),
            )
            if cur.rowcount == 0:
                self._conn.rollback()
                raise ValueError(f"unknown turn_id: {turn_id!r}")
            self._conn.commit()

    def sweep_stale_turns(self, session_id: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE turns SET status = 'failed', completed_at = ?, error_json = ? "
                "WHERE session_id = ? AND status = 'in_progress'",
                (
                    _now_iso(),
                    json.dumps("swept: turn was in_progress at store startup"),
                    session_id,
                ),
            )
            count = cur.rowcount
            self._conn.commit()
            return count
