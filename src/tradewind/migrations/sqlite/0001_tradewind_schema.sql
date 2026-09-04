-- Sessions, turns, and messages: the mirror's core schema
-- (ARCHITECTURE.md section 4). Ids are namespaced `tradewind` so they can
-- never collide with a co-embedded library's own migrations.
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
