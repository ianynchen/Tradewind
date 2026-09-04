# ADR-0003: Tradewind owns its schema, applied with yoyo

Date: 2026-09-03 · Status: Accepted · Supersedes: the hand-stepped
`PRAGMA user_version` migration in `SqliteSessionStore.migrate()`.

## Context

Tradewind persists sessions in SQLite today and, eventually, PostgreSQL
(P-4). It is a library embedded in someone else's process, which raised the
question of who manages its schema. An earlier framing in discussion
proposed that the embedding service apply tradewind's DDL at deploy time.

That framing conflated two independent questions: **who owns the schema**
and **who executes the migration**. It also ignored a working in-house
precedent — `binnacle` is likewise a library, and already runs
yoyo-migrations from inside itself with in-package `.sql` files.

## Decision

**Tradewind owns its schema. The host supplies only a repository** — a file
path, a DSN, or a Postgres schema name. The host never authors, applies, or
tracks tradewind's DDL; a host that had to would be pinned to tradewind's
internals across every upgrade. Execution stays inside tradewind, scoped to
the repository it was granted.

Migrations are applied with **yoyo-migrations 9.0.0** (binnacle's pin), from
`.sql` files shipped inside the package, per dialect
(`src/tradewind/migrations/sqlite/`, later `postgres/`). yoyo is confined to
`tradewind.adapters` by an import-linter contract.

### Co-embedding safety

A service may embed binnacle *and* tradewind. yoyo creates four bookkeeping
tables and only `_yoyo_migrations` is renameable (`_yoyo_log`, `yoyo_lock`,
`_yoyo_version` are class attributes) — so table naming alone cannot isolate
two libraries; *where the unqualified tables land* is what isolates them.
Unisolated, the failure is **silent**: yoyo keys migrations by
filename-derived id, so two libraries sharing bookkeeping and both shipping
`0001_schema` would make the second one skip its own migration and report
success, surfacing later as `no such table`.

Four rules, all mandatory:

1. **Postgres: `schema_name` is required**, with yoyo connecting under
   `search_path` set to it (binnacle's `_yoyo_uri` pattern), so all four
   tables land in tradewind's own schema.
2. **SQLite: tradewind's store file is tradewind's own.** Sharing a SQLite
   database with a host that also migrates it is unsupported — SQLite has
   no schemas, so no `search_path` escape hatch exists.
3. **Migration ids are namespaced**: `0001_tradewind_schema`.
4. **`migration_table` is set explicitly** to
   `_tradewind_yoyo_migrations`.

### The `:memory:` exception

Verified: each `:memory:` connection is a *private database*, and yoyo opens
its own connection — so against the ephemeral store (FR-5.7, the default) it
would migrate a throwaway database while the store saw nothing. In-memory
stores therefore apply the same `.sql` files directly through their own
connection, without bookkeeping.

This is principled rather than a workaround: migrations exist to evolve a
*persistent* store across versions, and an ephemeral database is created at
the current schema and destroyed. Both paths read the same files, and a test
pins schema equality between them.

## Consequences

- One migration model across binnacle and tradewind; per-migration
  bookkeeping and rollbacks replace an integer that was hand-stepped.
- One new runtime dependency (`yoyo-migrations`, plus `zipp`). Measured cost
  on a file-backed construction: ~1.4 ms (vs ~0.08 ms for the old
  `executescript`); `:memory:` is unaffected.
- No adoption shim was needed: every statement is `IF NOT EXISTS` /
  `INSERT OR IGNORE`, so applying them to a store already at
  `user_version = 2` merely records bookkeeping. Doing this before sextant
  created its first persistent store is why. `PRAGMA user_version` remains
  written but is no longer authoritative.
- Content-shape versioning (FR-5.9) stays a separate axis — data migrations
  over `content_json`, not DDL. It needs no separate tool when the first one
  arrives: yoyo supports Python migration steps.

## Alternatives rejected

- **Keep `PRAGMA user_version` stepping.** Idiomatic for embedded SQLite,
  zero dependencies — but no per-migration bookkeeping (making "did an old
  store upgrade correctly" untestable without bespoke scaffolding), no
  rollbacks, and it does not extend to Postgres.
- **Host applies the DDL.** Rejected on ownership grounds, above.
- **yoyo for `:memory:` too.** Impossible: private per-connection database.
