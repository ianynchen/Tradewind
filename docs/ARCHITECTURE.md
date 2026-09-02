# ARCHITECTURE

Architecture for **Tradewind** (see `docs/REQUIREMENTS.md`; research grounding in
`docs/research/2026-09-01-agent-backends.md`). Tradewind is a Python library that
drives four LLM agent backends behind one port interface, owns a durable
backend-neutral session store, and is embedded by the meridian service (and any
script) as its LLM engine.

## 1. Architectural Position

Tradewind sits below the sibling systems and above the provider SDKs:

```
meridian / scripts / waypoint invocations
        │  (Python API: run, resume, stop, history, spawn)
        ▼
   ┌───────────── tradewind ─────────────┐
   │ application: turn runner, sessions, │
   │ permission broker, event normalizer │
   │ ports: Backend, ToolHost, Store     │
   │ adapters: claude │ codex │ cursor │ │
   │           langchain │ sqlite store  │
   └─────────────────────────────────────┘
        │ claude-agent-sdk │ openai-codex │ cursor-sdk │ anthropic API
```

### 1.1 Principles

1. **MCP is the tool contract.** Every caller tool must render as an MCP server,
   because the least capable backend (Codex) accepts nothing else. In-process
   execution is a fast path behind the same abstraction, never the abstraction.
2. **Native stores are a fast path; the mirror is the floor.** Backends keep their
   own session state and resume from it cheaply; Tradewind's database guarantees no
   conversation is ever lost to a vendor's retention policy (NFR-2).
3. **Honest capabilities over false uniformity.** Backends differ (no system prompt
   on Cursor, no permission callback on Cursor, no in-process tools on Codex);
   the port exposes flags instead of shims that pretend (NFR-3).
4. **Resume = id + config.** Engines restore conversation content on resume but
   never configuration; the session row snapshots everything that must be re-passed.
5. **The engine is replaceable; sessions are not.** Adapters are thin and
   disposable; the session schema and its invariants are the long-lived contract.

### 1.2 Vocabulary

Backend, profile, session, turn, native store, mirror, tool, permission broker,
child session — as defined in REQUIREMENTS §2.

## 2. Context (C4 L1)

- **Meridian service** (primary embedder): calls Tradewind for every LLM turn;
  supplies tools (portolan queries, binnacle writes, filesystem within workspace
  roots) and its MCP endpoints; renders session history in its UI from Tradewind's
  store.
- **Provider runtimes**: the four SDKs/APIs, each with its own auth (subscription
  login locally; API keys in cloud) selected by profile.
- **Native session stores** on the host machine (`~/.claude/projects`, `$CODEX_HOME/sessions`,
  Cursor's store) — read/written by the SDKs, corralled by Tradewind configuration.
- **Tradewind database**: SQLite file owned by the embedding service (Postgres via
  the same store port when meridian centralizes storage).

## 3. Components (C4 L2)

| Component | Responsibility |
|---|---|
| **Turn Runner** | The one entry point: resolve profile → backend, assemble native options from the session snapshot + call arguments, execute the turn, pump events. |
| **Backend Port + Adapters** | `Backend` protocol (run, resume probe, interrupt, capabilities); one adapter per provider. Adapters are the only code importing provider SDKs. |
| **Tool Host** | Registry of caller tools; renders them as (a) in-process handlers where supported, (b) a spawned stdio MCP shim that proxies calls back over a unix socket for Codex/Cursor. Tools stay live closures in the host process. |
| **Permission Broker Port** | Caller-supplied policy (`allow / deny / ask`). Wired to Claude `can_use_tool`, the langchain tool loop, Codex approval events; declared unavailable on Cursor. |
| **Session Store** | Owns the schema (§4), the mirror writer (consumes the normalized event stream), retrieval (flat/tree), and the options snapshot. |
| **Resume Planner** | Implements NATIVE → REPLAY → FRESH (FR-6.1): probes native validity, rebuilds context from the mirror when needed. |
| **Event Normalizer** | Maps each backend's stream onto the unified event model; attaches the verbatim native payload; drops deltas before the mirror. |
| **Subagent Runner** | Caller-orchestrated child sessions (FR-9.1); optional `spawn_agent` tool exposing model-initiated delegation with Tradewind-owned scheduling (FR-9.2). |

### 3.1 The Backend port

All four implementations are subclasses of one abstract interface; nothing outside
`adapters/` may reference a concrete backend class. The Turn Runner, Resume
Planner, and Session Store are written against the port alone, so adding or
replacing a backend touches only `adapters/` plus the profile registry.

```python
class Backend(ABC):
    """One adapter per provider. Constructed by the profile registry."""

    name: BackendName                      # 'claude' | 'codex' | 'cursor' | 'langchain'

    @abstractmethod
    def capabilities(self) -> Capabilities: ...
        # supports_system_prompt, supports_structured_output,
        # supports_interactive_permissions, supports_in_process_tools,
        # supports_native_resume, supports_fork, supports_transcript_read (FR-8)

    @abstractmethod
    async def run(
        self, session: SessionHandle, prompt: Prompt, options: TurnOptions,
        tools: ToolHost, broker: PermissionBroker,
    ) -> AsyncIterator[Event]: ...
        # one call = one turn; yields normalized events (FR-7); the engine may
        # make many model requests underneath

    @abstractmethod
    async def probe_native(self, session: SessionHandle) -> bool: ...
        # can this session's native id still be resumed? (Resume Planner, §5.2)

    @abstractmethod
    async def read_native_transcript(
        self, session: SessionHandle, after_native_id: str | None,
    ) -> list[NativeItem]: ...
        # reconciliation/backfill source (FR-6.4); raises Unsupported where
        # capabilities().supports_transcript_read is False (Cursor)

    @abstractmethod
    async def interrupt(self, session: SessionHandle) -> None: ...   # FR-6.2
```

Rules:

- **R-1** Adapters raise `Unsupported` for capabilities their flags deny; they
  never silently emulate. Flags describe *native* capability only. Emulation
  (e.g. Cursor rules-file system prompts, fork-by-copy) is performed *above* the
  port by the Turn Runner / client, driven by the flags, so the policy lives in
  one place. Emulations that touch the caller's workspace are namespaced and
  reversible: the Cursor system-prompt emulation writes only
  `.cursor/rules/tradewind-session.mdc` — never `AGENTS.md` or any existing
  file — and falls back to first-message prompt folding when it cannot write.
- **R-2** The same shape applies to the other two ports: `SessionStorePort`
  (SQLite now, Postgres later — P-4) and `ToolHost` (in-process handler vs.
  stdio MCP shim are two renderings behind one registry). One interface, N
  subclasses, swap by configuration.
- **R-3** Enforced mechanically, not by convention: import-linter forbids
  `application`/`domain` from importing `adapters` or any provider SDK (§6),
  and a conformance test suite runs the identical scenario matrix against every
  adapter, skipping cases its declared capabilities exclude.

### 3.2 Event taxonomy (frozen)

Frozen at task 11 (P-1 resolved, §7): the six members of `domain.events.Event`,
finalized against the two adapters implemented so far (claude, langchain) rather
than on paper. No member is added without a new P-numbered decision reopening
this list.

| Event | Emitted when | claude | langchain |
|---|---|---|---|
| `TurnStarted` | Always, the first event of every turn. | yes | yes |
| `TextDelta` | A streamed text chunk, ahead of the `ItemCompleted` it assembles into. | no (SDK hands messages over whole, not incrementally) | yes |
| `ItemCompleted` | One normalized message (`text`/`thinking`/`tool_use`/`tool_result`) is complete and mirrored. | yes | yes |
| `PermissionRequested` | A tool call's broker verdict is `"deny"` — **deny-only, by design**: an `"allow"` verdict has no side effect distinct from the call itself proceeding, so it is not separately eventized. | yes | yes |
| `TurnCompleted` | A turn finishes cleanly or is interrupted (`result.status` distinguishes the two). | yes | yes |
| `TurnFailed` | A turn ends in an unrecoverable error. | yes | yes |

`ThinkingDelta` (a streamed reasoning chunk) was removed from the draft
taxonomy at task 11: `claude` never streams deltas at all, and `langchain`'s
would-be emission site is unreachable — nothing in Tradewind's current request
construction enables Anthropic extended thinking on that path, so it never sees
a `"reasoning"` streaming chunk to map. Persisted `kind="thinking"`
`ItemCompleted` items (emitted by both adapters) are unaffected — only the
live, in-flight delta signal for that content was removed.

## 4. Domain Model — session schema

Authoritative store schema (SQLite dialect; Postgres-compatible). Full field
mapping per backend: research doc §4.2.

```sql
CREATE TABLE sessions (
  session_id            TEXT PRIMARY KEY,     -- caller-minted UUID (v7 recommended; FR-5.6)
  backend               TEXT NOT NULL,        -- 'claude' | 'codex' | 'cursor' | 'langchain'
  profile               TEXT NOT NULL,
  native_session_id     TEXT,                 -- Claude session_id | Codex thread.id | Cursor agent id | NULL
  parent_session_id     TEXT REFERENCES sessions(session_id),
  spawn_kind            TEXT,                 -- NULL | 'fork' | 'subagent'
  spawned_by_message_id INTEGER,              -- parent message that launched this child (splice anchor)
  title                 TEXT,
  cwd                   TEXT,
  model                 TEXT,
  system_prompt         TEXT,                 -- NULL where unsupported (Cursor)
  options_json          TEXT NOT NULL,        -- everything that must be re-passed on resume
  status                TEXT NOT NULL DEFAULT 'active',
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL,
  native_meta_json      TEXT
);
CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX idx_sessions_native ON sessions(backend, native_session_id);

CREATE TABLE turns (
  turn_id        TEXT PRIMARY KEY,   -- tradewind-minted UUID (native ids are not globally unique)
  session_id     TEXT NOT NULL REFERENCES sessions(session_id),
  native_turn_id TEXT,               -- Codex turn.id | Cursor run id | Claude promptId | NULL
  seq            INTEGER NOT NULL,
  status       TEXT NOT NULL,      -- 'completed'|'interrupted'|'cancelled'|'failed'|'in_progress'
  final_text   TEXT,
  usage_json   TEXT,
  cost_usd     REAL,
  started_at   TEXT, completed_at TEXT,
  error_json   TEXT
);

CREATE TABLE messages (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id       TEXT NOT NULL REFERENCES sessions(session_id),
  turn_id          TEXT REFERENCES turns(turn_id),
  seq              INTEGER NOT NULL,   -- total order within session
  role             TEXT NOT NULL,      -- 'user'|'assistant'|'tool'|'system'
  kind             TEXT NOT NULL,      -- 'text'|'thinking'|'tool_use'|'tool_result'
                                       -- |'command_execution'|'file_change'|'plan'
                                       -- |'web_search'|'compaction'|'event'
  content_json     TEXT NOT NULL,      -- normalized block per kind
  native_id        TEXT,
  parent_native_id TEXT,
  agent_path       TEXT,               -- subagent attribution within a native stream
  model            TEXT,
  created_at       TEXT,
  raw_json         TEXT                -- verbatim native payload; excluded from default reads
);
CREATE INDEX idx_messages_session_seq ON messages(session_id, seq);
CREATE UNIQUE INDEX idx_turns_native ON turns(session_id, native_turn_id);
```

`sessions` additionally carries `native_history_json` (append-only): when a
cross-profile REPLAY re-homes a session onto a new backend (§5.2), the superseded
`(backend, native_session_id)` pair is appended there rather than forgotten — the
old native transcript remains locatable.

### 4.1 Invariants

- **I-1 Child separation.** Child conversations are their own session rows; their
  messages never carry the parent's `session_id`. Claude sidechains are split out
  at ingestion by their markers. This is what makes flat-vs-tree retrieval a pure
  ID-set question.
- **I-1a Caller-minted identity.** `session_id` comes from the caller (FR-5.6);
  Tradewind validates UUID shape at the port and resolves intent internally
  (`create` strict / `resume` strict / `ensure` get-or-create). Child sessions
  spawned *by* Tradewind (subagents) are the exception: Tradewind mints those.
- **I-2 Options snapshot completeness (declarative parts).** `options_json`
  snapshots everything *declarative*: system prompt, model tier, cwd, sandbox,
  MCP definitions (secret-bearing fields redacted to config references, resolved
  at resume — secrets never land in the store), and tool *names + schemas*. Live
  objects — tool callables, the permission broker — cannot be serialized and are
  re-supplied by the embedder at resume; Tradewind validates that re-supplied
  tool names match the snapshot and raises on mismatch rather than silently
  running with different tools.
- **I-5 One in-flight turn per session.** A second `run()`/`stream()` on a
  session with an unfinished turn raises `TurnInProgress`; queuing, if wanted,
  is built above the library. This is what makes `seq` assignment and turn rows
  well-defined.
- **I-3 Completed items only.** The mirror stores no deltas; the normalizer folds
  them before write.
- **I-4 Raw is an escape hatch.** Anything that doesn't fit `kind`/`content_json`
  rides in `raw_json` verbatim rather than being force-normalized.

### 4.2 Retrieval

- Flat: `SELECT … WHERE session_id = ? ORDER BY seq` (one indexed query).
- Tree: recursive CTE over `parent_session_id` joined to `messages`, ordered
  `(session_id, seq)`; splice ordering via `spawned_by_message_id` when a UI wants
  children inline (OQ-1).
- Flags: `include_children`, `include_raw`; cursor pagination by `(session_id, seq)`.

## 5. Key Interactions

### 5.1 A turn with tools on Codex (the least capable backend, hence the shape of the design)

1. Caller: `run(session, prompt, tools=[fn…], mcp=[…])`.
2. Tool Host starts (or reuses) the stdio shim definition: `python -m tradewind.toolproxy`
   with the host's unix-socket address in env; merges caller MCP servers.
3. Adapter renders `config_overrides` (MCP servers incl. the shim), starts/resumes
   the thread, runs the turn with per-turn options (`approval_mode`, `sandbox`,
   `output_schema`).
4. Codex spawns the shim; each `tools/call` is proxied over the socket to the live
   registry; the broker is consulted before dispatch (broker verdicts also answer
   Codex approval events).
5. Normalizer maps thread items to unified events; mirror writer persists completed
   items; turn row finalized with usage/status.

On Claude the same call skips the shim (in-process SDK tools) and wires the broker
to `can_use_tool`; on langchain Tradewind's own loop executes tools directly.

### 5.2 Resume

1. Resume Planner loads the session row. Same backend + native id → probe NATIVE
   (`resume=` / `thread_resume` / `Agent.resume`) re-passing `options_json`.
   Before continuing, **reconcile**: fetch the native transcript tail
   (`get_session_messages()` / `thread_read(include_turns=True)`), diff against
   the mirror's last `native_id`, and backfill turns added out-of-band — e.g. a
   human resumed the same session from the vendor CLI (FR-6.4).
2. Probe failure (reaped file, moved machine, archived thread) → REPLAY: on
   `langchain`, reconstruct the exact messages array (lossless); on SDK backends,
   inject a rendered/summarized transcript into a fresh native session and record
   the new native id. **Not yet wired** (§7 P-7): no caller invokes
   `ResumePlanner.plan()`/`probe_native()` today, so this degrade path is
   unreachable in practice — a reaped native store currently surfaces as
   `TurnFailed` instead.
3. Caller may force `REPLAY` or `FRESH` (e.g. cross-backend continuation under a
   different profile).

### 5.3 Stop

`stop(session)` routes to the adapter's interrupt (Claude `interrupt()`, Codex
`TurnHandle.interrupt()`, Cursor `run.cancel()`, langchain stream close), waits for
the terminal event, records turn status `interrupted`/`cancelled`. Partial output
already mirrored stays.

## 6. Technology

- Python ≥3.13, uv-managed; Typer only if a debug CLI emerges (library first, NFR-5).
- Pinned provider packages: `claude-agent-sdk`, `openai-codex`, `cursor-sdk`,
  `langchain-anthropic` (+ `anthropic`); `mcp` for the tool shim; `sqlite3` stdlib
  behind the store port.
- Layering enforced by import-linter: `adapters → application → domain`; provider
  SDK imports confined to `adapters/`; `domain` imports no SDK and no `sqlite3`.
- mypy strict; pytest with an `integration` marker for tests that exercise real
  SDKs (run locally where subscriptions exist; skipped in CI without credentials).

### 6.1 Decision records

- **DR-1 No LangGraph.** No call graph exists; checkpointing solves durable graph
  execution, not transcripts. The `langchain` backend is `langchain-anthropic` plus
  Tradewind's own tool loop, with the mirror as the message store.
- **DR-2 MCP-first tools** (Principle 1). Consequence: the toolproxy shim (~100
  lines) is core infrastructure, not a workaround.
- **DR-3 CLI-compatible native stores, mirror as the floor.** SDK backends write
  to their standard native locations so vendor CLIs can resume the same session
  (FR-6.4) — sessions are shared property between Tradewind and the human at a
  terminal. Durability comes from the mirror, not from relocating stores; store
  relocation (Codex `CODEX_HOME`, Cursor `LocalAgentStore`) is an opt-in isolation
  mode for hosts where no human runs CLIs (cloud). Claude `cleanupPeriodDays`
  raised on managed hosts. Consequence: the Resume Planner reconciles before
  continuing (§5.2).
- **DR-4 Child sessions as rows** (Invariant I-1), chosen over mirroring Claude's
  interleaved sidechain format: retrieval semantics beat ingestion convenience.
- **DR-5 Subscription/API split is a profile, not code.** Local = SDK backends on
  subscription auth; cloud = API keys (langchain, or Claude SDK in key mode).

## 7. Pending Architectural Decisions

- ~~**P-1** Exact unified event taxonomy (working set: FR-7.1) — finalize against
  the first two adapters implemented, not on paper.~~ **RESOLVED (task 11)**:
  see §3.2 "Event taxonomy (frozen)".
- ~~**P-2** Broker mapping for Codex approval events (OQ-2) — spike before the
  Codex adapter is declared done.~~ **RESOLVED (task 13)**: see
  `docs/research/2026-09-02-codex-approvals-spike.md`.
- **P-3** `spawn_agent` tool scheduling/limits (OQ-3, FR-9.2) — after first
  meridian embedding.
- **P-4** Postgres store adapter timing — when meridian centralizes storage.
- **P-5** Cursor CLI ↔ SDK store sharing: whether `cursor-agent` can resume agents
  created via the SDK's default local store is undocumented — verify by experiment
  before relying on FR-6.4 for Cursor. **BLOCKED (task 15, phase-1 close-out)**: no
  Cursor subscription exists on this machine, so the spike cannot run; the `cursor`
  adapter ships `EXPERIMENTAL` (never live-verified) and stays open until a
  subscription is available.
- **P-6** Codex native item-id scheme mismatch (FR-6.4 follow-up, found task 14):
  live-streamed `item/completed` ids and `thread_read(includeTurns=true)` ids are
  different, unstable schemes for the same logical item, so
  `ResumePlanner.reconcile()`'s cursor (`store.last_native_id`) can never match a
  `thread_read` id past a session's first turn — `CodexBackend.thread_read_items`
  was made to return nothing rather than everything on a cursor miss (favoring
  mirror correctness over completeness), which means Codex's DR-3 backfill of
  out-of-band CLI activity is effectively inert today. `supports_transcript_read`/
  `supports_native_resume` stay `True` (every other consumer of those flags works
  correctly) — this is a data-completeness gap in one reconcile path, not a broken
  capability. Needs a follow-up spike on stable cross-representation item ids (or an
  architecture change to how `import_native_items`/`reconcile` dedupe) before Codex's
  FR-6.4 backfill can be relied on. See `docs/RUNBOOK.md` for the confirmed evidence.
- **P-7** NATIVE→REPLAY degrade unimplemented (§5.2, found in the final review wave):
  `ResumePlanner.plan()`/`probe_native()` are both implemented but have no callers —
  `TurnRunner.execute` never invokes either, and every SDK-backed adapter resumes
  unconditionally by native id whenever `session.native_session_id` is set. A reaped
  native store (deleted Claude session file, expired Codex thread, archived Cursor
  agent, …) therefore surfaces to the caller today as a plain `TurnFailed` from the
  provider's own resume error, instead of degrading to a REPLAY turn (rendering a
  summarized mirror transcript into a fresh native session, per §5.2 point 2). Wiring
  the degrade — calling `probe_native()` ahead of the native-path branch and routing a
  `False` result into the REPLAY injection path — is phase-2 work, not implemented here.

## 8. References

- `docs/research/2026-09-01-agent-backends.md` — verified backend behavior, native
  format observations, schema field mapping.
- Sibling conventions: `../waypoint/docs/ARCHITECTURE.md` (layering, contracts),
  `GUIDELINES.md` (working standards).
