# RUNBOOK

Per GUIDELINES §5: lessons learned during execution, reviewed before starting new work
to avoid repeating past mistakes. This is the phase-1 (tasks 1–16) close-out record.

## Conformance matrix — final evidence (task 16, 2026-09-02)

Full matrix run against `tests/conformance/matrix.py`'s seven scenarios
(`single_turn_text`, `tool_allow_deny`, `interrupt_midturn`,
`resume_continues_context`, `history_flat_and_tree`, `structured_output`,
`system_prompt_respected`), one backend per column. `claude`/`codex` cells
are from real subscription-authenticated live runs this session; `langchain`
is the fake-model conformance suite (`tests/conformance/test_langchain.py`,
always runs in `check.sh`) plus its live-run status; `cursor` is the
permanent self-skip.

| Scenario | langchain | claude | codex | cursor |
|---|---|---|---|---|
| `single_turn_text` | PASS (fake model, `check.sh`) | PASS | PASS | BLOCKED: no cursor subscription |
| `tool_allow_deny` | PASS | PASS | PASS | BLOCKED: no cursor subscription |
| `interrupt_midturn` | PASS | PASS (flaky — see below) | PASS | BLOCKED: no cursor subscription |
| `resume_continues_context` | PASS | PASS | PASS | BLOCKED: no cursor subscription |
| `history_flat_and_tree` | PASS | PASS | PASS | BLOCKED: no cursor subscription |
| `structured_output` | SKIP (`supports_structured_output=False`) | SKIP (`supports_structured_output=False`) | PASS | BLOCKED: no cursor subscription |
| `system_prompt_respected` | PASS | PASS | PASS | BLOCKED: no cursor subscription |

Commands:

```bash
bash scripts/check.sh                                                        # hermetic; includes langchain conformance
TRADEWIND_RUN_CLAUDE_INTEGRATION=1 uv run pytest tests/conformance/test_claude.py -v -m integration
TRADEWIND_RUN_CODEX_INTEGRATION=1  uv run pytest tests/conformance/test_codex.py  -v -m integration
uv run pytest tests/conformance/test_cursor.py -v      # always self-skips; no env gate exists
```

**This session's runs (2026-09-02):**

- `bash scripts/check.sh` — **306 passed, 7 skipped**, 0 failed.
- Claude live matrix — first attempt: **5 passed, 1 failed** (`interrupt_midturn`), 1 skipped
  (`structured_output`, capability-gated). Isolated re-run of `interrupt_midturn` alone:
  **failed again**. Full re-run (third attempt): **6 passed, 1 skipped**, 0 failed — clean,
  including `interrupt_midturn`. Recorded honestly per GUIDELINES §13.6 rather than reporting
  only the clean run: see "Known live flakiness" below, this is a documented pre-existing
  characteristic (tasks 10/11), not a new regression, but this session's two-failures-then-pass
  pattern is worse than the "occasional" flakiness those tasks described and is flagged for
  follow-up if it recurs.
- Codex live matrix — **7 passed**, 0 failed, 0 skipped, first attempt, clean.
- `tests/integration/test_claude_live.py` (direct-adapter smoke test) — 1 passed, clean.
- `tests/conformance/test_cursor.py` — 1 skipped (module-level `BLOCKED` skip, expected, no
  Cursor subscription on this machine).
- langchain: no `GROQ_API_KEY`/`ANTHROPIC_API_KEY` on this machine, so
  `tests/integration/test_langchain_live.py`'s two live-model tests remain **DEFERRED**
  (both self-skip; part of `check.sh`'s 7 skips). The langchain adapter's own conformance
  coverage (`tests/conformance/test_langchain.py`, fake `_ScriptedChatModel`) runs and passes
  on every `check.sh` invocation — see the matrix row above.

## Known live flakiness

**`interrupt_midturn` on `claude` (and, less often, `resume_continues_context`) — live-model
instruction-compliance variance, not an adapter defect.** Root-caused in tasks 10 and 11
(see `.superpowers/sdd/2026-09-02-tradewind-implementation/task-10-report.md`,
`task-11-report.md`): the scenario sends `"hi"` plus an appended system-prompt instruction
("count to 1000" / "reply verbatim"), then interrupts (or checks history) expecting the model
to still be following that instruction when the check runs. The `claude_code` preset's own
persona sometimes wins over the appended instruction — the model replies with a short greeting
and finishes the turn naturally before the interrupt round-trip lands, or produces extra
`kind="thinking"` items the exact-shape assertions don't expect. `ClaudeSDKClient.interrupt()`
itself was independently verified correct in isolation (task-10: `terminal_reason=
"aborted_streaming"`, no `TurnCompleted`/`TurnFailed` leaking through when a turn genuinely
gets interrupted). Mitigation if this becomes disruptive: tighten the scenario's instruction
wording, or lengthen the "still working" window the harness waits on before calling `stop()`
— out of scope for this close-out task; flagged here for whoever revisits `matrix.py`.

**Codex**: no comparable live flakiness observed — 7/7 clean on every live run recorded
across tasks 13, 14, and this close-out session.

## Operational notes

### CLI interop (FR-6.4) — evidence status

- **`claude --resume`**: manually verified (task 10 report, "Verified SDK facts"): a session
  created by `ClaudeBackend` was independently resumed via `claude --resume <native id>` from
  the vendor CLI and continued the same conversation. Not written to a RUNBOOK entry at the
  time (this repo's `docs/RUNBOOK.md` didn't exist until this task) — recorded here now,
  retroactively, from the task-10 report.
- **`codex resume`**: manually verified (task 14 report, "Manual `codex resume` evidence"): a
  thread created by `CodexClient.thread_start()` in a standalone script, told a secret number
  in turn 1, was independently resumed via `codex exec resume <thread-id> "..."` from the
  bundled CLI and correctly recalled the secret — confirming the SDK-created native thread is
  a real, CLI-interoperable Codex session.
- **`cursor-agent` CLI ↔ SDK store sharing**: **unverified** — P-5 spike blocked, no Cursor
  subscription on this machine (task 15). `docs/ARCHITECTURE.md` §7 P-5 stays open.

### Codex FR-6.4 backfill limitation (confirmed live, task 14)

Live-streamed Codex `item/completed` notifications carry the model provider's own item id
(`msg_...`/`rs_...`); the *same logical item* read back later via `thread_read(includeTurns=
true)` comes back with a different, sequentially-renumbered id (`item-1`, `item-2`, ...), and
reasoning items are dropped from that persisted view entirely. `ResumePlanner.reconcile()`
always diffs against the mirror's last known native id, which — given the id-scheme mismatch
— can never match a `thread_read` id past the first turn. `CodexBackend.thread_read_items` was
fixed to treat a cursor miss as "return nothing" (not "return everything", which is
`ClaudeBackend`'s rule and previously caused unbounded duplication on Codex too) — this trades
away Codex's ability to backfill genuinely-new out-of-band native activity (DR-3, e.g. a human
resuming the same thread from the `codex` CLI) in favor of never corrupting the mirror.
`supports_transcript_read=True`/`supports_native_resume=True` stay set (resume, history,
interrupt, structured output, and tool-gating all verified working live) — this is a
data-completeness gap in one specific reconcile path, not a broken capability. Flagged as a
real open architectural item; see `docs/ARCHITECTURE.md` §7 for the pending-decision entry.

### Cursor — P-5 blocked (task 15)

The `cursor` adapter is implemented, unit-tested, and wired (capabilities, mapping, R-1
system-prompt emulation to `.cursor/rules/tradewind-session.mdc`), but ships marked
**`EXPERIMENTAL`** (module docstring) because it has never run against a live Cursor session:
no Cursor account/subscription exists on this machine, so `cursor-sdk-bridge` cannot
authenticate. Both the P-5 spike (whether `cursor-agent` CLI can resume an SDK-created agent)
and the live conformance run are `BLOCKED: no cursor subscription`, not faked or stubbed as
passed. `tests/conformance/test_cursor.py` self-skips unconditionally at module level; remove
that skip once a subscription exists (see the file's own docstring for the exact steps).

### Socket security posture (toolproxy shim, task 12)

The `ToolHost` unix-domain socket (`serve_socket()`/`shim_server_def()`) that proxies tool
calls from a spawned subprocess (Codex, and any future subprocess-shim backend) back to the
live tool registry in the host process has **no authentication of its own** — anything that
can connect to the socket can invoke every registered tool. Mitigated by filesystem
permissions and the same-user assumption:
- The socket's containing directory is created `0o700` (`serve_socket()` explicitly `chmod`s
  it past a permissive umask) whenever `ToolHost` creates the directory itself; a
  caller-supplied `socket_dir` that already exists is left untouched (documented choice, not
  an oversight — see `tool_host.py`'s own "Security" docstring paragraph).
- This is a same-machine, same-user security boundary, not a network one: any other local
  process running as the same OS user can still connect. Acceptable for the local-subscription
  deployment profile (`FR-10.1`); revisit before ever exposing the socket path across a
  container/user boundary.
- Socket filenames are kept short (`{uuid4().hex[:8]}.sock`) to stay under the ~104-byte
  `sockaddr_un.sun_path` cap on macOS when `socket_dir` is deeply nested.

### Env gates for integration/live runs

| Variable | Gates | Notes |
|---|---|---|
| `TRADEWIND_RUN_CLAUDE_INTEGRATION=1` | `tests/conformance/test_claude.py`, `tests/integration/test_claude_live.py` | Requires a real Claude Code subscription login on the machine; real, billed-by-subscription API calls (~40s for the full matrix). |
| `TRADEWIND_RUN_CODEX_INTEGRATION=1` | `tests/conformance/test_codex.py` | Requires a real ChatGPT/Codex subscription login; spawns real `codex app-server` subprocesses (~100s for the full matrix). |
| `ANTHROPIC_API_KEY` | `tests/integration/test_langchain_live.py::test_...anthropic` | Real Anthropic API key; DEFERRED — none available yet on this machine. |
| `GROQ_API_KEY` | `tests/integration/test_langchain_live.py::test_...groq` | Free-tier Groq key (`console.groq.com`, `ChatGroq`, `llama-3.3-70b-versatile`); DEFERRED — none available yet. |
| (none — Cursor) | `tests/conformance/test_cursor.py` | No env gate exists; the module skips unconditionally until a Cursor subscription is available and the skip is removed by hand. |

None of these are read by the Tradewind library itself (NFR-5/FR-10.4: no env reads by the
library) — they are read only by the test files that need real credentials to run.

## Lessons

- **A conformance scenario written against one fake model silently encodes that model's
  quirks as the spec.** `matrix.py`'s original exact-history-list assertions
  (`single_turn_text`/`resume_continues_context`/`history_flat_and_tree`) were only ever
  validated against langchain's `_ScriptedChatModel`, which never emits a `kind="thinking"`
  item — so they broke against every real model (Claude's `claude_code` preset emits thinking
  blocks on nearly every turn). Generalized in task 11 to invariant-based assertions (prompt
  present, exactly one assistant reply, ordering) with a `_non_thinking()` filter, instead of
  an exact item list. Write conformance scenarios against invariants from the start when more
  than one real backend will exercise them.
- **A tool schema with no `properties` is technically valid JSON Schema but tells a real model
  nothing about parameter shape.** `{"type": "object"}` alone let both the Claude adapter's own
  schema-normalization bug (task 10 fix round 1: bare `{"type": "object"}` got reinterpreted by
  the SDK's shorthand-detection as a parameter literally named `type`) and plain model
  guessing (string `"1"` instead of int `1`) hide behind "the model complied roughly." Real
  `properties` with typed fields removes an entire class of live-conformance false failures.
- **A single shared instance-attribute for "the last native id this backend saw" leaks across
  sessions.** `ClaudeBackend` originally kept `last_native_session_id` as one attribute on an
  adapter instance that `Tradewind._resolve_backend` caches and reuses across every session on
  a profile — session A's native id could get written onto session B's row if B failed before
  producing its own result. Fixed by keying a dict on tradewind `session_id` and popping
  (never just reading) the entry, so a value is consumed exactly once by the turn that produced
  it. Any "last thing this backend instance saw" state needs to be scoped per session, not per
  adapter instance, the moment the adapter instance is shared/cached.
- **Two ids that look interchangeable (a live-stream item id vs. a transcript-read item id for
  the same logical item) are not the same value just because both are called `id`.** Confirmed
  on Codex (task 14): assuming they were the same, as `ClaudeBackend`'s own precedent correctly
  assumes for its two id sources, caused unbounded history duplication the first time a session
  reconciled. Verify id-scheme identity empirically before reusing a cross-adapter assumption,
  even one that held for a different adapter.
- **A per-turn subprocess-shim adapter (Codex) needs its own broker gate at the point tool
  calls actually arrive**, not just relayed through the approval-event bridge. Codex's shim
  path auto-accepts every call to tradewind's own MCP shim server (by design — `ToolHost.call()`
  gates authoritatively a moment later over the socket), but that division of labor initially
  meant a denied call produced a correct error result with **no `PermissionRequested` event**
  anywhere (FR-4.1 violation, task 14 fix round 1). Any adapter with more than one path a tool
  call can take to the model needs an explicit audit of "does every path that can deny still
  emit the event," not just "does the happy path."
- **The rules-file/fold system-prompt emulation for a backend without native system-prompt
  support must know which turn it's on.** Cursor's original wiring (task 15 fix round 1) reused
  one `prompt` variable for both what got persisted to the mirror and what the backend
  received, so the `[Instructions]/[Task]` fold wrapper got written into session history as if
  the caller had typed it, and re-applied on every turn instead of only the first. Fixed by
  keeping two variables (`backend_prompt` vs. the caller's original `prompt`) and gating the
  fold on `is_first_turn`. Emulation logic that touches what gets sent to a backend must never
  share a variable with what gets persisted as the caller's own words.
- No `CHANGELOG.md`/`PROJECT.md`/version bump exist in this repo as of phase 1 close-out
  (confirmed absent since task 9, unchanged through task 16) — GUIDELINES §5/§11 name them as
  house conventions; flagged again here in case phase 2 wants them started, matching the
  precedent every task report through 15 already recorded rather than introducing them
  unilaterally at close-out.
