# Improvement roadmap: Pi-gap closure and hardening

Date: 2026-09-03 · Status: AGREED (user, 2026-09-03 — path locked as
proposed: 2a → 2b → 2c → 3 → 4 → 5) ·
Basis: the honest Pi-vs-tradewind assessment (research notes in
`docs/research/2026-09-03-*`), the repo's own documented gaps (P-5, P-6,
P-7, the unimplemented `request_timeout_s`), and Phase 1 as shipped
(PR #4, v0.7.0).

Governing principle, restated because every row below bends to it: a
feature ships per backend only at the strength that backend can honestly
deliver, declared via capability flags — never approximated silently
(FR-1.2).

---

## 1. The improvement inventory, with per-backend honesty

What each item can ACHIEVE on the four backends — not what we wish.
"engine-owned" means the SDK's engine controls that concern and tradewind
must not fake control of it.

### A. Resilience: retry + overflow recovery  *(the old Phase 2)*
| | langchain | claude | codex | cursor |
|---|---|---|---|---|
| Agent-level retry of a failed model call | **Full** — we own the loop; typed-error classification (httpx/anthropic status codes), Pi's backoff (2s·2^n, max 3), failed partial output persisted but removed from retry context | **Pre-turn only** — re-running a started turn risks duplicated tool side effects (research gap 3); only transport/spawn failures *before the first event* are safely re-issuable; the CLI retries API errors internally anyway | same as claude | same as claude |
| Overflow recovery (compact + retry once) | **Full** — overflow error → strip failed response → compact → single retry | n/a — engine compacts natively | n/a — engine-owned | n/a — engine-owned |
| Declared as | `supports_turn_retry=True` | `False` (pre-turn transport retry is runner-internal, not a claimed capability) | `False` | `False` |

Retry visibility rides `ItemCompleted(kind="event")` items (same zero-
taxonomy-growth choice as compaction), carrying Pi's proven payload
fields (attempt, delay, error class).

### B. Wall-clock turn timeout  *(unimplemented `request_timeout_s`)*
All four: **fully achievable** — the runner enforces the deadline and
cancels via each backend's own `interrupt()` (all four implement it).
Needs one decision: the terminal shape. Proposal: `status="interrupted"`,
`end_reason` grows a `"timeout"` member (a Literal addition is
backward-compatible; `EndReason` is ours, not the frozen event taxonomy).

### C. Message content-shape versioning  *(cheapest borrow, do early)*
Backend-neutral. A `shape_version` recorded once per store (alongside
`PRAGMA user_version`) + documented shape-change rules. Must land
**before sextant depends on the shapes**; retrofitting is the expensive
path Pi's versioned header exists to avoid.

### D. Turns read verb + session-level accounting
Backend-neutral store work: `last_turn_usage()` / `session_usage()` on
the port, and a public `tw.usage(session_id)` rollup (tokens by field,
cost sum, summarizer spend gathered from compaction records). Unlocks:
provider-usage-based compaction triggering (replacing the chars/4-only
amendment), and the "what has this session cost" question every embedder
asks. Cost column honesty per backend is already settled by FR-10.5
(claude reported; langchain/codex/cursor computed-when-table; else None).

### E. Live long-session compaction validation
langchain only (compaction's only host). A gated live test driving a
genuinely long session on the real API: checkpoint quality across ≥2
chained compactions, trigger calibration vs provider-reported usage,
evidence recorded in the RUNBOOK. Converts Phase 1 from design-correct to
trustworthy; also the moment to verify the codex cached-token cost-mapping
assumption against one live codex turn.

### F. Broker verdict enrichment  *(the old Phase 3)*
| | langchain | claude | codex | cursor |
|---|---|---|---|---|
| Deny **reason** reaches the model | **Full** — the reason IS the error tool_result text | **Expected full** — `can_use_tool` deny carries a message field (verify against SDK before speccing) | **Doubtful** — the approval/elicitation reject shape may carry no text; verify, else flag off | **No** — no interception at all (existing `supports_interactive_permissions=False`) |
| **Terminate** the turn on deny | Full — end the loop honestly (`end_reason` decision needed) | Approximate — deny + `interrupt()`; racy, declare accordingly | same as claude | No |
| Input **rewriting** | Possible, but deliberately DEFERRED — mutation hooks are a philosophy change; needs its own decision | — | — | — |

### G. NATIVE→REPLAY degrade  *(ARCH P-7 — tradewind's own unkept promise)*
Targets the three SDK backends (a reaped/expired native session currently
= `TurnFailed`). The Phase-1 summarizer is the missing half now built:
REPLAY = summarize the mirror (domain/compaction.py, unchanged) → inject
as the opening prompt of a FRESH native session → rehome. langchain n/a
(mirror is the record; nothing to degrade from). Cursor rows stay marked
EXPERIMENTAL until P-5 (live verification) is resolved.

### H. Deliberately parked / decisions-not-work
- **Extension hooks** (`before_compact` veto, tool hooks): philosophy
  change (tradewind's hooks are observation-only today). Revisit only on
  a concrete sextant need.
- **Model-catalog helper**: optional versioned data vs NFR-3
  no-invented-defaults; a decision row, not engineering.
- **Fork branch summaries**: parked (needs `spawned_by_message_id`
  deferral resolved for good positioning).
- **Steering / mid-turn queueing**: stays above the library (I-5).
- **Known debt kept visible**: P-5 cursor never live-verified; P-6 codex
  out-of-band backfill; manual-compact single-flight is in-process only
  (documented single-process store assumption).

---

## 2. Proposed migration path

Ordered so each phase unblocks or de-risks the next; one branch + PR +
user-confirmed SemVer bump per phase; spec-before-code per GUIDELINES
§1.1 (each phase gets its own short spec like Phase 1's).

| Phase | Contents | Why this position | Risk |
|---|---|---|---|
| **2a — Foundations** | C (shape versioning) + D (turns verb, `tw.usage()`) | Cheapest items; C is time-sensitive (pre-sextant-dependency); D unlocks 2b's better trigger and is pure addition | Low |
| **2b — Resilience** | A (retry per matrix) + B (timeout, all four) + upgrade compaction trigger to provider-reported usage (via D) | The biggest robustness gap; needs D's verb for the trigger upgrade | Medium (per-adapter error taxonomies are original work) |
| **2c — Validation** | E (live long-session run + calibration; codex cost-mapping check) | Only meaningful after 2b (a long live run without retry would flake on transport noise) | Low code, spends API budget |
| **3 — Broker verdicts** | F per matrix (verify claude/codex SDK shapes first; flags where honest) | Independent of 2x; after validation so 2b/2c lessons inform adapter work | Medium |
| **4 — REPLAY** | G (SDK backends; closes P-7) | Reuses the by-then-validated summarizer; the largest behavioral promise outstanding | Medium-high (resume semantics per engine) |
| **5 — Decisions** | H items: hooks?, catalog?, fork summaries? | Explicit go/no-go rows once sextant has real usage feedback | — |

Explicitly NOT in the path until sextant feedback exists: extension
hooks, input rewriting, catalog shipping, steering.
