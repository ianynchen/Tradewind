# Component: Session Store

## Purpose

Durable, backend-neutral persistence for sessions, turns, and messages
(REQUIREMENTS FR-5, FR-6.4, NFR-1, NFR-2). The mirror for SDK backends, the
system of record for `langchain`. It stores and retrieves; resume policy,
reconciliation decisions, and event normalization live above it.

## Owns

The schema of ARCHITECTURE §4 (authoritative there; not duplicated here), its
migrations, the intent verbs' storage semantics, the mirror writer, and the two
retrieval shapes.

## Depends on

Implements `SessionStorePort` declared in `application`; `domain` never imports
it. Default engine is stdlib `sqlite3`; a caller-built implementation of the port
(Postgres, P-4) is accepted via a `SessionStorePort`-valued `TradewindConfig.store`
(ADR-0001: one union field — path, port instance, or None for the ephemeral
in-memory mirror, FR-5.7).

## Storage decisions

| Decision | Choice | Why |
|---|---|---|
| Engine | stdlib `sqlite3`, WAL, `foreign_keys=ON`, `busy_timeout` 5 s | House pattern (waypoint 01); zero deps; embedder-friendly single file. |
| Location | path-valued `TradewindConfig.store`, no default path; `None` selects an ephemeral in-memory database (FR-5.7) | Library rule: the host decides where data lives (NFR-5) — including nowhere. |
| Schema ownership | **Tradewind owns its schema; the host supplies only a repository** (a path, later a DSN + `schema_name`). The host never authors or applies tradewind's DDL. | ADR-0003. A host authoring these tables would be pinned to tradewind's internals across every upgrade. |
| Schema versioning | yoyo-migrations over in-package `.sql` files (`src/tradewind/migrations/sqlite/`), forward-only, applied at open (ADR-0003; replaced hand-stepped `PRAGMA user_version`, which remains written but is no longer authoritative) | Per-migration bookkeeping and rollbacks; one model shared with binnacle; extends to Postgres. |
| Content-shape versioning (FR-5.9) | `meta.content_shape_version`, checked on every `migrate()`: **both** directions refuse loudly (`ConfigError`) — newer means a later tradewind owns the data, older means no shape migration exists for it yet | The JSON inside `content_json` is a contract too; Pi versions its session files for the same reason. Reading one shape version as another corrupts meaning silently, so neither direction may fall through. |
| Timestamps | UTC ISO-8601 text | Legible in any SQLite browser. |
| Identifiers | caller-minted UUID text primary keys (FR-5.6, I-1a) | Idempotent creation; embedder records the id before calling. |
| `raw_json` | stored always, returned only on `include_raw` | The payload elephant stays out of the hot path (NFR-1). |

## Migration rules (ADR-0003)

Isolation is mandatory, because the failure mode when it is missing is
**silent**. yoyo creates four bookkeeping tables and only `_yoyo_migrations`
is renameable; a service embedding both tradewind and another yoyo-using
library (e.g. `binnacle`) would otherwise share them, and since yoyo keys
migrations by filename-derived id, the second library to migrate would skip
its own `0001_schema`, report success, and fail later as `no such table`.

| Rule | Value |
|---|---|
| Migration ids namespaced | `0001_tradewind_schema`, `0002_tradewind_meta` |
| Bookkeeping table named explicitly | `_tradewind_yoyo_migrations` |
| Postgres (P-4) | `schema_name` REQUIRED; yoyo connects with `search_path` set to it, so all four bookkeeping tables land inside tradewind's schema |
| SQLite | tradewind's store file is tradewind's own; sharing a database the host also migrates is UNSUPPORTED (no schemas, so no `search_path` escape hatch) |
| `:memory:` (FR-5.7) | yoyo cannot reach it — each `:memory:` connection is a private database and yoyo opens its own. The same `.sql` files are applied directly through the store's connection, without bookkeeping: an ephemeral database has no version history to track. Schema parity with a migrated file store is test-pinned. |

## Behavior contract

**Intent verbs** (FR-5.6): `create` inserts or raises `SessionExists`; `resume`
loads or raises `SessionNotFound`; `ensure` is get-or-create in one transaction.
All three return the row plus deserialized `options_json`.

**Mirror writer.** Consumes completed items from the Event Normalizer within a
turn context: opens the turn row (`in_progress`), appends messages with
monotonically assigned `seq` per session, finalizes the turn (status, usage,
cost, final text). Writes are per-item transactions so a crashed turn leaves a
readable partial transcript with the turn stuck `in_progress` — the Resume
Planner sweeps those to `failed` on next contact. Deltas never reach the store
(I-3).

**Child sessions** (I-1): stored as their own rows with `parent_session_id`,
`spawn_kind`, `spawned_by_message_id`; the store rejects a message whose
`session_id` differs from its turn's session.

**Backfill/reconciliation support** (FR-6.4): an idempotent
`import_native_items(session_id, items)` that dedupes on `native_id`, assigns
`seq` after the current tail, and tags provenance (`event` kind rows carry the
source). The *decision* to reconcile is the Resume Planner's; the store only
guarantees idempotency.

**Retrieval** (FR-5.5, NFR-1): flat = single indexed query ordered by `seq`;
tree = recursive CTE over `parent_session_id` joined to messages, ordered
`(session_id, seq)`; cursor pagination on `(session_id, seq)`. Both accept
`include_raw`; neither ever interleaves child messages into a flat read.

**Fork support** (for the `langchain` adapter's emulated fork): `copy_history
(src_session_id, dst_session_id, up_to_seq | None)` — a store-level copy that
creates the fork row with lineage.

## Acceptance

- Round-trip property test: normalized events in → retrieval out is loss-free for
  every `kind`, with `raw_json` byte-identical.
- Crash simulation: kill mid-turn, reopen, flat read succeeds, turn is
  `in_progress` and sweepable.
- Tree retrieval over a 3-deep, 5-session lineage returns exactly the ID set of
  the CTE; flat retrieval of the root contains zero child messages.
- `import_native_items` applied twice is a no-op the second time.
- 10k-message session: flat read without raw under 10 ms on local SQLite
  (NFR-1 smoke bound).


## Content shapes (v1) — FR-5.9

The per-kind `content` JSON stored in `messages.content_json`. ANY change
here bumps `CONTENT_SHAPE_VERSION` (domain/models.py) and ships a shape
migration in the same commit (§5.1 discipline applied to this contract).

| kind | content (v1) |
|---|---|
| `text`, `thinking` | `{"text": str}` (thinking's signature, when present, rides `raw`, not content) |
| `tool_use` | `{"id": str\|null, "name": str, "input": dict}` |
| `tool_result` | `{"tool_use_id": str, "content": str, "is_error": bool}` |
| `compaction` | `{"summary": str, "first_kept_seq": int, "tokens_before": int, "summarizer_usage": dict[str,int], "summarizer_cost_usd": float\|null}` |
| `event` | `{"type": str, ...}` — adapter-declared discriminator plus free fields (e.g. `compaction_failed` carries `error`) |
| `command_execution`, `file_change`, `plan`, `web_search` | adapter-defined pass-through of the engine's own payload (codex/cursor); readers must treat unknown fields as opaque |
