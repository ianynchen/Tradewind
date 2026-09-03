# Phase 2a: Foundations — content-shape versioning + session accounting

Date: 2026-09-03 · Status: PROPOSED (awaiting approval) · Parent:
`2026-09-03-improvement-roadmap.md` (path AGREED; this is its first
phase). Items C and D of the roadmap.

---

## Item C — Mirror content-shape versioning

**Problem.** The store's TABLE schema is versioned (`PRAGMA
user_version`, forward-only migrations), but the JSON *inside*
`content_json` — the per-kind shapes every adapter reads and sextant is
about to depend on — carries no version anywhere. Pi versions its session
files and auto-migrates; retrofitting after an embedder depends on shapes
is the expensive path.

**Design.**
1. `CONTENT_SHAPE_VERSION = 1` — a domain constant
   (`domain/models.py`), where the shapes themselves are defined.
2. The sqlite store grows a `meta(key TEXT PRIMARY KEY, value TEXT)`
   table (table-schema migration: `user_version` 1 → 2, forward-only per
   the store's existing rule). `migrate()` records
   `content_shape_version` on first write.
3. On open of an existing store: recorded **greater** than the library's
   constant → `ConfigError`, loudly ("store written by a newer tradewind;
   refusing to modify") — never risk corrupting a newer writer's data.
   Recorded **less** than current → the future shape-migration hook runs
   (empty today; versions are equal until a shape actually changes).
4. **The v1 shapes get written down**: component spec 02 gains a
   "Content shapes (v1)" table — the per-kind `content` schema
   (`text`/`thinking`/`tool_use`/`tool_result`/`compaction`/`event` and
   the pass-through kinds) — and the rule: any change to these shapes
   bumps `CONTENT_SHAPE_VERSION` and ships a shape migration in the same
   commit (mirroring GUIDELINES §5.1 for this contract).
5. Custom `SessionStorePort` implementations: the port's `migrate()`
   docstring gains one sentence — implementations own their equivalent
   check. No new abstract method for this item.

## Item D — Turns read verb + `tw.usage()`

**Problem.** Cost/usage lives on turn rows with no read verb and no
rollup: "what has this session used/cost" requires the embedder to scrape
rows; the 2b compaction-trigger upgrade (provider-reported usage) needs
the same missing verb.

**Design.**
1. New domain value object:

```python
@dataclass
class TurnUsage:
    turn_id: str
    status: TurnStatus
    usage: dict[str, int]
    cost_usd: float | None
```

2. New port verb (BREAKING for custom `SessionStorePort` implementers —
   abstract, honestly, not a silently-degrading default):
   `turn_usages(session_id) -> list[TurnUsage]`, ordered by turn seq;
   unknown session → `[]` (matching `history()`'s read semantics).
   2b's "last turn usage" trigger is `turn_usages(...)[-1]` — one verb
   serves both consumers.
3. New public rollup, `await tw.usage(session_id) -> SessionUsage`
   (pydantic model): `turns: int`; `usage: dict[str, int]` (field-wise
   sum over turns); `cost_usd: float | None` (sum of non-None turn
   costs; `None` only when every turn's is None — a zero-cost session
   and an unknown-cost session must not look alike);
   `summarizer_usage: dict[str, int]` and `summarizer_cost_usd:
   float | None` (gathered from `kind="compaction"` records). Validates
   the session exists (`SessionNotFound`) since it is a public verb.
   Flat scope only in this phase (no `include_children` rollup yet).

**Phase-1 amendment required (double-count prevention).** Phase 1 folded
the auto-compaction summarizer's cost into the triggering turn's
`cost_usd`, while manual compaction's spend lived only as tokens in the
record. An aggregate summing turns AND records would double-count the
auto path. Cleaner, uniform rule, amended in this phase:

- Summarizer spend (tokens AND, newly, `summarizer_cost_usd`, computed
  at compact time when the tier has a cost table) lives ONLY on the
  compaction record — auto and manual identically.
- A turn's `cost_usd` is the turn's own tokens, nothing else (the
  langchain adapter stops adding summarizer cost).
- `tw.usage()` = turns + records, no overlap by construction.

## Deliverables & verification

- Store migration test (fresh store lands at user_version 2 with the
  meta row; an existing v1 store migrates; a future-version store is
  refused with `ConfigError`); shape-constant equality test.
- `turn_usages` tests (ordering, unknown session, statuses included).
- `tw.usage()` tests: sums, all-None vs some-None vs zero cost,
  summarizer split (auto and manual records), `SessionNotFound`,
  no-double-count end-to-end (auto-compacted session: turn costs +
  record cost equal the expected total exactly once).
- Docs: component 02 (meta table, v1 shape table), component 01
  (`tw.usage`), README (one line under Long sessions), REQUIREMENTS
  FR-5.9 (versioned shapes) noted under FR-5, CHANGELOG, RUNBOOK.
- Version: minor bump proposed (0.7.0 → 0.8.0) with a BREAKING CHANGE
  footer for the new abstract port verb; confirmed before applying.

## Out of scope

The provider-usage compaction trigger (2b consumes the verb), retry,
timeout, any `include_children` usage rollup, and shape migration No. 1
itself (none needed — v1 IS the current shapes).
