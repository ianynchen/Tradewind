# REQUIREMENTS

Requirements for **Tradewind**, a Python library that drives LLM agent backends behind
one interface. Tradewind is the LLM engine for the sibling projects (meridian, portolan,
binnacle, waypoint): callers hand it a prompt, tools, MCP servers, and a session; it
runs the turn on whichever backend the deployment profile selects and returns a
normalized result and event stream.

Grounding research: `docs/research/2026-09-01-agent-backends.md` (per-claim citations
for every backend capability referenced here).

## 1. Problem Statement

Four viable ways to run an agentic LLM turn exist today — the Anthropic API (via
`langchain-anthropic`), the Claude Agent SDK, the OpenAI Codex SDK, and the Cursor
SDK — each with its own call shape, tool registration, session model, permission
mechanism, and event stream. The owner's services must run on a subscription-backed
local machine today (SDK backends) and on API-key-backed cloud hosts later (API
backend) without the calling code changing. No existing library unifies these four
with honest capability reporting and durable, backend-neutral session history.

## 2. Glossary

- **Backend** — one adapter over a provider SDK/API: `claude`, `codex`, `cursor`, `langchain`.
- **Profile** — a named deployment configuration selecting backends and auth mode (e.g. `local` = subscription SDKs, `cloud` = API keys).
- **Session** — one conversation: Tradewind's durable identity for a thread of turns, mapped to at most one native session/thread/agent id.
- **Turn** — one prompt → final-response cycle within a session, including all tool calls.
- **Native store** — a backend's own persistence (Claude session `.jsonl`, Codex rollouts, Cursor agent store).
- **Mirror** — Tradewind's own copy of the transcript in its database, built from the live event stream.
- **Tool** — a caller-supplied Python callable exposed to the model.
- **Permission broker** — the caller-supplied policy asked before a tool runs.
- **Child session** — a session spawned from another (subagent or fork); recorded with lineage.

## 3. Functional Requirements

### FR-1 Unified agent call
- **FR-1.1** A single call surface MUST accept: prompt, session (new or existing), tools, MCP server definitions, system prompt, model/effort options, and structured-output schema — executing on the profile-selected backend.
- **FR-1.2** Options a backend cannot honor (see FR-8) MUST fail loudly or degrade explicitly per the capability flags; silent dropping is prohibited.
- **FR-1.3** Results MUST be returned both as a normalized event stream (FR-7) and as a collected turn result (final text, usage, cost where reported, status).

### FR-2 Tools
- **FR-2.1** Callers register tools as plain Python callables with typed signatures; registration is per-call and dynamic (no static config files).
- **FR-2.2** Every registered tool MUST be renderable as an MCP server, because Codex accepts tools only via MCP. In-process execution (Claude SDK tools, Cursor `custom_tools`, langchain direct calls) is an optimization, not the model.
- **FR-2.3** For backends that spawn the tool server as a subprocess, tool calls MUST be proxied back to the live registry in the host process (tools remain live closures; nothing is serialized or imported by the subprocess).

### FR-3 MCP servers
- **FR-3.1** Caller-supplied MCP servers (stdio and HTTP) MUST be attachable per call and forwarded to the backend in its native form (Claude `mcp_servers`, Cursor inline definitions, Codex `config_overrides`, langchain client-side adapter).
- **FR-3.2** Backend-specific persistence quirks are Tradewind's job: definitions that a backend forgets on resume (Cursor inline tools/MCP) MUST be re-supplied automatically from the stored session options.

### FR-4 Permissions
- **FR-4.1** A permission broker interface MUST be consulted before tool execution wherever the backend allows interception: Claude `can_use_tool`, langchain's own tool loop, Codex approval events.
- **FR-4.2** Where interception is impossible (Cursor: file-based hooks only), the limitation MUST be declared via capability flags and the closest static mechanism configured; Tradewind MUST NOT claim enforcement it cannot deliver.
- **FR-4.3** When a required tool or permission is absent, the broker's `ask` path surfaces the request to the caller (which may prompt a human) rather than failing the turn outright.

### FR-5 Sessions and history
- **FR-5.1** Tradewind owns a canonical, backend-neutral transcript in its own database (SQLite default; Postgres capable). For `langchain` it is the system of record; for the three SDK backends it is a mirror and the native store remains authoritative for native resume.
- **FR-5.2** The mirror is built from the live event stream at turn time; native-file/`thread_read`/`get_session_messages` import is a backfill path, not the primary path.
- **FR-5.3** Sessions map Tradewind's id to the native id (Claude `session_id`, Codex `thread.id`, Cursor agent id) plus a full options snapshot sufficient to resume (id + re-passed config).
- **FR-5.4** Child sessions (subagents, forks) MUST be stored as their own session rows with `parent_session_id`, `spawn_kind`, and the spawning parent message (`spawned_by_message_id`). Backends that interleave child output into the parent's native stream (Claude sidechains) are split at ingestion.
- **FR-5.5** History retrieval: by session id with `include_children` (flat conversation vs. entire tree via lineage) and `include_raw` (verbatim native payloads) flags. Flat retrieval never contains child-session messages.
- **FR-5.6 Caller-supplied session ids.** The caller mints the session id (UUID; v7 recommended) and passes it for new and existing sessions alike — creation is idempotent and the embedder can record the id before calling. Session acquisition offers three intents: `create` (error if exists), `resume` (error if missing), `ensure` (get-or-create); existence checking is internal, never the caller's job.
- **FR-5.7 Optional persistence.** Configuring a store is optional. With no store configured (`TradewindConfig.store = None`, the default), the mirror is an ephemeral in-memory database private to the client instance: turns, history, single-flight, and reconciliation behave identically within the process, nothing tradewind-side touches disk, and everything is gone at exit. SDK backends (claude/codex/cursor) still write their own native stores and resume natively regardless; a `langchain` session's conversation context then lives exactly as long as the instance. FR-5.1's "system of record"/durability language applies only when a persistent store is configured.

- **FR-5.8 Mirror compaction.** On mirror-fed paths only (`langchain` today; REPLAY when implemented), tradewind can summarize older transcript into a structured checkpoint recorded as a first-class mirror message (`kind="compaction"`, carrying `summary`, `first_kept_seq`, `tokens_before`, summarizer usage) and rebuild later requests from `[checkpoint + retained tail]`. Cut points never split a `tool_use` from its `tool_result`; a cut landing mid-turn (exact, via `turn_id`) summarizes the turn prefix separately and merges. Repeated compaction chains: the previously-kept tail plus newer messages are re-summarized with the old checkpoint supplied as context, never as conversation. Triggering is **automatic** (gated on `CompactionSettings.auto` AND the tier declaring `ModelMeta.context_window`; conservative chars/4 estimate vs `window − reserve_tokens`) and **manual** (`Session.compact(instructions)` — always available on mirror-fed backends, metadata-free; `Unsupported` on native-resume backends, whose engines own their context). A truncated or unusable summary is a hard failure, never a checkpoint (automatic: surfaced as a `kind="event"` item, turn proceeds uncompacted; manual: `CompactionFailed`). The mirror never discards a row — compaction changes what is FED, not what is stored; forks drop compaction records (seq renumbering) and copy verbatim history. Design ported from Pi (MIT; `docs/research/2026-09-03-pi-implementation-notes.md` §1).
- **FR-5.9 Versioned content shapes & session accounting.** The per-kind `content` JSON shapes stored in the mirror are versioned (`CONTENT_SHAPE_VERSION`; the v1 shape table lives in `docs/components/02-session-store.md`): any shape change bumps the constant and ships a forward-only shape migration in the same commit, and a store recorded at a NEWER shape version than the library's is refused loudly rather than risk corruption. Companion accounting: `SessionStorePort.turn_usages()` reads per-turn usage/cost rows, and `Tradewind.usage(session_id)` rolls up a session — turn token/cost totals and summarizer spend (from compaction records) reported separately, summing to the session total with no double counting; a cost total is `None` only when every contributing cost is unknown.

### FR-6 Resume, stop, lifecycle
- **FR-6.1** Resume policy is explicit and three-valued: **NATIVE** (same backend, native id valid → resume by id with re-passed config), **REPLAY** (cross-backend or native store lost → rebuild context from the mirror; lossless message reconstruction on `langchain`, summarized prompt injection into SDK backends), **FRESH**. NATIVE is attempted first; failure degrades to REPLAY, never to an error that loses the conversation.
- **FR-6.2** A running turn MUST be cancellable (Claude `interrupt()`, Codex `TurnHandle.interrupt()`, Cursor `run.cancel()`, langchain stream close). Turn status is recorded as `completed | interrupted | cancelled | failed | in_progress`.
- **FR-6.3** Native-store durability hazards are mitigated without breaking FR-6.4: the mirror is the durability floor (when a persistent store is configured — FR-5.7); `cleanupPeriodDays` is raised on managed hosts; store relocation (Codex `CODEX_HOME`, Cursor `LocalAgentStore`) is an opt-in isolation mode, off by default.
- **FR-6.4 CLI interop.** SDK backends record to their standard native locations so the vendor CLIs can resume the same conversation directly (`claude --resume`, `codex resume`; Cursor pending verification). Turns added out-of-band (from a CLI) MUST be reconciled into the mirror on Tradewind's next contact with the session, by diffing the native transcript against the mirror's last known native id. Not applicable to `langchain` (no native store, no CLI).
- **FR-6.5 Honest turn end.** `TurnResult` carries a machine-readable `end_reason` — `end_turn` (model finished cleanly) | `max_tokens` (provider truncated the output) | `max_tool_rounds` (the caller's cap stopped the tool loop) | `interrupted` — so a consumer can distinguish a clean finish from truncation from a cap without parsing text; truncated output MUST NOT be reported as a clean finish. A per-call `max_tool_rounds` option (non-negative int; 0 = one model response, no tool execution) caps tool-execution rounds; hitting the cap completes the turn as an honest partial (`status="completed"`, `end_reason="max_tool_rounds"`), never as a failure. Adapters that cannot enforce the cap honestly (`supports_tool_round_cap = False`) raise `Unsupported` when it is set rather than approximating one with timers.

### FR-7 Streaming events
- **FR-7.1** All backends emit through one normalized event model (text delta, thinking, tool call, tool result, permission request, turn complete, error), with the verbatim native event carried alongside for consumers that need it.
- **FR-7.2** Deltas are for live consumers only; the mirror stores completed items, never deltas.

### FR-8 Capability flags
- **FR-8.1** Each backend declares machine-readable capabilities, at minimum: `supports_system_prompt`, `supports_structured_output`, `supports_interactive_permissions`, `supports_in_process_tools`, `supports_native_resume`, `supports_fork`, `supports_transcript_read`, `supports_tool_round_cap`.
- **FR-8.2** Known deficits (Cursor: no system prompt, no structured output, no permission callback, no tool-round cap; Codex: no in-process tools, no tool-round cap) are encoded here, not worked around silently.

### FR-9 Subagents
- **FR-9.1** Caller-orchestrated subagents are the portable core: spawn a child session (own prompt set, clean context, optional model override), run, return the result to the caller. This works identically on all four backends.
- **FR-9.2** Model-initiated delegation is an optional layer: a `spawn_agent` tool Tradewind exposes to the model, with Tradewind owning scheduling, concurrency limits, and result reintegration. Native subagent features (Claude `agents`, Cursor nesting) MAY be used where present but are not the abstraction.
- **FR-9.3 Context scope.** What stored context is fed to a turn is a per-call choice, `history_scope: none | flat | tree`, loaded lazily (the store is not read until a backend consumes it — native-resume backends never do, so their turns cost no history query). `flat` replays the session's own history; `tree` additionally folds each descendant session's transcript into one wrapped plain-text block positioned after the parent turn that spawned it (text verbatim, tool activity as one-liners, thinking dropped) — wrapping, not raw interleave, so the parent's role alternation and tool pairing stay valid; `none` feeds nothing. The DEFAULT is per-backend and truthful: `flat` where the backend rebuilds its request from the mirror (langchain), `none` where the backend resumes natively (its engine replays its own history). An explicit `flat`/`tree` on a native-resume backend raises `Unsupported` per FR-1.2; an explicit `none` is accepted anywhere.

### FR-10 Profiles
- **FR-10.1** Deployment profiles select backend and auth mode without caller code changes: `local` (subscription-authenticated SDKs on the owner's machines) and `cloud` (API-key auth; `langchain`, or Claude Agent SDK in API-key mode).
- **FR-10.2** A session records the profile/backend it ran under; continuing a session under a different profile follows FR-6.1 (REPLAY).
- **FR-10.3 Symbolic model tiers.** Callers never name concrete models at call sites: each profile maps a caller-defined set of symbolic tier names (e.g. `ultra`, `strong`, `standard`, `light`) to concrete model ids (plus reasoning effort where supported). Sessions and calls reference tiers; the profile resolves them, so switching profile (local↔cloud, backend swap) re-targets every tier in one place. All profiles MUST define the same tier-name set (validated at construction) so a session's recorded tier stays meaningful across profiles; an unknown tier at call time is an error, never a passthrough.
- **FR-10.4 Config-object initialization.** As an embedded library, Tradewind is initialized with a single caller-constructed config object (profiles, store, broker, defaults) and never reads files, environment variables, or global state itself; multiple independently configured instances may coexist in one process. See `docs/components/01-configuration-and-client.md`.
- **FR-10.5 Model metadata.** A tier's `ModelSpec` MAY carry optional `ModelMeta` (`context_window`, `max_tokens`, and a `ModelCost` table in $/Mtok with whole-request pricing tiers). Consumers: `TurnResult.cost_usd` is computed from token usage on backends that report tokens but no dollar figure (`langchain`, `codex`, `cursor`) — on ALL auth modes, a subscription profile's figure being the API-equivalent price of the tokens used, not billed spend; a backend-reported cost (`claude`) is never overwritten by a computed one; and automatic compaction (FR-5.8) reads `context_window`. Absence is honest: no cost table → `cost_usd` stays None; no `context_window` → no auto-compaction. Cost semantics ported from Pi (`docs/research/2026-09-03-pi-implementation-notes.md` §3).

## 4. Non-Functional Requirements

- **NFR-1 Latency.** Flat history retrieval is a single indexed query; tree retrieval is one recursive-CTE round trip. `raw_json` is excluded from default reads. Required indexes: `messages(session_id, seq)`, `sessions(parent_session_id)`, `sessions(backend, native_session_id)`.
- **NFR-2 Durability.** No conversation is ever lost to a backend's storage policy: the mirror plus REPLAY is the floor. Native resume is a fast path, never a dependency.
- **NFR-3 Honesty.** The library never simulates a capability a backend lacks; capability flags are the contract and are covered by tests against real SDKs.
- **NFR-4 Version pinning.** All four provider packages are pre-1.0 or fast-moving; exact pins plus a thin ports layer isolate churn. Only the official packages are used (`claude-agent-sdk`, `openai-codex`, `cursor-sdk`, `langchain-anthropic`) — PyPI lookalikes are explicitly rejected in dependency review.
- **NFR-5 Embeddability.** Tradewind is a library: no daemon, no global state, no config files of its own beyond the profile the host passes in. The meridian service embeds it; scripts can too.
- **NFR-6 House standards.** Layered architecture enforced by import-linter; mypy strict; the spec-as-contract discipline of GUIDELINES.md applies.

## 5. Out of Scope

- Workflow/process control (waypoint's domain), knowledge graphs (portolan), decision records (binnacle).
- LangGraph: no call graph exists; the `langchain` backend is a plain client plus Tradewind's own tool loop.
- Multi-tenant auth, quotas, billing — the embedding service's concern.
- Non-Anthropic API providers on the `langchain` path (possible later behind the same port; not required now).

## 6. Open Questions

- **OQ-1** Splice ordering for tree retrieval (children inline at spawn points) — schema supports it via `spawned_by_message_id`; rendering still deferred until a UI needs it. Status unchanged at phase-1 close-out.
- ~~**OQ-2** Codex approval-event round-trip fidelity (approvals reviewer protocol) — verified as protocol events; the exact broker mapping needs a spike.~~ **RESOLVED (task 13 spike)**: see `docs/research/2026-09-02-codex-approvals-spike.md` and `docs/ARCHITECTURE.md` §7 P-2.
- **OQ-3** Whether the `spawn_agent` tool (FR-9.2) ships in v1 or after the first embedding in meridian — still open; no meridian embedding has happened yet.
