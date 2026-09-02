# Tradewind Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Tradewind library: one port interface over four LLM agent backends, a durable session store, config-object initialization — per the committed phase-1 contract.

**Architecture:** Layered library (`domain ← application ← adapters`, import-linter-enforced). Caller-minted session UUIDs; SQLite mirror as durability floor with native stores as fast path; MCP-first tools with in-process fast paths; capability flags over emulation-in-adapters.

**Tech Stack:** Python ≥3.13, uv, hatchling, pydantic v2, anyio, stdlib sqlite3, `langchain-anthropic`, `claude-agent-sdk`, `openai-codex`, `cursor-sdk`, `mcp`; pytest + `integration` marker, mypy strict, ruff, import-linter.

**Spec:** `docs/REQUIREMENTS.md`, `docs/ARCHITECTURE.md`, `docs/components/01-configuration-and-client.md`, `02-session-store.md`, `03-langchain-adapter.md`; research grounding `docs/research/2026-09-01-agent-backends.md`.

## Global Constraints

- Python `>=3.13`; async-first (`anyio`); no sync facade in this plan.
- Exact pins for all four provider packages; only the OFFICIAL packages (`claude-agent-sdk`, `openai-codex`, `cursor-sdk`, `langchain-anthropic`) — reject PyPI lookalikes (NFR-4).
- Library rules (NFR-5/FR-10.4): no file/env/global reads by Tradewind; config object only; multiple instances per process must work.
- Layering (ARCHITECTURE §6): `tradewind.domain` imports no SDK and no sqlite3; provider SDKs imported only under `tradewind.adapters`; enforced by import-linter contracts in pyproject.
- mypy `strict`; ruff clean; every commit passes `scripts/check.sh`.
- Flags describe NATIVE capability only; emulation lives in application layer (§3.1 R-1).
- Secrets never written to the store (I-2): MCP secret fields redacted to config references.
- Tests that need real provider credentials carry `@pytest.mark.integration` and skip cleanly when credentials are absent.
- **Available test credentials (2026-09-02):** Claude Agent SDK (subscription on this machine) and Codex SDK (subscription) — full integration testing. **No Anthropic API key and no Groq key yet** — ALL langchain live/integration tests are DEFERRED: write them skip-by-default (`GROQ_API_KEY` / Anthropic key absent → skip), do not attempt to run them, and rely on the fake-model unit tests for Task 8's gate. The owner will set up a Groq free-tier key later (`console.groq.com`; `langchain-groq` `ChatGroq`, `llama-3.3-70b-versatile`; the TEST reads the key, never the library); Ollama remains the offline alternative. **No Cursor subscription** — Cursor integration and the P-5 spike are BLOCKED; only credential-free unit tests run for Task 15. Task 16's matrix: langchain rows report `DEFERRED: free-tier key pending`.

## File Structure (locked)

```
src/tradewind/
  domain/            models.py  events.py  errors.py
  application/       ports.py  client.py  config.py  turn_runner.py
                     resume.py  tool_host.py  toolproxy_protocol.py
  adapters/          sqlite_store.py  langchain_backend.py  claude_backend.py
                     codex_backend.py  cursor_backend.py
  toolproxy/         __main__.py        # stdio MCP shim (spawned by engines)
tests/               unit/…  conformance/…  integration/…  architecture/test_layering.py
scripts/check.sh
```

---

## Stage 1 — vertical slice (store + config/client + langchain)

### Task 1: Package scaffold and gates

**Files:**
- Create: `pyproject.toml`, `src/tradewind/__init__.py` (+ empty subpackage `__init__.py`s per File Structure), `scripts/check.sh`, `tests/architecture/test_layering.py`, `.python-version`

**Interfaces:**
- Produces: importable `tradewind` package; `scripts/check.sh` running `ruff format --check`, `ruff check`, `mypy src`, `lint-imports`, `pytest`.

- [x] **Step 1: Write pyproject** — hatchling build; deps: `pydantic>=2.9`, `anyio>=4`; dev group: `pytest`, `pytest-asyncio` (or anyio pytest plugin), `mypy`, `ruff`, `import-linter`. Provider SDKs are NOT deps yet (added per-adapter task, pinned then). Import-linter contracts:
```toml
[tool.importlinter]
root_package = "tradewind"
[[tool.importlinter.contracts]]
name = "layers"
type = "layers"
layers = ["tradewind.adapters", "tradewind.application", "tradewind.domain"]
[[tool.importlinter.contracts]]
name = "domain is pure"
type = "forbidden"
source_modules = ["tradewind.domain"]
forbidden_modules = ["sqlite3", "langchain_anthropic", "claude_agent_sdk", "openai_codex", "cursor_sdk"]
```
- [x] **Step 2: Write `tests/architecture/test_layering.py`** — subprocess-runs `lint-imports`, asserts exit 0.
- [x] **Step 3: Write `scripts/check.sh`** (set -euo pipefail; the five gates above) and run it — expect PASS on empty package.
- [x] **Step 4: Commit** `chore: scaffold tradewind package with layering gates`

### Task 2: Domain — models, events, errors

**Files:**
- Create: `src/tradewind/domain/models.py`, `domain/events.py`, `domain/errors.py`
- Test: `tests/unit/test_domain_models.py`

**Interfaces (produced — every later task consumes these EXACT names):**
```python
# errors.py
class TradewindError(Exception): ...
class SessionExists(TradewindError): ...
class SessionNotFound(TradewindError): ...
class TurnInProgress(TradewindError): ...
class Unsupported(TradewindError): ...
class ToolMismatch(TradewindError): ...
class ConfigError(TradewindError): ...

# models.py  (pydantic v2 unless noted)
BackendName = Literal["claude", "codex", "cursor", "langchain"]
TierName = str
EffortLevel = Literal["low", "medium", "high", "xhigh"]
Role = Literal["user", "assistant", "tool", "system"]
Kind = Literal["text", "thinking", "tool_use", "tool_result", "command_execution",
               "file_change", "plan", "web_search", "compaction", "event"]
TurnStatus = Literal["completed", "interrupted", "cancelled", "failed", "in_progress"]
SpawnKind = Literal["fork", "subagent"]

class Capabilities(BaseModel):
    supports_system_prompt: bool; supports_structured_output: bool
    supports_interactive_permissions: bool; supports_in_process_tools: bool
    supports_native_resume: bool; supports_fork: bool; supports_transcript_read: bool

class ModelSpec(BaseModel): model: str; effort: EffortLevel | None = None
class SubscriptionAuth(BaseModel): kind: Literal["subscription"] = "subscription"
class ApiKeyAuth(BaseModel):
    kind: Literal["api_key"] = "api_key"; api_key: SecretStr
class Profile(BaseModel):
    backend: BackendName; auth: SubscriptionAuth | ApiKeyAuth
    models: dict[TierName, ModelSpec]; backend_options: dict[str, Any] = {}

class Tool(BaseModel):                      # live handler; never serialized whole
    model_config = ConfigDict(arbitrary_types_allowed=True)
    name: str; description: str; input_schema: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]

class McpServerDef(BaseModel):
    name: str; transport: Literal["stdio", "http"]
    command: list[str] | None = None; url: str | None = None
    env: dict[str, str] = {}; headers: dict[str, str] = {}   # values may be "ref:<config-key>"

Verdict = Literal["allow", "deny"]
class PermissionBroker(Protocol):
    async def decide(self, tool_name: str, tool_input: dict[str, Any]) -> Verdict: ...
    # "ask" semantics: the broker itself blocks on the human; adapter just awaits.

class SessionOptions(BaseModel):
    profile: str | None = None; system_prompt: str | None = None
    tools: list[Tool] = []; mcp_servers: list[McpServerDef] = []
    tier: TierName | None = None; output_schema: dict[str, Any] | None = None
    permission_broker: PermissionBroker | None = None; cwd: Path | None = None
    def snapshot(self) -> dict: ...   # declarative parts only (I-2): tool names+schemas,
                                      # redacted mcp defs, system_prompt, tier, cwd

@dataclass class NormalizedMessage:
    role: Role; kind: Kind; content: dict[str, Any]
    native_id: str | None = None; parent_native_id: str | None = None
    agent_path: str | None = None; model: str | None = None
    raw: dict[str, Any] | None = None
@dataclass class StoredMessage(NormalizedMessage):
    seq: int = 0; session_id: str = ""; turn_id: str | None = None; created_at: str = ""

@dataclass class TurnResult:
    turn_id: str; status: TurnStatus; final_text: str | None
    usage: dict[str, int]; cost_usd: float | None

@dataclass class SessionRow:
    session_id: str; backend: BackendName; profile: str
    options_snapshot: dict[str, Any]
    native_session_id: str | None = None; parent_session_id: str | None = None
    spawn_kind: SpawnKind | None = None; spawned_by_message_id: int | None = None
    title: str | None = None; cwd: str | None = None; system_prompt: str | None = None
    status: str = "active"; native_meta: dict | None = None
    native_history: list[dict] = field(default_factory=list)

# events.py (frozen dataclasses; taxonomy DRAFT until Task 11 / P-1)
@dataclass class TurnStarted: turn_id: str
@dataclass class TextDelta: text: str
@dataclass class ThinkingDelta: text: str
@dataclass class ItemCompleted: message: NormalizedMessage
@dataclass class PermissionRequested: tool_name: str; tool_input: dict; verdict: Verdict
@dataclass class TurnCompleted: result: TurnResult
@dataclass class TurnFailed: turn_id: str; error: str
Event = TurnStarted | TextDelta | ThinkingDelta | ItemCompleted | PermissionRequested | TurnCompleted | TurnFailed
```

- [x] **Step 1: Write failing tests** — `SessionOptions.snapshot()` excludes handlers/broker, includes tool names+schemas, redacts `McpServerDef.env` values not prefixed `ref:` → replaced with `"ref:!redacted"` is WRONG; correct rule: values are stored verbatim ONLY when already `ref:`-prefixed, else replaced by `"ref:missing"` and snapshot flagged `{"has_unrefed_secrets": true}`; `Tool` accepts async handler; invalid `Capabilities` missing a flag fails.
- [x] **Step 2: Run, verify FAIL** (`pytest tests/unit/test_domain_models.py -v`).
- [x] **Step 3: Implement models exactly as above; run to PASS.**
- [x] **Step 4: `scripts/check.sh`; commit** `feat(domain): models, events, errors`

### Task 3: SQLite store — schema, migrations, intent verbs

**Files:**
- Create: `src/tradewind/application/ports.py` (SessionStorePort portion), `src/tradewind/adapters/sqlite_store.py`
- Test: `tests/unit/test_store_sessions.py`

**Interfaces:**
- Consumes: Task 2 models/errors.
- Produces (`ports.py`; store is SYNC — client wraps with `anyio.to_thread`):
```python
class SessionStorePort(ABC):
    @abstractmethod def migrate(self) -> None: ...
    @abstractmethod def create_session(self, row: SessionRow) -> SessionRow: ...      # SessionExists
    @abstractmethod def get_session(self, session_id: str) -> SessionRow | None: ...
    @abstractmethod def ensure_session(self, row: SessionRow) -> SessionRow: ...
    @abstractmethod def update_options(self, session_id: str, snapshot: dict) -> None: ...
    @abstractmethod def rehome_native(self, session_id: str, backend: BackendName,
                                      native_session_id: str | None) -> None: ...     # appends old pair to native_history
```
- Schema: verbatim from ARCHITECTURE §4 (sessions incl. `native_history_json`; turns with UUID PK + `native_turn_id` + unique `(session_id, native_turn_id)`; messages; three indexes). `PRAGMA user_version=1`, WAL, `foreign_keys=ON`, `busy_timeout=5000`.

- [x] **Step 1: Failing tests** — migrate on fresh file sets user_version 1 and is idempotent; `create_session` then `create_session` same id raises `SessionExists`; `ensure_session` returns existing row unchanged (get-or-create in one transaction); `get_session` unknown → None; `rehome_native` appends `{"backend": old, "native_session_id": old}` to `native_history` and sets new pair.
- [x] **Step 2: Run FAIL. Step 3: Implement (single `sqlite3.Connection`, `check_same_thread=False`, one lock). Step 4: Run PASS. Step 5: check.sh; commit** `feat(store): schema, migrations, session intent verbs`

### Task 4: SQLite store — turns, mirror writer, I-5

**Files:**
- Modify: `application/ports.py`, `adapters/sqlite_store.py`
- Test: `tests/unit/test_store_turns.py`

**Interfaces (added to SessionStorePort):**
```python
@abstractmethod def begin_turn(self, session_id: str, turn_id: str,
                               native_turn_id: str | None) -> None: ...     # TurnInProgress if one open
@abstractmethod def append_message(self, session_id: str, turn_id: str | None,
                                   msg: NormalizedMessage) -> int: ...      # returns assigned seq
@abstractmethod def finalize_turn(self, turn_id: str, *, status: TurnStatus,
    final_text: str | None, usage: dict | None, cost_usd: float | None,
    error: str | None) -> None: ...
@abstractmethod def sweep_stale_turns(self, session_id: str) -> int: ...    # in_progress → failed
```

- [x] **Step 1: Failing tests** — `begin_turn` twice without finalize raises `TurnInProgress` (I-5); `append_message` assigns 1,2,3… per session across turns; message with mismatched session/turn rejected (`ValueError`); crash sim: begin+append, new store object on same file → history readable, `sweep_stale_turns` returns 1 and turn reads `failed`; `finalize_turn` persists status/usage/cost/final_text.
- [x] **Step 2–5: FAIL → implement (seq = `1 + COALESCE(MAX(seq),0)` inside the write transaction) → PASS → check.sh → commit** `feat(store): turns and mirror writer with single-flight invariant`

### Task 5: SQLite store — history, fork copy, native import

**Files:**
- Modify: `application/ports.py`, `adapters/sqlite_store.py`
- Test: `tests/unit/test_store_history.py`

**Interfaces (added):**
```python
@abstractmethod def history(self, session_id: str, *, include_children: bool = False,
    include_raw: bool = False, after_seq: int | None = None,
    limit: int | None = None) -> list[StoredMessage]: ...
@abstractmethod def copy_history(self, src_session_id: str, dst_row: SessionRow,
    up_to_seq: int | None = None) -> SessionRow: ...        # creates dst with spawn_kind='fork'
@abstractmethod def import_native_items(self, session_id: str, turn_id: str | None,
    items: list[NormalizedMessage]) -> int: ...             # dedupe on native_id; returns inserted count
@abstractmethod def last_native_id(self, session_id: str) -> str | None: ...
```

- [x] **Step 1: Failing tests** — build lineage root→A→B (B child of A, `spawned_by_message_id` set) plus sibling C: flat(root) has zero A/B/C messages; tree(root) = recursive-CTE ID set ordered `(session_id, seq)`; `include_raw=False` returns `raw=None` even when stored; pagination `after_seq`/`limit`; `copy_history` creates fork row with lineage and copies ≤ `up_to_seq`; `import_native_items` twice → second returns 0; 10k-message flat read (no raw) < 10 ms (mark `@pytest.mark.perf`, assert generously < 50 ms in CI).
- [x] **Step 2–5: FAIL → implement (tree via `WITH RECURSIVE` exactly as ARCHITECTURE §4.2) → PASS → check.sh → commit** `feat(store): history retrieval, fork copy, native import`

### Task 6: Config validation and client shell

**Files:**
- Create: `src/tradewind/application/config.py`, `src/tradewind/application/client.py`, `src/tradewind/__init__.py` exports
- Test: `tests/unit/test_config.py`, `tests/unit/test_client_sessions.py`

**Interfaces:**
- Consumes: store port (Tasks 3–5), domain (Task 2).
- Produces:
```python
# config.py
class StoreConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    sqlite_path: Path | None = None; store: SessionStorePort | None = None   # exactly one
class NativeStoreConfig(BaseModel):
    isolation_mode: bool = False; codex_home: Path | None = None; cursor_store: Any | None = None
class ToolHostConfig(BaseModel): socket_dir: Path | None = None
class TurnDefaults(BaseModel): tier: TierName = "standard"; request_timeout_s: float = 600.0
EventHook = Callable[[Event], None]
class TradewindConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    profiles: dict[str, Profile]; default_profile: str
    store: StoreConfig; permission_broker: PermissionBroker | None = None
    native_stores: NativeStoreConfig = NativeStoreConfig()
    tool_host: ToolHostConfig = ToolHostConfig()
    defaults: TurnDefaults = TurnDefaults(); on_event: EventHook | None = None
    secret_refs: dict[str, SecretStr] = {}      # resolves "ref:<key>" at use time
    # model_post_init validates: default_profile in profiles; len(profiles)>=1;
    # ALL profiles share one identical tier-name set; every profile's models non-empty;
    # StoreConfig has exactly one of sqlite_path/store. Raises ConfigError.

# client.py
class Session:  # thin handle bound to a Tradewind instance
    id: str
    async def run(self, prompt: str, **overrides) -> TurnResult: ...
    def stream(self, prompt: str, **overrides) -> AsyncIterator[Event]: ...
    async def stop(self) -> None: ...
    async def spawn(self, prompt: str, *, tier: TierName | None = None) -> "Session": ...

class Tradewind:
    def __init__(self, config: TradewindConfig): ...   # opens/migrates store, validates; NO network
    async def aclose(self) -> None: ...
    async def __aenter__/__aexit__: ...
    async def create(self, session_id: str, options: SessionOptions) -> Session: ...
    async def resume(self, session_id: str, options: SessionOptions | None = None) -> Session: ...
    async def ensure(self, session_id: str, options: SessionOptions) -> Session: ...
    async def fork(self, src_session_id: str, dst_session_id: str) -> Session: ...
    async def history(self, session_id: str, *, include_children=False,
                      include_raw=False) -> list[StoredMessage]: ...
```

- [x] **Step 1: Failing tests (config)** — missing default_profile key → `ConfigError`; profiles with differing tier sets → `ConfigError`; both/neither store options → `ConfigError`; two `Tradewind` instances on two sqlite files coexist.
- [x] **Step 2: Failing tests (sessions)** — non-UUID id → `ValueError`; `create` twice → `SessionExists`; `resume` missing → `SessionNotFound`; `ensure` idempotent; resume with options whose tool NAMES mismatch snapshot → `ToolMismatch`; resume with matching names rebinds handlers; unknown tier at call time → `ConfigError`; `fork` uses `copy_history` and returns a session with `spawn_kind="fork"`.
- [x] **Step 3–5: FAIL → implement (store calls via `anyio.to_thread.run_sync`; adapters lazily constructed in a later task — `run/stream` raise `NotImplementedError` for now) → PASS → check.sh → commit** `feat(client): config validation and session lifecycle`

### Task 7: Tool host (in-process + MCP client)

**Files:**
- Create: `src/tradewind/application/tool_host.py`
- Modify: `pyproject.toml` (add pinned `mcp`)
- Test: `tests/unit/test_tool_host.py`

**Interfaces:**
```python
class ToolHost:
    def __init__(self, tools: list[Tool], mcp_servers: list[McpServerDef],
                 resolve_ref: Callable[[str], str]): ...
    def schemas(self) -> list[dict]: ...              # anthropic tool-schema dicts (name/description/input_schema)
    async def call(self, name: str, arguments: dict) -> ToolOutcome: ...
    async def __aenter__/__aexit__: ...               # connects MCP clients (stdio/http)
@dataclass class ToolOutcome: content: str; is_error: bool
```
- Behavior: local tools dispatch to `handler(**arguments)`; MCP-server tools are namespaced `mcp__<server>__<tool>` and proxied through the `mcp` client; `resolve_ref` expands `ref:` values from config at connect time (never persisted); unknown tool → `ToolOutcome(is_error=True)`.

- [x] **Step 1: Failing tests** — local tool round trip; handler exception → `is_error=True` with message, never raises; schemas list includes both local and (faked) MCP tools; `ref:` env resolved via injected resolver (assert the resolver was called, value never appears in `schemas()`).
- [x] **Step 2–5: FAIL → implement → PASS → check.sh → commit** `feat(tools): ToolHost with in-process dispatch and MCP client`

### Task 8: LangChain adapter

**Files:**
- Create: `src/tradewind/application/ports.py` Backend ABC (verbatim ARCHITECTURE §3.1, with `TurnContext`), `src/tradewind/adapters/langchain_backend.py`
- Modify: `pyproject.toml` (pin `langchain-anthropic`, `langchain-core`)
- Test: `tests/unit/test_langchain_adapter.py` (FakeMessagesListChatModel-style stub), `tests/integration/test_langchain_live.py`

**Interfaces:**
```python
@dataclass class TurnContext:
    session: SessionRow; turn_id: str; prompt: str
    model_spec: ModelSpec; system_prompt: str | None
    output_schema: dict | None; tools: ToolHost; broker: PermissionBroker
    load_history: Callable[[], list[StoredMessage]]   # mirror, include_raw=False

class Backend(ABC):
    name: ClassVar[BackendName]
    def __init__(self, profile: Profile, native_config: NativeStoreConfig): ...
    @abstractmethod def capabilities(self) -> Capabilities: ...
    @abstractmethod def run(self, ctx: TurnContext) -> AsyncIterator[Event]: ...
    @abstractmethod async def probe_native(self, session: SessionRow) -> bool: ...
    @abstractmethod async def read_native_transcript(self, session: SessionRow,
        after_native_id: str | None) -> list[NormalizedMessage]: ...
    @abstractmethod async def interrupt(self, session_id: str) -> None: ...
```
- Adapter behavior per `docs/components/03-langchain-adapter.md`: rebuild messages from `load_history()` (thinking kinds OMITTED; `tool_use`/`tool_result` → real content blocks), `system` param each request, loop with broker gate (`deny` → synthesized error tool_result + `PermissionRequested` event), max 25 iterations guard, interrupt via `anyio.CancelScope` registered per session id, capabilities exactly the table in the spec (`supports_fork=False`).

- [x] **Step 1: Failing unit tests with a scripted fake chat model** — (a) two-tool turn: model emits tool_calls A,B; broker allows A denies B; assert A executed, B got error tool_result, events contain `PermissionRequested(verdict="deny")`, final `TurnCompleted`; (b) rebuild: seed mirror with text+thinking+tool pair, assert request messages exclude thinking and include reconstructed tool blocks; (c) interrupt mid-loop → iterator ends, no exception escapes, last event `TurnFailed` NOT emitted (status handled by runner).
- [x] **Step 2–5: FAIL → implement → PASS.** The adapter constructs `ChatAnthropic` by default but accepts any injected `BaseChatModel` (this is also how the fake-model unit tests work). Integration tests, two variants: (a) `test_langchain_live.py` against the real Anthropic API — written now, skipped while no API key exists; (b) `test_langchain_free.py` — inject `ChatGroq(model="llama-3.3-70b-versatile")` (skip when `GROQ_API_KEY` unset) and run one real tool-loop turn free of charge; keep an `ChatOllama` fixture variant as the offline fallback (skip when daemon unreachable). Add dev-only deps `langchain-groq` and `langchain-ollama` (pinned). check.sh; commit `feat(adapter): langchain backend with broker-gated tool loop`

### Task 9: Turn runner + wiring + conformance baseline

**Files:**
- Create: `src/tradewind/application/turn_runner.py`, `src/tradewind/application/resume.py`, `tests/conformance/matrix.py`, `tests/conformance/test_langchain.py`
- Modify: `application/client.py` (real `run/stream/stop/spawn`)

**Interfaces:**
- Produces: `TurnRunner.execute(session, prompt, overrides) -> AsyncIterator[Event]` which: merges option layers (defaults < snapshot < overrides), resolves tier→`ModelSpec` via profile, `begin_turn` (UUID turn id), streams adapter events, mirrors every `ItemCompleted` via `append_message`, calls `on_event` tap, finalizes turn (status from termination cause), sweeps stale turns on session open. `ResumePlanner.plan(session) -> Literal["native","replay","fresh"]` — for langchain always `"replay"` (trivially: rebuild happens per-request anyway).
- Conformance `matrix.py`: named scenario functions parameterized by backend fixture, each guarded by the capability flag it needs (`pytest.skip` when flag False): `single_turn_text`, `tool_allow_deny`, `interrupt_midturn`, `resume_continues_context`, `history_flat_and_tree`, `structured_output`, `system_prompt_respected`.

- [x] **Step 1: Failing conformance run for langchain (fake model fixture)** — all seven scenarios red.
- [x] **Step 2: Implement runner/resume/client wiring; scenarios green.**
- [x] **Step 3: End-to-end embedding test** (spec 01 acceptance): build `TradewindConfig` from a plain dict, `ensure` → `run` → `history`, assert no env/file access (monkeypatch `os.environ` access counter is overkill — assert no config file exists and store path was the only path touched).
- [x] **Step 4: check.sh; commit** `feat(runner): turn execution, mirroring, conformance baseline`

**Stage-1 exit:** meridian could embed tradewind on the `langchain` profile today.

---

## Stage 2 — Claude adapter, event taxonomy freeze

### Task 10: Claude adapter

**Files:**
- Create: `src/tradewind/adapters/claude_backend.py`
- Modify: `pyproject.toml` (pin `claude-agent-sdk`)
- Test: `tests/unit/test_claude_mapping.py` (pure mapping fns), `tests/conformance/test_claude.py` + `tests/integration/test_claude_live.py` (subscription; `@integration`)

**Interfaces:**
- Consumes: Backend ABC, TurnContext.
- Produces: `ClaudeBackend(Backend)` — `run()` uses `claude_agent_sdk.query()` with `ClaudeAgentOptions(resume=native_id or None, system_prompt={"type":"preset","preset":"claude_code","append": ctx.system_prompt} if ctx.system_prompt else None, can_use_tool=<broker bridge>, mcp_servers=<ToolHost as in-process SDK MCP server + caller defs>, model=ctx.model_spec.model, cwd=...)`; captures `session_id` from init/`ResultMessage` → `rehome_native` on first turn; maps SDK message stream → events (AssistantMessage blocks → `ItemCompleted` per block; ResultMessage → usage/cost/final_text). `interrupt()` via held `ClaudeSDKClient.interrupt()`. `probe_native` = attempt `get_session_info`; `read_native_transcript` via `get_session_messages()` mapped to `NormalizedMessage` (sidechain records split out per I-1 — child splitting itself deferred to `spawn()` implementation note; sidechain rows tagged via `agent_path`).
- Capabilities: all True except none — exactly: system_prompt T, structured_output F (no output_schema param; runner emulates via instruction only when asked → actually declare F and let conformance skip), interactive_permissions T, in_process_tools T, native_resume T, fork T, transcript_read T.

- [x] **Step 1: Failing mapping unit tests** — fixture SDK-message objects → expected `NormalizedMessage` kinds (text/thinking/tool_use/tool_result), usage extraction from ResultMessage.
- [x] **Step 2: Implement mapping + adapter; unit PASS.**
- [x] **Step 3: Conformance vs live SDK (`@integration`, subscription on the mini):** scenarios green or capability-skipped; verify `claude --resume <native id>` manually once and record in RUNBOOK (FR-6.4 evidence).
- [x] **Step 4: check.sh; commit** `feat(adapter): claude backend over claude-agent-sdk`

### Task 11: Reconciliation + freeze the event taxonomy (P-1)

**Files:**
- Modify: `application/resume.py` (reconcile step), `domain/events.py` (freeze), `docs/ARCHITECTURE.md` (P-1 → resolved; record final taxonomy)
- Test: `tests/unit/test_reconcile.py`

**Interfaces:**
- Produces: `ResumePlanner.reconcile(session) -> int` — when `capabilities().supports_transcript_read`: `read_native_transcript(after=store.last_native_id(sid))` → `import_native_items`; called before every native resume (§5.2).

- [x] **Step 1: Failing test** — mirror has items ≤ native_id N; fake backend returns items N+1..N+3; reconcile imports 3; second call imports 0.
- [x] **Step 2: Implement; PASS.**
- [x] **Step 3: Review both adapters' event usage; remove/adjust any draft event nobody emits; update ARCHITECTURE (P-1 resolved, taxonomy listed) — docs commit in same change.**
- [x] **Step 4: check.sh; commit** `feat(resume): native reconciliation; freeze event taxonomy`

---

## Stage 3 — toolproxy shim, Codex, Cursor

### Task 12: Toolproxy stdio shim

**Files:**
- Create: `src/tradewind/toolproxy/__main__.py`, `src/tradewind/application/toolproxy_protocol.py`, socket server in `application/tool_host.py`
- Test: `tests/unit/test_toolproxy.py`

**Interfaces:**
- Produces: `ToolHost.serve_socket() -> Path` (unix socket; newline-delimited JSON: `{"op":"list"} → {"tools":[schemas]}`, `{"op":"call","name":…,"arguments":…} → {"content":…,"is_error":…}`); `python -m tradewind.toolproxy` = stdio MCP server (via `mcp` package) reading `TRADEWIND_TOOL_SOCKET` env, forwarding `tools/list`/`tools/call` to the socket; `ToolHost.shim_server_def() -> McpServerDef` (stdio, command `[sys.executable, "-m", "tradewind.toolproxy"]`, env carrying the socket path).

- [x] **Step 1: Failing test** — start `serve_socket`; spawn the shim as a real subprocess speaking MCP over stdio via the `mcp` client; `tools/list` shows registered tool; `tools/call` executes the live closure in THIS process (assert on a mutated local variable — proves the proxy pattern).
- [x] **Step 2–4: FAIL → implement → PASS → check.sh; commit** `feat(toolproxy): stdio MCP shim proxying to live tool registry`

### Task 13: Spike — Codex approval events (P-2, timeboxed 0.5 day)

**Files:**
- Create: `docs/research/2026-09-XX-codex-approvals-spike.md`

- [x] **Step 1:** Against the pinned `openai-codex`, run a real turn with `approval_mode` requiring approval; capture how approval requests surface (server-request routing in the client) and answer them programmatically.
- [x] **Step 2:** Record in the spike doc: exact hook point, request/response shapes, and the chosen broker mapping (`allow`→approve, `deny`→reject). Update ARCHITECTURE P-2 → resolved. Commit `docs: codex approvals spike (P-2)`.

### Task 14: Codex adapter

**Files:**
- Create: `src/tradewind/adapters/codex_backend.py`
- Modify: `pyproject.toml` (pin `openai-codex`)
- Test: `tests/unit/test_codex_mapping.py`, `tests/conformance/test_codex.py` (`@integration`)

**Interfaces:**
- Produces: `CodexBackend(Backend)` — `run()`: `Codex(CodexConfig(config_overrides=<mcp_servers incl. ToolHost.shim_server_def()>))`; `thread_start()`/`thread_resume(native_id)`; capture `thread.id` → `rehome_native`; per-turn `thread.turn(prompt, approval_mode=…, sandbox=…, model=ctx.model_spec.model, effort=ctx.model_spec.effort, output_schema=ctx.output_schema)`; stream via `turn.stream()` mapped to events (thread items → kinds incl. `command_execution`/`file_change` native fits); broker wired per Task 13 findings; `interrupt()` via held `TurnHandle.interrupt()`; `read_native_transcript` via `thread_read(include_turns=True)`; `probe_native` via `thread_read` success. Capabilities: system_prompt T (base_instructions), structured_output T, interactive_permissions T (approval granularity), in_process_tools F, native_resume T, fork T (`thread_fork`), transcript_read T. Isolation mode: set `CODEX_HOME` in the spawned client env ONLY when `native_stores.isolation_mode` (DR-3).
- [x] **Steps: mapping unit tests (fixture ThreadItems → NormalizedMessage) FAIL → implement → PASS; conformance `@integration` green/skipped; manual `codex resume <thread-id>` evidence in RUNBOOK; check.sh; commit** `feat(adapter): codex backend with MCP shim tools`

### Task 15: Spike + Cursor adapter — **PARTIALLY BLOCKED (no Cursor subscription)**

> Only the credential-free portions run now: mapping unit tests and the adapter
> code itself (written against the pinned SDK's types). The P-5 spike (Step 1)
> and the conformance run (Step 4) REQUIRE a Cursor subscription — mark both
> checkboxes `BLOCKED: cursor subscription` and leave ARCHITECTURE P-5 open.
> Do not fake or stub these as passed; the adapter ships `experimental` until
> they run for real.

**Files:**
- Create: `docs/research/2026-09-XX-cursor-cli-resume-spike.md`, `src/tradewind/adapters/cursor_backend.py`, rules-file emulation in `application/turn_runner.py`
- Modify: `pyproject.toml` (pin `cursor-sdk`)
- Test: `tests/unit/test_cursor_mapping.py`, `tests/conformance/test_cursor.py` (`@integration`)

- [ ] **Step 1 (spike, P-5): `BLOCKED: cursor subscription`** — no Cursor account/subscription exists on this machine; `docs/research/2026-09-XX-cursor-cli-resume-spike.md` was not created (fabricating spike findings without running the experiment would be dishonest research). ARCHITECTURE P-5 stays open.
- [x] **Step 2: Failing mapping tests** (SDK message stream → NormalizedMessage; the combined call/result record splits into two rows per research §4.2).
- [x] **Step 3: Implement** — `Agent.create(LocalAgentOptions(custom_tools=<ToolHost handlers>, mcp=<caller defs>))` / `Agent.resume(agent_id)` with tools re-passed (FR-3.2); capture agent id → `rehome_native`; `run.cancel()` for interrupt; capabilities: system_prompt F, structured_output F, interactive_permissions F, in_process_tools T, native_resume T, fork F, transcript_read F. Turn-runner emulation (driven by flags, §3.1 R-1): system prompt → write `.cursor/rules/tradewind-session.mdc` (namespaced; never `AGENTS.md`; fallback = prepend `[Instructions]…` to first prompt), removed on session archive.
- [ ] **Step 4: `BLOCKED: cursor subscription`** — `tests/conformance/test_cursor.py` exists, is fully wired against the real `Tradewind`/`Session`/`TurnRunner` stack, and is collected by pytest, but self-skips unconditionally (no subscription to run it against). `check.sh` green; commit `feat(adapter): cursor backend with rules-file emulation` landed for the credential-free portions (Steps 2–3).

### Task 16: Close-out

**Files:**
- Create: `README.md`, `docs/RUNBOOK.md`
- Modify: `docs/ARCHITECTURE.md` (pending list), `docs/REQUIREMENTS.md` (OQ status)

- [x] **Step 1:** Full `scripts/check.sh` (306 passed, 7 skipped) + live conformance matrix for claude (subscription, 6/7 passed + 1 capability-skip, `interrupt_midturn` flaky across runs, see RUNBOOK) and codex (subscription, 7/7 passed clean) — no `GROQ_API_KEY`/`ANTHROPIC_API_KEY` on this machine, so langchain rows are `DEFERRED: free-tier key pending` (per Global Constraints above) rather than live-run, backed by the fake-model conformance suite that runs in `check.sh`; cursor rows recorded `BLOCKED: no cursor subscription`. Full matrix table in `docs/RUNBOOK.md`.
- [x] **Step 2:** README: install, 20-line embedding example (config → ensure → run → history), capability matrix table.
- [x] **Step 3:** Update pending/OQ statuses; commit `docs: close out phase; conformance evidence`.

## Self-Review (performed)

- **Spec coverage:** FR-1→T9, FR-2→T7/T12, FR-3→T7/T8/T14/T15, FR-4→T8/T13/T14 (+Cursor declared-F), FR-5→T3–T6, FR-6→T9/T11 (+CLI evidence T10/T14), FR-7→T2/T9/T11, FR-8→each adapter task, FR-9.1→T6/T9 (`spawn`), FR-9.2 deferred (OQ-3, per spec), FR-10→T6. NFR-1→T5 perf test; NFR-3→conformance skip-by-flag; NFR-4→pins per adapter task; NFR-6→T1 gates.
- **Placeholder scan:** spike tasks intentionally produce research docs, not code placeholders; no TBDs in code tasks.
- **Type consistency:** all cross-task names pulled from Task 2/Task 3–5/Task 8 interface blocks; `ToolOutcome`, `StoredMessage`, `TurnContext` used consistently.
- **Known deviation to flag at execution:** Claude `structured_output=False` — spec's FR-8 lists the flag but 03-spec only claims True for langchain; conformance skips elsewhere. Matches contract.
