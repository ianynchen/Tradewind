# Phase 4: REPLAY — NATIVE→REPLAY degrade and cross-backend continuation

Date: 2026-09-03 · Status: APPROVED (user, 2026-09-03) · Parent:
`2026-09-03-improvement-roadmap.md` (item G). Closes ARCHITECTURE **P-7**
and delivers FR-6.1's unkept promise: "NATIVE is attempted first; failure
degrades to REPLAY, never to an error that loses the conversation."
ARCH §5.2 point 2 already specifies the shape ("inject a
rendered/summarized transcript into a fresh native session and record the
new native id"); this phase picks the RENDERED branch.

## 1. Replay context = deterministic rendering, not a model call

The injected preamble is the mirror's compacted view serialized to plain
text (`domain/compaction.serialize_for_summary` — the exact
serialization the live-validated summarizer consumes), newest-first
budget-capped (`CompactionSettings.keep_recent_tokens`-style, with a
"[earlier history truncated]" marker when cut), wrapped:

```
[Resumed session: the prior conversation was restored from tradewind's
mirror after the native session became unavailable.]
<transcript>
[User]: ...
[Assistant]: ...
</transcript>

<the caller's actual prompt>
```

Chosen over a model-generated summary: zero extra billing, deterministic
and unit-testable, no summarizer-model dependency on SDK-only profiles,
and ARCH §5.2's "rendered" branch makes it spec-conformant as-is. (A
session that was mirror-compacted contributes its checkpoint text
automatically via `compacted_view` — the two features compose.)

## 2. Detection — reactive at the resume step, fail-closed

The degrade decision happens where it is provably side-effect free: the
native resume/connect step, before any model response or tool call (the
same safe zone as FR-6.6's pre-turn retry).

- **claude** bonus: its native store is a LOCAL jsonl, so `probe_native`
  is upgraded from truthy-id to a real existence check (the
  `get_session_messages` path resolving) — deterministic pre-detection,
  no error-string matching. A resume that still fails after a true probe
  falls to the reactive rule below.
- **codex / cursor**: reactive — the resume error is classified
  conservatively (not-found / no-such / expired / archived patterns per
  engine). An UNCLASSIFIED resume error stays `TurnFailed` (fail-closed:
  tradewind never silently abandons native context on an ambiguous
  error). Cursor rides along EXPERIMENTAL (P-5 unchanged).
- After degrading: the adapter starts a FRESH native session with the
  preamble-injected prompt; the new native id flows through the existing
  `take_native_session_id` → `rehome_native` machinery, which already
  preserves the superseded id in `native_history` (append-only).

## 3. Cross-backend continuation (FR-10.2) — same machinery, planned not reactive

When the session row's recorded `backend` differs from the profile's
resolved backend (config changed between runs), the runner routes REPLAY
up front: the stale `native_session_id` is NOT passed to the new backend;
target **langchain** rebuilds losslessly from the mirror (its normal
path — engine-specific kinds render via the serializer's pass-through
line); target **SDK** gets the same rendered-preamble injection into a
fresh native session. Rehome then re-homes the row onto the new
`(backend, native id)`.

## 4. Wiring (closes the "no callers" note)

- `ResumePlanner.plan()` finally gets its caller: `TurnRunner.execute`
  consults it — `"native"` (same backend, id present, probe passes),
  `"replay"` (cross-backend; or probe says the native store is gone; or
  reactive degrade), `"fresh"` (no native id and empty mirror).
- `TurnContext` gains two lazy fields: `load_replay_history` (always-flat
  closure — scope-"none" SDK turns can still fetch mirror rows, but ONLY
  when actually degrading: zero reads on healthy turns) and
  `force_replay: bool` (the cross-backend route).
- Every degrade is VISIBLE: a mirrored `kind="event"` item
  `{"type": "resume_degraded", "policy": "replay", "reason": ...}` — the
  established zero-taxonomy pattern.

## 5. Honesty rules

- Degrade only on classified loss or planned cross-backend continuation —
  never on ambiguous errors (fail-closed to `TurnFailed`).
- The mirror is never mutated by REPLAY (rows are read, rendered, kept);
  the preamble is the BACKEND prompt only — the mirror records the
  caller's original prompt, never the injection (the R-1 emulation
  precedent, same `backend_prompt` vs `prompt` separation).
- P-6 (codex out-of-band backfill gap) is unaffected and stays open.

## Deliverables & verification

- Tests: preamble rendering (budget cap, truncation marker, compacted
  view composition, caller-prompt untouched in mirror); claude probe
  upgrade (existing file → native; missing file → replay preamble +
  fresh session + rehome; ambiguous error → TurnFailed) via fake SDK
  client; codex thread_resume not-found → replay vs unclassified →
  TurnFailed (worker-thread level); cross-backend routing (recorded
  claude session + langchain profile → lossless rebuild, no native id
  passed, rehome; reverse direction → injection); resume_degraded event
  mirrored; planner unit table; zero replay-history reads on healthy
  turns.
- Docs: ARCHITECTURE — P-7 RESOLVED (§5.2 updated, §7 note), REQUIREMENTS
  FR-6.1 note (rendered branch chosen), components 01, README (a
  "Resume & recovery" section), CHANGELOG, RUNBOOK.
- Version: minor bump proposed (0.10.0 → 0.11.0), no breaking surface
  expected (two additive TurnContext fields); confirmed before applying.

## Out of scope

Model-generated replay summaries (the rendered branch can be upgraded
later without interface change), fixing P-6, cursor live verification
(P-5), and caller-forced `FRESH` (`resume(policy=...)` — a small
follow-up if sextant asks).
