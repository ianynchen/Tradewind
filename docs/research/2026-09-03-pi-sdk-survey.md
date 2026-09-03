# Pi SDK survey: what tradewind could borrow

Date: 2026-09-03. Researched against primary sources only: the official docs at
pi.dev, starting from https://pi.dev/docs/latest/sdk and following its own
navigation. Each claim carries a citation. Compared against tradewind's
`docs/REQUIREMENTS.md`, `docs/ARCHITECTURE.md`,
`src/tradewind/application/ports.py`, and `README.md`.

**What Pi is** (so the comparison is honest about scope): Pi is a *coding
agent* by Earendil Inc.; the SDK is a Node.js/TypeScript package
(`@earendil-works/pi-coding-agent`) giving "programmatic access to pi's agent
capabilities" [PI-SDK]. Architecturally Pi is a **single agent engine over a
multi-provider model layer** — the analogue of one of tradewind's *backends*
(closest to the Claude Agent SDK), not of tradewind itself (a multi-*engine*
abstraction). The borrowable material is therefore mostly in Pi's session
format, context management, retry/event discipline, and model metadata — not
in its overall shape.

Sources (all fetched 2026-09-03; abbreviations used in citations):

- **[PI-SDK]** https://pi.dev/docs/latest/sdk
- **[PI-SESS]** https://pi.dev/docs/latest/sessions
- **[PI-FMT]** https://pi.dev/docs/latest/session-format
- **[PI-COMP]** https://pi.dev/docs/latest/compaction
- **[PI-EXT]** https://pi.dev/docs/latest/extensions
- **[PI-RPC]** https://pi.dev/docs/latest/rpc
- **[PI-JSON]** https://pi.dev/docs/latest/json
- **[PI-SET]** https://pi.dev/docs/latest/settings
- **[PI-PROV]** https://pi.dev/docs/latest/providers
- **[PI-MODELS]** https://pi.dev/docs/latest/models
- **[PI-CUSTPROV]** https://pi.dev/docs/latest/custom-provider
- **[PI-SKILLS]** https://pi.dev/docs/latest/skills

Tradewind references: **[REQ]** `docs/REQUIREMENTS.md`, **[ARCH]**
`docs/ARCHITECTURE.md`, **[PORTS]** `src/tradewind/application/ports.py`,
**[README]** `README.md`.

## Verdict summary

Worth borrowing, in priority order:

- **Compaction machinery for the mirror-fed paths.** Pi's compaction design —
  token-budget cut points that never split a tool call from its result,
  `keepRecentTokens`, chained boundaries across repeated compactions, and the
  compaction recorded as a first-class transcript entry with
  `firstKeptEntryId`/`tokensBefore` [PI-COMP] — is a directly reusable design
  for tradewind's two places that rebuild context from the mirror: the
  `langchain` backend (today replays *unbounded* full history every request
  [README §Backend notes]) and the unimplemented REPLAY summarization (ARCH §7
  P-7). Strongest borrow in this survey.
- **Agent-level retry with explicit retry events.** Pi retries transient
  errors with exponential backoff (`retry.maxRetries=3`, `baseDelayMs=2000`)
  and emits `auto_retry_start`/`auto_retry_end` events [PI-SET, PI-SDK].
  Tradewind has no retry layer at all — a transient provider error is a
  terminal `TurnFailed` [ARCH §3.2]. A Turn-Runner-level retry policy,
  eventized so it is never silent, fits the layering and the no-silent-
  degradation principle exactly.
- **Model metadata: context window + cost table on the tier.** Pi attaches
  `contextWindow`, `maxTokens`, `reasoning`, input modalities, and a `cost`
  object (input/output/cacheRead/cacheWrite per Mtok, with usage tiers) to
  every model [PI-MODELS, PI-CUSTPROV]. Tradewind's `ModelSpec` is model +
  effort only [README §Configuration]; `cost_usd` is populated only where a
  backend reports it. Optional cost/context metadata on the profile's tier map
  would give `langchain` turns a computed `cost_usd` and give compaction (above)
  its trigger threshold.
- **Richer broker verdicts: deny-with-reason and terminate.** Pi's `tool_call`
  gate answers `{ block: true, reason?, terminate? }` [PI-EXT]; tradewind's
  broker returns bare `"allow" | "deny"` [README §Permission broker]. A denial
  *reason* the model can read, and a `terminate` verdict that ends the turn
  instead of feeding an error result, are small, honest, portable extensions.
- **Branch summary on fork** (smaller): Pi summarizes an abandoned branch and
  attaches the summary at the new position [PI-SESS]. Tradewind's
  `fork`/`copy_history` copies verbatim only [PORTS `copy_history`]. An
  optional summarized fork reuses the same summarizer as compaction.
- **Not worth borrowing:** Pi's in-file entry tree (`id`/`parentId`, in-place
  branching) [PI-FMT] — tradewind deliberately chose child-sessions-as-rows
  (ARCH DR-4) and must mirror four engines, three of which it does not control.
  Pi's steer/followUp mid-turn queueing [PI-SDK] is attractive but tradewind
  explicitly placed queueing above the library (ARCH I-5); a capability-flagged
  steer is at most future work.

All pages fetched successfully; none unreachable. One negative finding worth
recording: the Providers page documents auth modes only and "provides no
information about retry policies, failure handling mechanisms, cost reporting"
[PI-PROV] — retry and cost live in Settings and Models respectively.

---

## 1. What Pi is, structurally

Pi's SDK entry point is `createAgentSession({ sessionManager, modelRuntime,
model, tools, customTools, resourceLoader, ... })`, returning an
`AgentSession` with `prompt()`, `steer()`, `followUp()`, `subscribe()`,
`setModel()`, `setThinkingLevel()`, `navigateTree()`, `compact()`, `abort()`,
`dispose()` [PI-SDK]. Providers span subscription OAuth (ChatGPT Plus/Pro,
Claude Pro/Max, GitHub Copilot, xAI, OpenRouter) and 25+ API-key providers
plus cloud platforms and llama.cpp [PI-PROV] — i.e. Pi solves the
subscription-vs-API-key split at the *model-provider* layer inside one engine,
where tradewind solves it at the *engine* layer across four engines (REQ
FR-10, DR-5). Pi also ships three embedding surfaces: the SDK, an RPC mode
("headless operation of the coding agent via a JSON protocol over
stdin/stdout" [PI-RPC]), and a JSON event-stream mode [PI-JSON].

Consequence for this survey: Pi is a candidate *fifth backend* more naturally
than a design template for tradewind's core. That option is out of scope here
(REQ §5 limits providers deliberately) and Pi is Node-only — there is no
Python SDK on any page surveyed — so a Pi backend would mean driving the RPC
mode over a subprocess. Noted, not recommended.

## 2. Sessions: tree files vs. mirror rows

**Pi.** "Sessions auto-save to `~/.pi/agent/sessions/`, organized by working
directory. Each session is a JSONL file with a tree structure" [PI-SESS].
Every entry has an 8-char hex `id` and a `parentId`; "the current position is
the active leaf" [PI-SESS, PI-FMT]. Branching is in-place: `/tree` navigates
within the file, `/fork` creates "a new session from a previous user message",
`/clone` duplicates the active branch into a new file [PI-SESS].
`SessionManager` offers `inMemory()`, `create(cwd)`, `continueRecent(cwd)`,
`open(path)`, `list`/`listAll`, and a tree API (`getEntries`, `getTree`,
`getPath`, `getLeafEntry`, `branch(entryId)`, `branchWithSummary(id, text)`)
[PI-SDK]. The file header is versioned (`"version":3`; "older sessions
auto-migrate on load") [PI-FMT].

**Tradewind.** Sessions are rows in tradewind's own store; forks and subagents
are *separate session rows* with `parent_session_id`/`spawn_kind`, and flat
retrieval is a pure ID-set question (ARCH §4, I-1, DR-4). Fork is
`copy_history(src, dst_row, up_to_seq)` — a verbatim copy with fresh seq
[PORTS]. In-memory operation exists as the no-store default (REQ FR-5.7),
matching Pi's `SessionManager.inMemory()` in spirit.

**Judgment.** The entry-tree is *not* borrowable: it works because Pi owns its
single engine's transcript end-to-end, while tradewind's store is a mirror of
four engines it does not control (store-as-mirror; ARCH Principle 2) and DR-4
already chose rows over interleaving for retrieval semantics. Two adjacent
ideas *are* worth taking. First, **branch summaries**: "pi can summarize the
abandoned branch and attach that summary at the new position" [PI-SESS], with
its own token budget (`branchSummary.reserveTokens`, default 16384 [PI-SET])
and a dedicated `BranchSummaryEntry` whose `fromId` points at the divergence
point [PI-FMT]. Tradewind's fork could grow an optional `summarize=True` that
prepends a summary block instead of copying the full prefix — same summarizer
as compaction (§3), recorded honestly as its own `kind`. Second, the
**versioned session header with auto-migration** [PI-FMT] is a reminder that
tradewind's store schema already has `migrate()` [PORTS] but its *message
content_json* shapes carry no version; cheap to add before an embedder depends
on them.

## 3. Compaction — the strongest borrow

**Pi.** Compaction triggers automatically "when contextTokens > contextWindow
- reserveTokens" or manually via `/compact [instructions]` [PI-COMP].
Mechanism: walk backward until `keepRecentTokens` (default 20,000) accumulate;
summarize everything before the cut with an LLM; append a `CompactionEntry`;
rebuild context as summary + retained tail [PI-COMP]. Three design details are
the valuable part:

1. **Cut-point rules**: cutting mid-tool-result is prevented — "tool outcomes
   must stay with their calls"; oversized split turns get "two summaries"
   which are merged [PI-COMP].
2. **Boundary chaining**: "On repeated compactions, the summarized span starts
   at the previous compaction's kept boundary (`firstKeptEntryId`), not at the
   compaction entry itself" [PI-COMP] — repeated compaction never
   re-summarizes its own summaries.
3. **Honest recording**: the `CompactionEntry` persists the summary,
   `firstKeptEntryId`, `tokensBefore`, optional LLM usage for the compaction
   call itself, and cumulative read/modified file details [PI-COMP]; context
   building "walks from leaf to root, honoring compactions that act as
   checkpoints" [PI-FMT]. Live consumers get `compaction_start`/
   `compaction_end` events [PI-SDK, PI-JSON].

**Tradewind.** The schema reserves `kind='compaction'` for messages (ARCH §4)
— i.e. it can *record* a backend's native compaction — but tradewind performs
none of its own. The two places that rebuild context from the mirror have no
context management at all: the `langchain` backend, where "every request
rebuilds the messages array from the store" [README §Backend notes] with no
bound on growth, and REPLAY, whose "summarized prompt injection into SDK
backends" (REQ FR-6.1) is specified but unimplemented (ARCH §7 P-7 — a reaped
native store currently surfaces as `TurnFailed`).

**Judgment.** This fits tradewind unusually well. It lives entirely *above*
the Backend port (the Turn Runner / history-loading layer — the same place
`history_scope` folding already lives, PORTS `TurnContext.load_history`), so
layering is preserved; it applies only where tradewind already owns context
construction (langchain, REPLAY), so it never fakes a capability a native-
resume backend has (honest capabilities — native backends compact themselves);
and recording the compaction as its own mirrored item with `tokensBefore` and
the kept-boundary id keeps the mirror truthful about what the model actually
saw (no silent degradation). Pi's three details above — never split
tool_use/tool_result pairs (tradewind's mirror stores exactly these kinds,
ARCH §4), chain from the previous kept boundary, persist the boundary id — are
the parts to copy. The REPLAY summarizer P-7 needs is the same code path.
Prerequisite: a context-window number per model, which is the next item.

## 4. Model metadata: capabilities, thinking levels, cost

**Pi.** Every model — built-in or user-defined in `models.json` — carries
`contextWindow` (default 128000), `maxTokens` (default 16384), `reasoning`
(bool), `input` modalities, and a `cost` object: per-Mtok `input`, `output`,
`cacheRead`, `cacheWrite`, plus `tiers` applying alternative rates above a
token threshold (e.g. `inputTokensAbove: 272000`) [PI-MODELS]. Custom
providers declare the same `ProviderModelConfig` shape [PI-CUSTPROV]. Thinking
is a symbolic seven-level scale (`off`…`max`) with a per-model
`thinkingLevelMap` that maps each level "to provider values or `null` for
unsupported levels" [PI-MODELS, PI-SDK].

**Tradewind.** `ModelSpec(model=..., effort=...)` is the whole per-tier record
[README §Configuration]; `cost_usd` on a turn is filled "where reported" (REQ
FR-1.3), so backends that report nothing (langchain) leave it null; nothing
anywhere knows a context window.

**Judgment.** Borrow the *metadata*, not the machinery. An optional
`ModelMeta` on each tier's `ModelSpec` (context window, cost table) would (a)
let the langchain adapter compute `cost_usd` from the token usage it already
gets, closing an honesty gap where identical turns are costed on one backend
and blank on another, and (b) provide the `contextWindow - reserveTokens`
trigger for §3. It must stay optional and absent-by-default — inventing
numbers tradewind doesn't know would violate NFR-3; when absent, compaction
simply cannot auto-trigger and `cost_usd` stays null, both honestly. The
`thinkingLevelMap`-with-`null` pattern is also worth noting as precedent:
tradewind's capability flags are per-*backend* (REQ FR-8), while Pi shows
per-*model* capability truth (a level a given model lacks is `null`, never
approximated). Tradewind's `effort` field would hit this the moment one
backend serves models with different effort support; a per-tier "unsupported"
marker is the honest shape when it does.

## 5. Retries and failure taxonomy

**Pi.** Two explicit layers: agent-level retry ("Enable automatic agent-level
retry on transient errors", `retry.maxRetries` default 3, exponential backoff
"2s, 4s, 8s") and provider-level retry (`retry.provider.maxRetries` default 0,
`timeoutMs`, and a cap on server-requested backoff,
`retry.provider.maxRetryDelayMs` default 60s) [PI-SET]. Retries are visible in
the event stream as `auto_retry_start`/`auto_retry_end` [PI-SDK].

**Tradewind.** No retry layer exists at any level; the event taxonomy's only
failure signal is terminal `TurnFailed` (ARCH §3.2), and `TurnDefaults` covers
tier and timeouts only [README §Configuration].

**Judgment.** Worth borrowing, carefully scoped. A retry policy belongs in the
Turn Runner (above the port — adapters stay thin, layering intact) and only
for errors that occur *before any output was mirrored* or with idempotent
resubmission semantics per backend — a mid-turn retry on a native-resume
backend re-runs the provider's own loop and must not be pretended safe where
it isn't (this may itself need a capability flag). Pi's decision to *eventize*
the retry is the part that matches tradewind's principles: a silent retry
would hide latency and duplicate provider spend; `auto_retry_start/end` with
attempt count and cause makes the degradation explicit. Note the frozen event
taxonomy (ARCH §3.2) requires a new P-numbered decision to add members — this
survey flags retry events as a candidate for exactly that.

## 6. Tool registration and permissioning

**Pi.** Tools are declared with `defineTool({ name, label, description,
parameters, execute })` (Typebox schemas) and passed via `customTools`, with a
`tools: [...]` allowlist over built-ins plus `noTools`/`excludeTools`
[PI-SDK]. Gating is the `tool_call` event: "handlers can inspect the tool name
and arguments, then respond with `{ block: true, reason?: string, terminate?:
boolean }` to prevent execution", and "Mutations to `event.input` propagate to
actual execution, allowing argument patching before the tool runs" [PI-EXT].
In RPC mode, extension dialogs "emit an extension_ui_request on stdout and
block until the client sends back an extension_ui_response on stdin with the
matching id" [PI-RPC].

**Tradewind.** Tools are live Python closures registered per session (REQ
FR-2.1, README §Tools); the broker is `decide(tool_name, tool_input) ->
"allow" | "deny"`, may block indefinitely ("ask the human" lives inside it),
and a deny produces a `PermissionRequested` event plus "an error tool-result
the model sees" [README §Permission broker; REQ FR-4].

**Judgment.** Registration is parity — tradewind's dynamic-closure model is
already at least as good, and its MCP-first rendering (DR-2) solves a problem
Pi doesn't have. The borrowable delta is the *verdict vocabulary*: (a)
**deny-with-reason** — tradewind's deny currently synthesizes an error result,
but the broker cannot say *why*; a reason string the model reads converts a
dead-end deny into steerable feedback, and maps cleanly onto every backend
tradewind gates (the deny path is tradewind-owned on all of them). (b)
**terminate** — "deny and stop the turn" vs "deny and let the model continue"
are genuinely different policies (Pi separates them [PI-EXT]); tradewind can
implement terminate portably via its existing `interrupt()` path. (c) **input
rewriting** is the debatable one: it maps onto Claude's `can_use_tool`
(updated-input allow) and tradewind's own loops, but not onto Codex approval
events — so per honest-capabilities it would need a flag
(`supports_input_rewrite`) rather than universal adoption. The RPC
ui_request/response round-trip is parity with FR-4.3's blocking-broker "ask"
path — nothing to take.

## 7. Event stream

**Pi.** `subscribe(listener)` yields: `agent_start`/`agent_end`/
`agent_settled` (whole prompt-to-quiescence), `turn_start`/`turn_end` (one
"LLM turn with tool results"), `message_start`/`message_update`(text_delta /
thinking_delta)/`message_end`, `tool_execution_start`/`tool_execution_update`/
`tool_execution_end`, `queue_update`, `compaction_start/end`,
`auto_retry_start/end` [PI-SDK]. JSON mode keeps "message_update records ...
delta-only" and omits the cumulative message "to keep stream size linear"
[PI-JSON].

**Tradewind.** Six frozen events: `TurnStarted`, `TextDelta`, `ItemCompleted`,
`PermissionRequested` (deny-only), `TurnCompleted` (with `end_reason`),
`TurnFailed` (ARCH §3.2); deltas never reach the mirror (I-3).

**Judgment.** The taxonomies encode the same discipline (deltas linear and
transient; completed items durable — parity), and tradewind's deliberate
minimalism plus `end_reason` honesty (REQ FR-6.5) has no Pi counterpart on any
surveyed page — Pi documents no end-reason distinction between clean finish
and truncation, so on that axis tradewind is ahead. Two Pi members are worth
noting for the next taxonomy review, alongside retry events (§5):
**`tool_execution_update`** (streaming tool progress — tradewind's long
tool calls are silent between dispatch and `ItemCompleted`; Pi streams bash
output as updates [PI-RPC]) and the **agent vs turn span split** — tradewind's
"turn" equals Pi's "agent" span, and tradewind has no event marking model-
request boundaries within a turn, which a `max_tool_rounds` consumer might
reasonably want. Neither is urgent; both would need a P-decision.

## 8. Steering and mid-turn queueing

**Pi.** `steer(text)` and `followUp(text)` queue input during streaming;
`prompt()` during streaming *requires* `streamingBehavior: "steer" |
"followUp"` "otherwise throws"; queue state is observable via `queue_update`
[PI-SDK, PI-RPC]. Steered messages are "delivered after current tool calls
complete" [PI-RPC].

**Tradewind.** One in-flight turn per session; a concurrent run raises
`TurnInProgress`; "queuing, if wanted, is built above the library" (ARCH I-5).

**Judgment.** Tradewind's refusal is principled and should stand for
*follow-ups* (trivially built above the library). *Steering* is different — it
cannot be built above the library, because it must inject between tool rounds
of a live turn. The 2026-09-01 research doc already records native steer on
Codex (`TurnHandle.steer()`); Claude's streaming-input mode and tradewind's
own langchain loop could support it; Cursor could not. So a future
`session.steer()` behind `supports_steering` flags is coherent with honest
capabilities — but it is a feature bet, not a gap-fix, and nothing in
REQUIREMENTS demands it. Recommend recording as an open question, not
building.

## 9. Miscellany: parity and non-borrows

- **In-memory sessions**: `SessionManager.inMemory()` [PI-SDK] ≈ tradewind's
  no-store default (REQ FR-5.7). Parity.
- **Settings layering**: Pi merges global `~/.pi/agent/settings.json` with
  project `.pi/settings.json` and runtime `applyOverrides` [PI-SDK].
  Tradewind's config-object-only, no-file-reads stance (REQ FR-10.4) is the
  opposite by design; nothing to take.
- **Custom entries**: extension-persisted entries that "do NOT participate in
  LLM context" [PI-EXT] have no tradewind analogue, and shouldn't: the mirror
  mirrors the conversation (store-as-mirror), and embedders own their own
  state. Non-borrow.
- **Skills / prompt templates / resource discovery** (`.pi/skills`,
  `AGENTS.md` walk-up, progressive disclosure of SKILL.md) [PI-SKILLS,
  PI-SDK]: engine-side context features tradewind's backends already provide
  natively where they exist; adding a tradewind-level clone would duplicate
  engine behavior. Non-borrow (out of scope, REQ §5 spirit).
- **Provider abstraction**: Pi's `streamSimple(model, context, options)` with
  a strict `start → content → done/error` sequence and a mandatory
  stop-reason check ("if output.stopReason === 'pending' { throw ... }")
  [PI-CUSTPROV] is a tidy single-provider port, but tradewind's Backend port
  already covers a strictly harder problem (four engines, native stores,
  resume). Parity/non-borrow.

## Unverified / negative findings

- All twelve pages listed above were fetched successfully on 2026-09-03; no
  page was unreachable.
- The Providers page contains **no** documentation of retry policies, failure
  handling, or cost reporting [PI-PROV]; those claims here come from the
  Settings [PI-SET] and Models [PI-MODELS] pages respectively.
- No Python SDK exists on any surveyed page — the SDK is Node.js
  (`@earendil-works/pi-coding-agent`, with models via `@earendil-works/pi-ai`)
  [PI-SDK]; embedding Pi from Python would require its RPC or JSON modes
  [PI-RPC, PI-JSON].
- Pi's docs (surveyed pages) document no equivalent of tradewind's
  `end_reason` truncation honesty (REQ FR-6.5), capability flags (FR-8), or
  cross-engine mirror — stated as absence-of-evidence on the surveyed pages,
  not verified absence from the implementation.
