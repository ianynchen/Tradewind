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
(Postgres, P-4) is accepted via `StoreConfig.store`.

## Storage decisions

| Decision | Choice | Why |
|---|---|---|
| Engine | stdlib `sqlite3`, WAL, `foreign_keys=ON`, `busy_timeout` 5 s | House pattern (waypoint 01); zero deps; embedder-friendly single file. |
| Location | `StoreConfig.sqlite_path`, no default path | Library rule: the host decides where data lives (NFR-5). |
| Schema versioning | `PRAGMA user_version`, forward-only migrations at open | An embedder upgrade must never strand transcripts. |
| Timestamps | UTC ISO-8601 text | Legible in any SQLite browser. |
| Identifiers | caller-minted UUID text primary keys (FR-5.6, I-1a) | Idempotent creation; embedder records the id before calling. |
| `raw_json` | stored always, returned only on `include_raw` | The payload elephant stays out of the hot path (NFR-1). |

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
