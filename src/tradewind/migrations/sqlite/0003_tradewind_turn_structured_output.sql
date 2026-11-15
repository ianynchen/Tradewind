-- A schema turn's structured result (FR-1.3), stored as JSON text beside
-- the turn's prose `final_text`. NULL on every turn without an
-- `output_schema`, and on every row written before this migration.
ALTER TABLE turns ADD COLUMN structured_output TEXT;
