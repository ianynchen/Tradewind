-- The meta key/value table carrying `content_shape_version` (FR-5.9):
-- the version of the JSON shapes inside `messages.content_json`,
-- orthogonal to this table schema's own version.
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
INSERT OR IGNORE INTO meta (key, value) VALUES ('content_shape_version', '1');
