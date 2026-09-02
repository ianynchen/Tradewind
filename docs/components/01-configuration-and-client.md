# Component: Configuration and Client

## Purpose

The public face of the library (REQUIREMENTS FR-1, FR-5.6, FR-10; NFR-5). Defines
the config object an embedder constructs to initialize Tradewind, and the client
API every caller programs against. Everything else in the package is reachable
only through this surface.

## Owns

- `TradewindConfig` and its sub-models (the only way to initialize the library).
- The `Tradewind` client class and `Session` handle.
- The profile registry (name → backend + auth + defaults).
- The option-layering rule (config < session < call).

## Depends on

`application` (turn runner, resume planner, store port, tool host, broker port).
Imports no provider SDK and no adapter class (ARCHITECTURE §3.1 R-3).

## Configuration object

Tradewind is a library embedded in other packages (meridian first). It therefore
**never reads files, environment variables, or global state**. The host owns
configuration acquisition (its own config system, env, vault) and hands Tradewind
one validated object. Pydantic v2 models; construction fails loudly on invalid
config, never at first use.

```python
class TradewindConfig(BaseModel):
    profiles: dict[str, Profile]          # at least one
    default_profile: str                  # key into profiles
    store: StoreConfig                    # storage backend selection
    permission_broker: PermissionBroker | None = None   # default broker; per-call override allowed
    native_stores: NativeStoreConfig = NativeStoreConfig()
    tool_host: ToolHostConfig = ToolHostConfig()
    defaults: TurnDefaults = TurnDefaults()             # default tier, timeouts
    on_event: EventHook | None = None     # observability tap (logging/metrics), never control flow

class Profile(BaseModel):
    backend: BackendName                  # 'claude' | 'codex' | 'cursor' | 'langchain'
    auth: SubscriptionAuth | ApiKeyAuth   # discriminated union (DR-5)
    models: dict[TierName, ModelSpec]     # symbolic tier → concrete model (FR-10.3)
    backend_options: dict[str, Any] = {}  # adapter-specific passthrough, validated by the adapter

class ModelSpec(BaseModel):
    model: str                            # provider model id, verbatim for this profile's backend
    effort: EffortLevel | None = None     # for backends with reasoning effort (claude, codex)

class StoreConfig(BaseModel):             # one of:
    sqlite_path: Path | None = None       #   default engine (WAL, user_version migrations)
    store: SessionStorePort | None = None #   or a caller-built store (Postgres later, P-4)

class NativeStoreConfig(BaseModel):
    isolation_mode: bool = False          # DR-3: relocate native stores (cloud); default keeps CLI interop
    codex_home: Path | None = None        # only honored when isolation_mode
    cursor_store: Any | None = None       # LocalAgentStore instance; only when isolation_mode

class ApiKeyAuth(BaseModel):
    api_key: SecretStr                    # or api_key_provider: Callable[[], str] for rotation
class SubscriptionAuth(BaseModel):
    pass                                  # SDK uses the machine's logged-in credentials
```

Decisions:

| Decision | Choice | Why |
|---|---|---|
| Config transport | one object, constructor-injected | Library rule (NFR-5): host owns acquisition; Tradewind owns validation. No `tradewind.toml`, no env reads, no singletons. |
| Multiple instances | supported | Two `Tradewind(config)` instances with different stores/profiles may coexist in one process (tests, migrations). No module-level state permits otherwise. |
| Secrets | `SecretStr` / provider callable | Keys never repr into logs; callable supports rotation without reconstructing the client. |
| Backend-specific knobs | `Profile.backend_options` passthrough | Keeps `TradewindConfig` backend-agnostic; the adapter validates its own dict at startup, not at first turn. |
| Observability | `on_event` tap | Host logging/metrics without subclassing; the tap observes the normalized stream and cannot alter it. |

## Client API

Async-first (`anyio`-compatible); a thin sync facade may wrap it later, not in
phase 1.

```python
tw = Tradewind(config)                    # opens store, validates profiles; no network
await tw.aclose()                         # or: async with Tradewind(config) as tw

# Session acquisition — caller mints the UUID (FR-5.6); three intents:
s = await tw.create(session_id, options=SessionOptions(...))   # error if exists
s = await tw.resume(session_id, options=None)                  # error if missing; options carries
                                                               #   the LIVE objects (tools, broker) the
                                                               #   snapshot cannot store (I-2) — names
                                                               #   validated against the snapshot
s = await tw.ensure(session_id, options=...)                   # get-or-create
dst = await tw.fork(src_session_id, dst_session_id)            # client-level fork-by-copy for
                                                               #   store-of-record sessions; native
                                                               #   fork used where flags allow

class SessionOptions(BaseModel):          # declarative parts snapshot into options_json (I-2)
    profile: str | None = None            # defaults to config.default_profile
    system_prompt: str | None = None      # semantics per capability flags (FR-8)
    tools: list[Tool] = []                # live callables; names+schemas snapshotted, impls re-supplied
    mcp_servers: list[McpServerDef] = []  # secret fields redacted to config references in the snapshot
    tier: str | None = None               # symbolic model tier (FR-10.3); resolved by the profile
    output_schema: dict | None = None
    permission_broker: PermissionBroker | None = None   # live; never snapshotted
    cwd: Path | None = None

# Running a turn — one call, streamed or collected:
async for event in s.stream(prompt): ...            # normalized events (FR-7)
result = await s.run(prompt)                        # TurnResult: text, usage, cost, status
await s.stop()                                      # interrupt the in-flight turn (FR-6.2)

# History (FR-5.5):
msgs = await tw.history(session_id, include_children=False, include_raw=False)
tree = await tw.history(session_id, include_children=True)

# Subagents (FR-9.1):
child = await s.spawn(prompt_set, model=None)       # tradewind-minted child id, lineage recorded
```

Option layering: `TurnDefaults` (config) < `SessionOptions` (session) < per-call
arguments. Each layer overrides only keys it sets; the merged result is what the
adapter receives and what `options_json` snapshots.

## Contract points

- `Tradewind(config)` performs store open/migration and profile validation only —
  no SDK construction, no network. Adapters are built lazily per profile on first
  use and cached on the instance.
- `resume()` on a session whose profile/backend differs from its recorded one does
  not error: the Resume Planner routes to REPLAY (FR-10.2).
- Every public method validates the session id is a UUID at the boundary (I-1a).
- One in-flight turn per session (I-5): a concurrent `run()`/`stream()` raises
  `TurnInProgress`.
- All profiles must declare the identical tier-name set; construction fails on a
  mismatch, and an unknown tier at call time is an error (FR-10.3).
- Secrets never enter the store: snapshotting redacts secret-bearing MCP fields
  to config references, resolved at resume from the live config (I-2).
- `stream()` and `run()` are the same turn — `run()` is `stream()` drained with
  the events still mirrored and delivered to `on_event`.

## Acceptance

- Conformance suite constructs the client with: minimal config, multi-profile
  config, caller-built store, invalid configs (missing profile key, both/neither
  store options) — the last group fails at construction with typed errors.
- A meridian-shaped embedding test: host builds config from its own dict, runs a
  turn on `langchain`, reads history — without touching files or env vars.
