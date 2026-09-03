# Phase 1: Model metadata + mirror compaction

Date: 2026-09-03 · Status: APPROVED (user, 2026-09-03; incl. amendments:
manual compaction B3, all-auth cost, chars/4-only trigger) · Research basis:
`docs/research/2026-09-03-pi-sdk-survey.md`,
`docs/research/2026-09-03-pi-implementation-notes.md` (source-verified
against Pi @ github.com/earendil-works/pi, MIT, commit e44d75c).

Scope agreed 2026-09-03: Phase 1 = features 3 (model metadata) + 1
(compaction); Phase 2 = auto-retry; Phase 3 = broker verdicts; fork
branch-summary parked.

---

## Feature A — Model metadata (proposed FR-10.5)

**What.** An optional `ModelMeta` on `ModelSpec` (per tier, per profile):

```python
class ModelCostTier(BaseModel):
    input_tokens_above: int          # tier applies when request input exceeds this
    input: float; output: float      # $/Mtok
    cache_read: float; cache_write: float

class ModelCost(BaseModel):
    input: float; output: float; cache_read: float; cache_write: float   # $/Mtok
    tiers: list[ModelCostTier] = []  # whole-request tier: highest matched threshold wins

class ModelMeta(BaseModel):
    context_window: int              # tokens
    max_tokens: int                  # max output tokens
    cost: ModelCost | None = None

class ModelSpec(BaseModel):
    model: str
    effort: EffortLevel | None = None
    meta: ModelMeta | None = None    # NEW — optional, absent by default
```

**Consumption.**
1. `TurnResult.cost_usd` is computed from the turn's token usage wherever a
   cost table exists and the backend does not report a dollar figure itself
   — **langchain, codex, and cursor** (user decision 2026-09-03; all three
   already populate `TurnResult.usage` with engine-reported token counts).
   The shared `calculate_cost` (ported Pi semantics) lives in
   `domain/models.py` beside `ModelMeta`: whole-request tier selection
   (`input + cache_read + cache_write` vs the highest matched
   `input_tokens_above`), then `rate/1e6 * tokens` summed. Per-adapter
   field mapping: codex `cached_input_tokens` → cache-read rate (no
   cache-write field: 0); cursor `cache_read_tokens`/`cache_write_tokens`
   map directly; absent fields count as zero. The Anthropic
   1h-cache-write 2× rule is **omitted and documented** (no adapter
   surfaces the 1h split; guessing would be dishonest).
2. The compaction trigger (Feature B) reads `meta.context_window`.

**Honesty rules.** Absent `meta.cost` → `cost_usd` stays `None`; absent
`meta.context_window` → auto-compaction stays off (NFR-3: no defaults
invented). A backend that *reports* cost (claude `total_cost_usd`) keeps
the reported number — computed cost never overwrites reported cost.
Computed cost is applied on ALL auth modes, subscription included (user
decision 2026-09-03): on a subscription profile the figure is the
**API-equivalent price of the tokens used, not billed spend** — stated
verbatim in the `ModelMeta.cost` docstring and README so nobody mistakes a
budgeting aid for an invoice.

**Not in scope:** thinking-level maps, sampling params, per-API compat
(Pi has them; tradewind has no consumer).

---

## Feature B — Mirror compaction (proposed FR-5.8)

**What.** When a mirror-fed session's context approaches the tier's window,
tradewind summarizes the older transcript into a checkpoint, records it in
the mirror as a first-class message, and rebuilds subsequent requests from
`[summary + retained tail]`. Applies ONLY where tradewind feeds context from
the mirror: the langchain backend now, REPLAY (ARCH P-7) later. Native-
resume backends are untouched (their engines own context; claude/codex have
their own compaction).

### B1. Placement — inside the langchain adapter, pure core in domain

- Pure functions (cut-point selection, token estimation, serialization,
  prompts) in a new **`domain/compaction.py`** (stdlib-only — domain-pure,
  reusable by REPLAY and a future fork-summary).
- Trigger check + summarizer call + record emission live in
  **`adapters/langchain_backend.py`** — it owns the chat model and the
  rebuild. The Turn Runner and the `Backend` port are UNTOUCHED: the
  compaction record is emitted as an ordinary
  `ItemCompleted(NormalizedMessage(kind="compaction"))`, which the runner
  already mirrors, and the **frozen event taxonomy gains zero members**
  (the `Kind` literal has reserved `"compaction"` since task 2).

### B2. The mirror record

`NormalizedMessage(role="user", kind="compaction", content={...})`:

```python
{
  "summary": str,             # merged checkpoint text
  "first_kept_seq": int,      # mirror seq of the first retained message (Pi's firstKeptEntryId → flat-row analogue)
  "tokens_before": int,       # estimated context tokens pre-compaction
  "summarizer_usage": {...},  # token usage of the summarization call(s)
}
```

Summarizer spend lands INSIDE the record (research gap 5 decision): no
synthetic turn row, no schema migration; `TurnResult.cost_usd` for the turn
that triggered compaction includes the summarizer cost when metadata allows
computing it, and the record keeps the raw usage either way.

### B3. Trigger — automatic AND manual (user decision 2026-09-03)

- Settings: `CompactionSettings(auto=True, reserve_tokens=16384,
  keep_recent_tokens=20000)` on `TradewindConfig.defaults`. `auto`
  governs AUTOMATIC triggering only; `auto=False` never disables the
  manual verb. Automatic is additionally effective only when the turn's
  tier has `meta.context_window`; otherwise inert (honest — no
  estimated-window guessing).
- **Manual**: `Session.compact(instructions: str | None = None)` — the
  caller compacts NOW, through the same machinery (cut points, checkpoint
  prompt, chaining), with `instructions` appended to the summarization
  prompt as "Additional focus: …" (Pi's manual-compact parity). Works
  WITHOUT metadata (the caller supplies the "when"; summarizer budget
  falls back to `0.8 * reserve_tokens` when `meta.max_tokens` is absent).
  Raises `Unsupported` on native-resume backends (tradewind cannot
  compact context it does not feed — same doctrine as `history_scope`),
  and participates in the session single-flight (I-5): `TurnInProgress`
  if a turn is in flight, and a new turn cannot start mid-compaction.
  Returns the recorded compaction message so the caller sees the
  checkpoint it paid for.
- Predicate: `context_tokens > context_window - reserve_tokens`, where
  `context_tokens` is a chars/4 estimate over the messages about to be
  sent (AMENDED 2026-09-03, user-approved: the originally-spec'd
  turns-table read has no port verb; Pi's own estimator is deliberately
  conservative — it overestimates, so compaction fires early, never late.
  Provider-usage-based triggering is deferred until a turns read verb
  exists for other reasons).
- Checked **before the request** in the adapter (Pi's pre-request check);
  overflow-retry recovery is Phase 2 territory (retry layer) — Phase 1
  compacts proactively only.

### B4. Cut points (Pi's rule, simplified by tradewind's schema)

- Valid cut points: any message whose `kind != "tool_result"` (never split
  a `tool_use` from its result).
- Walk backwards accumulating chars/4 until `keep_recent_tokens`, then the
  nearest valid cut at-or-after — a soft floor on the retained tail.
- Split-turn detection is EXACT, not inferred: mirror rows carry `turn_id`,
  so "cut lands mid-turn" = cut row shares `turn_id` with the row before
  it. Split turns get Pi's two-summary treatment (turn-prefix summarized
  separately, merged under `**Turn Context (split turn):**`).
- Engine-specific kinds (`command_execution`, …) cannot appear in a
  langchain session's own rows; `history_scope="tree"`-folded child blocks
  are `kind="text"` → valid cut points. (Research gap 1 dissolves for
  Phase 1; REPLAY revisits it.)

### B5. Summarizer

Pi's design ported: the session's OWN tier model (no second model to
configure); conversation serialized to plain text (`[User]: …`,
`[Assistant tool calls]: name(args)`, tool results truncated to 2000
chars) so the model cannot "continue" it; fixed structured-checkpoint
prompt (`## Goal / ## Constraints / ## Progress / ## Key Decisions /
## Next Steps / ## Critical Context`); update-variant prompt with
`<previous-summary>` when chaining; `max_tokens = min(0.8 * reserve_tokens,
meta.max_tokens)`; a `length` stop is a HARD failure (a truncated summary
must never become a checkpoint) — the turn proceeds uncompacted and the
failure is surfaced in the turn's event stream as a `kind="event"` item,
never silently.

### B6. Chaining

Per Pi: re-summarize from the previous compaction record's
`first_kept_seq` (previously-kept tail + everything since), feeding the old
summary as `<previous-summary>` — never summarizing summary text as
conversation. File-op tracking (Pi's read/modified sets) is OMITTED —
langchain sessions have no file tools by default; caller tools are opaque.

### B7. Rebuild

`_rebuild_messages`: find the LATEST `kind="compaction"` row; emit it as a
user message ("The conversation history before this point was compacted
into the following summary: …"), then rows with `seq >= first_kept_seq`;
older rows omitted. Sessions without compaction records rebuild exactly as
today (regression tests pin this).

### B8. Interactions decided now (research gap 4)

- **Fork** (`copy_history` reassigns seq): compaction rows are DROPPED from
  the copy (a fork gets verbatim full history; `first_kept_seq` cannot
  survive renumbering honestly). Documented on FR-5.8.
- **`history_scope="tree"`**: child folding operates on the post-compaction
  rebuilt view (children of summarized-away turns fold after the summary).
- **`after_seq`/pagination, `tw.history()`**: retrieval is untouched —
  compaction affects what is FED to a model, never what is STORED or
  retrievable (the mirror keeps every row; NFR-1 unaffected).

---

## Deliverables & verification

- FR-10.5 + FR-5.8 added; ARCHITECTURE §3.2 note (compaction surfaces as
  `ItemCompleted`, taxonomy unchanged) + §4 record shape; component specs
  01/03; README section; CHANGELOG.
- Tests: cost math (tiers, absence, never-overwrite-reported), trigger
  gating (no meta → inert), cut-point unit tests incl. never-split and
  split-turn via `turn_id`, chaining, hard-fail on length-stop, rebuild
  with/without records, fork-drops-records, scripted end-to-end (long fake
  session compacts and later turns see summary + tail); manual: `compact()`
  works without metadata and with `auto=False`, appends caller instructions
  to the prompt, raises `Unsupported` on a native-resume backend and
  `TurnInProgress` mid-turn, and `auto=False` suppresses all automatic
  triggering.
- Version: minor bump (proposed 0.6.0 → 0.7.0), confirmed before applying.

## Out of scope (explicitly)

Retry/overflow-recovery (Phase 2), broker verdicts (Phase 3), fork branch
summaries (parked), REPLAY wiring (P-7 stays open, but `domain/compaction.py`
is written to serve it). Manual compaction (`Session.compact()`) is IN
scope — B3, pulled in by user decision 2026-09-03.
