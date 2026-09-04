# Schema-migration ownership, and adopting yoyo

Date: 2026-09-03 · Status: APPROVED (user, 2026-09-03) · Precedent:
`binnacle` (`src/binnacle/adapters/postgres_store.py`,
`src/binnacle/migrations/`), which solves the same problem as a library.

## The ownership rule (the thing being fixed)

**Tradewind owns its schema; the host provides a repository.** The
embedding service supplies a *place* for tradewind's data — a file path, a
DSN, a Postgres schema name — and nothing more. It never authors, applies,
or tracks tradewind's DDL: a host that had to would be pinned to
tradewind's internals across every upgrade.

This corrects an earlier framing in this repo's discussion notes ("the
embedder applies migrations at deploy time"), which conflated *who owns the
schema* with *who executes the DDL*. Ownership is tradewind's; execution
stays inside tradewind, scoped to the repository it was granted.

## Why yoyo, not the hand-stepped `PRAGMA user_version`

Today `SqliteSessionStore.migrate()` hand-steps an integer. It works, but:
it has no per-migration bookkeeping (so "did an OLD store upgrade
correctly" is untestable without a bespoke ladder test), no rollbacks, and
its content-shape branch silently falls through (see "Gaps closed" below).
`binnacle` already runs yoyo *as a library* with in-package `.sql` files,
so adopting it gives one mental model across both projects and one
debugging path.

Footprint measured: `yoyo-migrations==9.0.0` (binnacle's pin) adds exactly
one transitive dependency (`zipp`).

## Co-embedding safety (the decisive constraint)

A service may embed binnacle AND tradewind. yoyo creates **four**
bookkeeping tables and only one is renameable:

| Table | Renameable |
|---|---|
| `_yoyo_migrations` | yes (`get_backend(uri, migration_table=...)`) |
| `_yoyo_log` | no (class attribute) |
| `yoyo_lock` | no (class attribute) |
| `_yoyo_version` | no (class attribute) |

So `migration_table=` alone cannot isolate two libraries — *where* the
unqualified tables land is what isolates them. Unisolated, the failure is
**silent, not loud**: yoyo keys migrations by id derived from filename;
both projects would plausibly ship `0001_schema`, so whoever migrates
second sees it already applied, skips it, reports success, and the missing
tables surface much later as `no such table: sessions`.

Four rules, all mandatory:

1. **Postgres (P-4, when built): `schema_name` is REQUIRED**, and yoyo
   connects with `search_path` set to it (binnacle's `_yoyo_uri` pattern),
   so all four tables land inside tradewind's own schema. The `public`
   default must not be reachable by accident.
2. **SQLite: tradewind's store file is tradewind's own.** Pointing
   tradewind at a database the host also migrates is UNSUPPORTED and
   documented as such — SQLite has no schemas, so there is no
   `search_path` escape hatch.
3. **Migration ids are namespaced**: `0001_tradewind_schema`, never
   `0001_schema`. Costs nothing; makes an id collision impossible even
   under misconfiguration, which matters precisely because the failure is
   silent.
4. **`migration_table` is set explicitly** to
   `_tradewind_yoyo_migrations` — defense in depth for the shared-file case
   rule 2 forbids.

## The `:memory:` problem and its resolution

Verified this session: **each `:memory:` connection is a private
database**, and yoyo opens its own connection. Running yoyo against the
ephemeral store (FR-5.7, the *default*) would migrate a throwaway database
while the store's own connection saw nothing.

Resolution — one source of truth, two application paths:

- The `.sql` files are the single source of truth for the schema.
- **File-backed store**: yoyo applies them, with full bookkeeping, lock,
  and rollbacks.
- **`:memory:` store**: the same files are read and applied directly
  through the store's own connection, without bookkeeping. This is
  principled, not a workaround — migrations exist to evolve a *persistent*
  store across versions; an ephemeral database has no past and no future,
  it is created at the current schema and destroyed. Drift between the two
  paths is impossible because both read the same files, and a test pins
  schema equality between a migrated file store and a fresh `:memory:` one.

Measured cost of yoyo on a construction: ~1.4 ms median (vs ~0.08 ms for
the old `executescript`) — acceptable for file stores; irrelevant for
`:memory:`, which does not use yoyo.

## Layout

```
src/tradewind/migrations/__init__.py        # packaging marker; not imported as Python
src/tradewind/migrations/sqlite/
    0001_tradewind_schema.sql   + .rollback.sql
    0002_tradewind_meta.sql     + .rollback.sql
```

Dialects get their own directories (`sqlite/`, later `postgres/`): binnacle
is Postgres-only and can share one set, tradewind cannot. The *contract*
(which tables must exist) is stated once in component spec 02; the SQL is
per-dialect and owned by its adapter.

`yoyo` is confined to `tradewind.adapters` by an import-linter contract
(binnacle's `forbidden_modules` precedent); `domain` and `application`
never see it. mypy gets a narrow `yoyo.*` override — it ships no
`py.typed`, same as binnacle's house precedent.

## Existing stores

No adoption shim is needed. Every statement in `0001`/`0002` is
`CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` /
`INSERT OR IGNORE`, so applying them to a store already at
`user_version = 2` is a no-op that simply records the bookkeeping. Doing
this NOW — before sextant creates its first persistent store — is why no
stamping logic is required at all. `PRAGMA user_version` is left as
written but is no longer authoritative; the migration table is.

## Gaps closed at the same time

1. **Silent content-shape fall-through.** `recorded_shape <
   CONTENT_SHAPE_VERSION` currently falls through with no action and no
   error — a trap armed for the first shape bump (a v1 store would be read
   as if it held v2 shapes). It becomes a loud `ConfigError` naming both
   versions, symmetrical with the existing refuse-newer branch, until a
   real shape-migration registry exists.
2. **No ladder test.** Current tests only prove a *fresh* file reaches the
   current version and that `migrate()` is idempotent — nothing proves an
   *old* store upgrades. yoyo's per-migration bookkeeping makes this
   mechanical, and a test applies `0001` alone, then `migrate()`, and
   asserts the result matches a fresh store.

Content-shape migrations stay a separate axis (they are data migrations
over `content_json`, not DDL) but need no separate tool: yoyo supports
Python migration steps when the first one is needed.

## Verification

- Ladder test (old store → current), `:memory:`-vs-file schema parity,
  idempotent re-`migrate()`, the loud older-shape guard, namespaced
  migration ids present in `_tradewind_yoyo_migrations`, and the
  bookkeeping table name.
- `uv build` + install check that the `.sql` files ship in the wheel
  (a migration file missing from the distribution is an install-time
  landmine).
- Full gauntlet on 3.12 and 3.13; import-linter proves yoyo confinement.
- Version: minor bump proposed (0.11.0 → 0.12.0) — new dependency and
  changed migration mechanism; confirmed before applying.

## Out of scope

The Postgres store itself (P-4). Its rules are fixed here so the adapter
has a contract to build against, but note the harder blocker recorded in
`ports.py`: `seq` assignment is serialized by an *in-process*
`threading.Lock` and the single-flight turn invariant leans on in-process
state — multi-process correctness, not migrations, is that adapter's real
work.
