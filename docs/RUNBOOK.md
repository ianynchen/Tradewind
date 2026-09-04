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
  `tests/integration/test_langchain_live.py`'s Groq live test **PASSED on 2026-09-02** (`ChatGroq(model="openai/gpt-oss-120b")` via the free tier; the originally planned `llama-3.3-70b-versatile` was retired by Groq and the test updated). The Anthropic-API variant **PASSED on 2026-09-02** (real API key in git-ignored `.env`; runs on `claude-haiku-4-5` (bumped from the EOL'd 3.5 pin before the first successful run))
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

### Codex sandbox default when no broker is configured (final fix wave)

`CodexBackend` used to default `sandbox` to `workspace-write` unconditionally (module
docstring's "Approval/sandbox defaults" section) — fine when a `PermissionBroker` actually
gates tool calls, but with no broker configured anywhere (`SessionOptions.permission_broker`
and `TradewindConfig.permission_broker` both unset, the common case — `turn_runner.py`'s
`_AllowAllBroker` fallback then permits every call), `workspace-write` meant unrestricted
filesystem writes with no human-in-the-loop backstop at all. Controller ruling ("disclose AND
safe default"): `CodexBackend._run_turn` now detects that exact case — a duck-typed
`is_default_allow_all` marker on `_AllowAllBroker`, checked via `getattr(ctx.broker, ...)` so
the adapter never has to import `application.turn_runner` itself — and defaults `sandbox` to
`read-only` instead. An explicit `Profile.backend_options["sandbox"]` still always wins,
whichever broker is or isn't configured. See README.md's "Security defaults" section for the
caller-facing summary.

### Env gates for integration/live runs

| Variable | Gates | Notes |
|---|---|---|
| `TRADEWIND_RUN_CLAUDE_INTEGRATION=1` | `tests/conformance/test_claude.py`, `tests/integration/test_claude_live.py` | Requires a real Claude Code subscription login on the machine; real, billed-by-subscription API calls (~40s for the full matrix). |
| `TRADEWIND_RUN_CODEX_INTEGRATION=1` | `tests/conformance/test_codex.py` | Requires a real ChatGPT/Codex subscription login; spawns real `codex app-server` subprocesses (~100s for the full matrix). |
| `ANTHROPIC_API_KEY` | `tests/integration/test_langchain_live.py::test_...anthropic` | Real Anthropic API key; ACTIVE — key in git-ignored `.env`, run via `scripts/live-tests.sh`; passed 2026-09-02. |
| `GROQ_API_KEY` | `tests/integration/test_langchain_live.py::test_...groq` | Free-tier Groq key (`console.groq.com`, `ChatGroq`, `openai/gpt-oss-120b`); ACTIVE — key in git-ignored `.env`, run via `scripts/live-tests.sh`; passed 2026-09-02. |
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
- **`requires-python` metadata gates downstream resolvers on the declared *range*, not the
  interpreter actually in use.** Sextant (declares `>=3.12`, runs 3.14) could not resolve
  tradewind at `>=3.13` even though every interpreter involved satisfied it — uv resolves for
  the whole declared range. Confirmed the floor was pure metadata: no 3.13-only construct in
  source, deps resolve on 3.12, and the full suite + mypy strict + ruff + import-linter pass
  on 3.12 unchanged. Keep the floor as low as the code actually needs, and when changing it,
  update `pyproject.toml` (`requires-python`, mypy `python_version`, ruff `target-version`),
  `uv.lock`, README, and ARCHITECTURE §6 together.
- **P-7 CLOSED (Phase 4)** — and one design lesson from it: the REPLAY preamble is a
  DETERMINISTIC rendering (serialize the compacted view, budget-capped) rather than a
  model-generated summary — zero billing, unit-testable, no summarizer-model
  dependency on SDK-only profiles, and it composes with existing compaction
  checkpoints for free. Codex's reactive degrade uses a worker→loop request/reply
  queue pair so the replay prompt is built LAZILY on the event loop (zero mirror
  reads on healthy turns) without relaunching the worker thread and disturbing its
  cleanup finally.
- **Automated string-replaces anchored on comments are fragile across formatter
  runs** (Phase 3): the langchain terminate-break block silently failed to insert
  because its anchor comment had been re-wrapped since Phase 1 — the script printed
  success (other replacements matched) and only the behavior test caught the hole.
  After any scripted multi-replacement, grep for EACH inserted marker, not just
  exit status.
- **FR-4.4 shim-socket limitation (recorded, not silent):** a broker
  `Denial(terminate=True)` on codex's in-process-tool path (ToolHost over the unix
  socket) delivers the deny + reason but cannot end the turn — ToolHost has no
  handle to interrupt. The approval-handler paths (exec/patch/external MCP) do
  terminate. Revisit if sextant needs socket-path terminate.
- **Phase-2c LIVE VALIDATION PASSED (2026-09-03)** — the first real-model exercise of
  compaction, and the "prompt quality unproven" caveat from the Phase-1 report is
  RESOLVED. Setup: haiku (`claude-haiku-4-5`) with declared `context_window=3000`
  (the cheap-window trick), reserve 700 / keep_recent 400, 10 fact-bearing turns of
  ~400 tokens each (`tests/integration/test_compaction_live.py`, gated, re-runnable
  via `scripts/live-tests.sh`). Evidence: TWO chained compaction records (both
  structured `##` checkpoints, ~0.9–1.1k chars); the trigger fired at threshold on
  provider-reported numbers both times (context 2017→trip at est. 2390→1096 after;
  regrow 1893→trip→1186) — **no calibration change needed**; the memory probe
  recalled fact #1's codeword EXACTLY after it had been summarized away through two
  chained checkpoints; `tw.usage()` on live traffic: turns $0.0152 + summarizer
  $0.0063, disjoint. Whole run ≈ $0.022.
- **Codex cost-mapping assumption VERIFIED live (2026-09-03)** — two real codex turns:
  `cached_input_tokens ⊆ input_tokens` held on both (4352≤15956, then 19968≤36250
  with the cache warming turn-over-turn), and `total_tokens = input + output` with
  reasoning a subset of output. The FR-10.5 mapping (`input − cached` at input rate,
  `cached` at cache-read rate) stands as written.
- **`turn_usages()[-1]` during a turn is the turn ITSELF** — the runner opens the
  turn row before fetching, so the trigger upgrade initially read its own empty
  in-progress row instead of the last finished turn. Filter `status != "in_progress"`
  when "last turn" means "last FINISHED turn"; caught by the trigger test.
- **An accuracy upgrade can 'break' tests that depended on the old inaccuracy** —
  the provider-usage trigger stopped a rollup test's compaction from firing because
  the reported 40 tokens (correct) replaced a chars/4 estimate of ~101 (conservative
  overcount). When replacing an estimate with ground truth, audit every test that
  relied on the estimate's bias, and re-tune scenarios rather than loosening asserts.
- **Split turns keep eating scripted responses** (third occurrence): the overflow
  tests' 2-row single-turn history forced the split-turn path and its second
  summarizer call consumed the scripted 'recovered' response. The lesson is now a
  checklist item: any compaction-adjacent scripted test budgets summarizer calls
  FIRST (split turn = two), then turn replies.
- **When strict mypy blocks a content-dict read in the application layer, the read
  usually belongs in the domain** — `tw.usage()`'s summarizer extraction tripped
  `disallow_any_expr` in client.py; moving it to `domain/compaction.summarizer_spend`
  (where record semantics live, under the domain's documented relaxation) was better
  layering, not just a type-checker dodge.
- **Scripted-fake compaction tests must budget a response per SUMMARIZER call, and
  `FakeMessagesListChatModel` cycles its list** — the first Phase-1 integration tests
  under-provisioned: an unexpected split-turn cut consumed a second summarizer response,
  the fake wrapped around, and turn replies shifted by two. When a test's fake serves
  multiple consumers (turn loop + summarizer), count the calls per path (a split turn
  costs TWO) and script exact-length messages so the chars/4 trigger fires on the
  intended turn only.
- **FR numbering slipped a THIRD time** (FR-10.5 inserted above FR-10.4) — the grep
  habit exists but the insertion-anchor habit doesn't: anchor the Edit on the LAST
  existing bullet of the section, not the section header or the following heading.
- **`append_message`'s returned seq is a free "is this the session's first turn"
  oracle** — the prompt landing at seq 1 IS the empty-history fact, so FR-9.3's
  lazy loading could drop the eager whole-history read the R-1 emulation used to
  justify. When a read exists only to answer a yes/no question, look for a write
  that already returns the answer.
- **The Unreleased section of CHANGELOG.md silently accumulated four shipped
  versions** (0.2.0–0.5.0 all bumped pyproject without rolling the changelog) —
  caught while adding FR-9.3's entry. When bumping, roll `[Unreleased]` into a
  dated `[x.y.z]` section in the same commit; nothing enforces this, so it is a
  checklist discipline (now also a DoD reminder via this note).
- **Never hold an anyio `CancelScope` (or task group) open across a `yield` in an
  async generator.** `LangchainBackend.run`'s `with scope: async for ...: yield`
  passed every in-repo test yet failed sextant's first real integration: a consumer
  that abandons the stream (e.g. `Session.run` raising on `TurnFailed`) leaves the
  generator to asyncio's async-generator finalizer, which delivers `GeneratorExit`
  from ITS OWN task — the scope then exits in a task it wasn't entered in. The safe
  shape: run the work in a dedicated task that owns the scope end-to-end, stream
  events out through a memory channel; an anyio task group is NOT a fix (it is
  itself a scope across the yields). Regression-locked by
  `test_run_survives_being_closed_from_a_different_task` (proved failing pre-fix).
- **A `TurnFailed` whose error is `str(exc)` is only as good as the exception's
  message** — `str(NotImplementedError())` is empty, so `bind_tools` on a model
  without tool support produced `TurnFailed("")`. When wrapping third-party raises
  into event errors, catch the specific case and write the message yourself (§9).
- **Scripting langchain fakes: `GenericFakeChatModel` cannot script tool-call
  turns** — it streams by splitting message *content*, so a content-empty
  tool-call message yields zero chunks ("No generation chunks were returned"),
  and it has no `bind_tools`. Use `FakeMessagesListChatModel` (invoke-based; the
  whole message, tool_calls included, arrives as one chunk) subclassed with a
  no-op `bind_tools` returning `self` — the repo's `_ScriptedChatModel` pattern.
- **Never batch-rewrite code with a bare-substring regex.** The `StoreConfig`
  migration's `re.sub("StoreConfig, ", ...)` also matched *inside*
  `NativeStoreConfig, ` — producing `NativeTradewindConfig` imports and
  `native_config: Nativeevents:` signatures across six test files. Use word
  boundaries (`\bStoreConfig\b`) or AST-aware edits, and always run the suite
  immediately after a mechanical rewrite (which is what caught it).
- **`sqlite3` `":memory:"` + a single-connection store = a free ephemeral
  `SessionStorePort`.** `SqliteSessionStore` holds one connection for its
  lifetime, so `SqliteSessionStore(":memory:")` is a complete in-memory
  implementation (FR-5.7/ADR-0001) — no new store class, per-instance
  isolation guaranteed by sqlite's per-connection memory databases
  (test-locked by `test_two_ephemeral_instances_never_share_sessions`).
- **`__version__` in `__init__.py` drifts silently from `pyproject.toml`** —
  it sat at 0.1.0 through two confirmed bumps because nothing checks the two
  agree. Caught while touching the file for ADR-0001. When bumping, grep for
  the version string repo-wide; a follow-up could assert equality in a test.
- **Check a spec's numbering before citing a new requirement id.** FR-6.5's first draft
  cited "FR-6.3" in fourteen code comments — but FR-6.3 already existed (native-store
  durability); grep REQUIREMENTS.md for the id before writing it into code. Caught
  before commit this time.
- **A "reserved" enum member needs an emitter or a definition before it spreads to new
  contracts.** `TurnStatus`'s `"cancelled"` has neither: no backend distinguishes cancel
  from interrupt (each SDK has exactly one abort primitive — claude `interrupt()`,
  codex `TurnInterruptRequest`, cursor `run.cancel()` — and the runner normalizes all of
  them to `"interrupted"`, including cursor's own wire word "cancelled"). `EndReason`
  (FR-6.5) therefore deliberately does NOT mirror it; user-confirmed 2026-09-02. Open
  follow-up: decide whether `TurnStatus."cancelled"` gets defined semantics or is
  dropped at the next breaking rev.
- **The claude CLI reports its `max_turns` stop as an *error* result** (`subtype=
  "error_max_turns"`, `is_error=True`, `terminal_reason="max_turns"`). Since tradewind
  only ever sets `max_turns` from the caller's own `max_tool_rounds`, that "error" is the
  caller's requested cap working — `_drive_client` must reclassify it as an honest
  `max_tool_rounds` completion BEFORE the generic `is_error` → `TurnFailed` branch.
- **RESOLVED (PR #2): the 50ms wall-clock perf test is rewritten as deterministic
  invariants.** Root cause of the flake (three occurrences: twice under local load, once on
  a shared CI runner at 163ms): an absolute stopwatch budget only ~3x above the ~17ms median
  runtime, when scheduler noise alone spans more than that — 58.9ms was observed on an IDLE
  dev machine within 15 runs. A wall clock measures the machine's momentary load, not the
  code. The rewrite asserts what the budget was a proxy for: exactly one SQL statement
  (trace callback), `raw_json` absent from that statement (NFR-1), an index SEARCH not a
  SCAN (`EXPLAIN QUERY PLAN`), plus a 2s catastrophic bound (~100x headroom). Lesson:
  **a perf assertion whose budget is within one order of magnitude of the typical runtime
  on dedicated hardware WILL flake on shared hardware — assert the mechanism (statement
  count, plan, selected columns), keep wall-clock only as a catastrophic bound.** The
  `perf` pytest marker is now unused; kept for future genuine benchmarks.
- `CHANGELOG.md` started and first version bump applied (0.1.0 → 0.2.0, user-confirmed) with
  the 3.12-floor change — the flag in the bullet below is resolved; versioning now follows
  GUIDELINES §11 as written. `PROJECT.md` still absent.
- No `CHANGELOG.md`/`PROJECT.md`/version bump exist in this repo as of phase 1 close-out
  (confirmed absent since task 9, unchanged through task 16) — GUIDELINES §5/§11 name them as
  house conventions; flagged again here in case phase 2 wants them started, matching the
  precedent every task report through 15 already recorded rather than introducing them
  unilaterally at close-out.
- **Each `:memory:` SQLite connection is a PRIVATE database.** Verified while
  adopting yoyo (ADR-0003): yoyo opens its own connection, so pointing it at
  the ephemeral store would have migrated a throwaway database while the
  store's own connection saw nothing — a silent no-op, not an error. Any tool
  that "connects to the database by URI" is incompatible with an in-memory
  store held open by someone else's connection; it must be handed the
  connection, or the work must be done directly on it.
- **A shared migration-bookkeeping table fails SILENTLY, not loudly.** yoyo
  keys migrations by filename-derived id, so two libraries co-embedded in one
  service, sharing `_yoyo_migrations` and both shipping `0001_schema`, would
  make the second one SKIP its own migration and report success — surfacing
  much later as `no such table`. Namespace migration ids and name the
  bookkeeping table per-library; on Postgres, scope everything to an owned
  schema via `search_path` (only `_yoyo_migrations` is renameable — `_yoyo_log`,
  `yoyo_lock`, `_yoyo_version` are fixed class attributes).
- **Ownership and execution are different questions.** An early framing in
  this repo's discussion held that the embedding service should apply
  tradewind's migrations because tradewind is a library. That conflated *who
  owns the schema* (tradewind) with *who runs the DDL* (also tradewind, inside
  the repository the host grants). `binnacle` had already solved this as a
  library; check sibling projects for precedent before designing from first
  principles.
- **yoyo 9.0.0 emits a `DeprecationWarning`** on Python ≥3.12 for sqlite's
  default datetime adapter (its own `backends/base.py`). Cosmetic today and
  harmless on the supported 3.12/3.13 floors, but it is a removal candidate in
  a future Python — revisit if the floor rises.
- **A vendored JS bundle can settle a question the typed Python API cannot.**
  Cursor's `SummaryStartedUpdate`/`SummaryUpdate`/`SummaryCompletedUpdate`
  looked like "a summary of the work done"; the Python types say nothing either
  way. Reading the vendored `@cursor/sdk` bundle settled it: its persisted
  conversation model carries `summary`, `summary_archive(s)`,
  `self_summary_count` and `message_count_at_last_compaction` in ONE message,
  and the SDK filters these three events out of the conversation delta flow --
  so summary IS cursor's compaction. When a wrapper SDK is too thin to answer a
  semantic question, the bundled implementation underneath usually can.
- **A thin wrapper SDK can silently drop wire fields.** Cursor's
  `summary-completed` carries `hookMessage` and `failed` on the wire, but the
  Python parser builds `SummaryCompletedUpdate(type=...)` and discards the
  payload -- so compaction success/failure is unreachable without going behind
  the SDK. Check the wire form before promising a field to callers.
- **Ask what the observer cannot derive.** The first design for the engine
  compaction record carried `source` ("tradewind"/"engine") and `backend`.
  Both restate what the caller already holds: with `session.stream()` you know
  the session, with `tw.history(session_id)` you queried by it, so the profile
  and backend follow. Dropping them also kept the langchain record's shape
  byte-identical, so `CONTENT_SHAPE_VERSION` needed no bump and no shape
  migration -- a field you do not add is a migration you do not write.
