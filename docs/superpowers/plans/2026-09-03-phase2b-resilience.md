# Phase 2b: Resilience — retry, turn timeouts, provider-usage trigger

Date: 2026-09-03 · Status: APPROVED (user, 2026-09-03) · Parent:
`2026-09-03-improvement-roadmap.md` (path AGREED; items A + B plus the 2a
follow-through). Research basis: Pi implementation notes §2 (auto-retry)
and §1.1 (trigger), with the roadmap's per-backend honesty matrix binding.

---

## Item 1 — Retry

### 1.1 Langchain: full-strength, per MODEL CALL (not per turn)

The retry unit is one model call inside the tool loop — smaller than Pi's
(which replays agent state it owns). This is deliberately side-effect
safe: tool executions already completed in the turn are NEVER re-run; only
the failed request is retried against unchanged messages.

- **Classification is typed-first** (roadmap: Pi's regexes survive only as
  a checklist): retryable = a `status_code` attribute in
  {408, 429, 500, 502, 503, 504, 529}, or a connection/transport error
  (`httpx.TransportError`, `ConnectionError`, timeouts); non-retryable =
  other 4xx (auth, invalid request, not-found) and anything unclassified —
  **fail closed to TurnFailed, never retry blindly**. A narrow string
  fallback covers Pi's documented transient classes only when no status
  exists ("overloaded", "connection reset").
- **Backoff**: Pi's schedule — `base_delay * 2^(attempt-1)`, defaults
  `RetrySettings(max_attempts=3, base_delay_s=2.0)` on `TurnDefaults`
  (config-wide; `max_attempts=0` disables).
- **Streamed partials**: deltas already yielded are live-only (I-3, never
  mirrored) — nothing to undo; the partial gather is discarded from the
  retried request. Matches Pi's persist-but-exclude semantics given our
  delta rules.
- **Overflow recovery** (Pi §2.3, compaction-owned, distinct from retry
  counts): an error classified as context-overflow (400 + the provider's
  overflow message shapes) triggers ONE compact-then-retry per turn,
  reusing the FR-5.8 machinery — works without `ModelMeta` (the error
  itself is the trigger) and regardless of `CompactionSettings.auto`
  (recovery is not proactive compaction); a second overflow in the same
  turn is TurnFailed. Compaction failure during recovery → TurnFailed with
  the compaction error, loudly.

### 1.2 SDK backends: pre-turn transport only

Re-running a STARTED turn on claude/codex/cursor risks duplicated tool
side effects (research gap 3) and their engines retry API errors
internally. Tradewind retries only the initial connect/spawn step
(claude `connect`, codex `client.start`, cursor `launch_bridge`) with the
same backoff — failures where provably nothing ran yet.

### 1.3 Declaration and visibility

- New capability flag `supports_turn_retry` (mid-turn model-call retry):
  langchain True; claude/codex/cursor False. **Breaking** for
  `Capabilities` constructors (required field, no default — the
  established honesty pattern).
- Every scheduled retry is VISIBLE: an `ItemCompleted(kind="event")` item
  `{"type": "retry_scheduled", "phase": "model_call"|"connect",
  "attempt": int, "max_attempts": int, "delay_s": float, "error": str}` —
  mirrored, zero event-taxonomy growth (the compaction precedent).

## Item 2 — Turn timeout (all four backends)

`TurnDefaults.request_timeout_s` (default 600.0) becomes REAL:

- The runner arms a watchdog task per turn (a plain task + flag —
  deliberately NOT a cancel scope across the generator's yields, the
  pitfall class we fixed in 0.5.0); on deadline it calls the backend's own
  `interrupt()` and marks the turn timed out.
- The stream then ends without a terminal event (the existing interrupt
  contract); the runner's synthesized terminal becomes
  `TurnCompleted(status="interrupted", end_reason="timeout")`.
- `EndReason` grows `"timeout"` (Literal growth: backward-compatible).
- Per-call override `request_timeout_s` (positive number; validated like
  `max_tool_rounds`, `ConfigError` otherwise, pre-`begin_turn`). No
  disable knob — raise the default instead (documented).
- BEHAVIOR CHANGE, called out: hung turns that previously ran forever now
  end at 600 s by default.

## Item 3 — Provider-usage compaction trigger (consumes 2a's verb)

Replace the chars/4-only estimate with Pi's layering: provider-reported
tokens where available, conservative estimate for the trailing gap.

- The runner (only when the backend feeds mirror context — the
  `feeds_mirror_context` gate already computed for `history_scope`, so
  SDK turns still issue ZERO extra reads) fetches
  `turn_usages(session_id)[-1]` alongside the turn setup and passes
  `TurnContext.last_turn_usage: dict[str, int] | None` and
  `last_turn_id: str | None` (two optional fields, defaults None).
- Adapter trigger math: `context_tokens = last_turn_usage["total_tokens"]
  + chars/4 estimate of history rows AFTER the last row bearing
  last_turn_id` (+ prompt + system prompt), falling back to the pure
  estimate when no usage exists.
- Pi's staleness guard, ported to seq-space: if a compaction record exists
  NEWER (higher seq) than the last row of that turn, the reported usage is
  pre-compaction — ignore it and use the estimate (prevents the
  immediately-re-trigger-after-compacting bug Pi patched).

## Deliverables & verification

- Tests: classification table (each retryable status retries, 401/400
  do not, unclassified fails closed); backoff schedule and max-attempts
  exhaustion → TurnFailed with the LAST error; retry event items mirrored;
  tools not re-executed across a mid-loop retry (call-count locked);
  overflow → compact+retry-once → second overflow fails; SDK pre-turn
  retry (scripted connect failure then success) and no mid-turn retry;
  timeout: turn ends `interrupted`/`"timeout"` at deadline, watchdog
  cancelled on normal completion, per-call override honored and validated;
  trigger: reported-usage path, trailing estimate, staleness guard,
  SDK turns still zero `turn_usages` reads.
- Docs: REQUIREMENTS (FR-6.6 resilience; FR-8.1 flag list; FR-5.8 trigger
  note), ARCHITECTURE §3.2 (`retry_scheduled` event item; timeout
  terminal), components 01/03, README, CHANGELOG, RUNBOOK.
- Version: minor bump proposed (0.8.0 → 0.9.0) with a BREAKING footer for
  the required `supports_turn_retry` capability field; confirmed before
  applying.

## Out of scope

Retrying SDK turns after first event (unsafe, permanently); retry-after
header honoring (Pi's provider layer — revisit with real 429 data in 2c);
`RetrySettings` per-call overrides (config-wide only this phase); the 2c
live validation itself.
