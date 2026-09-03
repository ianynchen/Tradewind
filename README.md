# Tradewind

A Python library that drives four LLM agent backends — the Anthropic API (via
`langchain-anthropic`), the Claude Agent SDK, the OpenAI Codex SDK, and the Cursor SDK — behind
one port interface, with a durable, backend-neutral session store and honest, machine-readable
capability reporting. Embedded as a library (no daemon, no config files of its own, no env
reads); the caller supplies a single config object.

- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) — functional and non-functional requirements
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — components, session schema, key interactions
- [docs/RUNBOOK.md](docs/RUNBOOK.md) — conformance evidence, live flakiness, operational notes
- [GUIDELINES.md](GUIDELINES.md) — working and delivery standards

## Install

```bash
uv add "tradewind @ git+https://github.com/ianynchen/Tradewind.git"
# or
pip install "tradewind @ git+https://github.com/ianynchen/Tradewind.git"
```

Requires Python ≥3.12. No PyPI release yet.

## Quick start

```python
import uuid
from pathlib import Path

from tradewind import Tradewind, TradewindConfig
from tradewind.domain.models import SessionOptions

config = TradewindConfig.model_validate(
    {
        "profiles": {
            "default": {
                "backend": "langchain",
                "auth": {"kind": "api_key", "api_key": "sk-..."},
                "models": {"standard": {"model": "claude-sonnet-4-5"}},
            }
        },
        "default_profile": "default",
        "store": "./sessions.db",  # omit entirely for an ephemeral in-memory store (see Configuration)
    }
)

async def main() -> None:
    tw = Tradewind(config)          # opens/migrates the store; no network
    session_id = str(uuid.uuid4())  # the CALLER mints session ids (UUIDv7 recommended)
    session = await tw.ensure(session_id, SessionOptions(system_prompt="Be concise."))
    result = await session.run("What's 2+2?")
    print(result.status, result.final_text)

    for msg in await tw.history(session_id):
        print(msg.role, msg.kind, msg.content)

    await tw.aclose()
```

## Core concepts

- **Backend** — one adapter per provider: `langchain`, `claude`, `codex`, `cursor`. Selected by
  profile, never by call-site code.
- **Profile** — a named deployment configuration: backend + auth mode + model-tier mapping.
  Typical setup: a `local` profile using subscription-authenticated SDKs and a `cloud` profile
  using API keys. Same calling code either way.
- **Tier** — a symbolic model name (`"ultra"`, `"strong"`, `"standard"`, `"light"` — the names
  are yours). Call sites say `tier="standard"`; each profile maps every tier to a concrete
  `ModelSpec(model=..., effort=...)`. All profiles must define the identical tier set
  (validated at construction) so a session's recorded tier stays meaningful across profiles.
- **Session** — one conversation, identified by a caller-minted UUID, holding many **turns**
  (one prompt → final-response cycle each, tool calls included).
- **Mirror** — Tradewind's own transcript copy in its SQLite store. For `langchain` it is the
  system of record; for the SDK backends it is a durability floor beside their native stores.
- **Capabilities** — every backend declares what it natively supports; unsupported requests
  raise `Unsupported` loudly rather than degrading silently.

## Configuration

`TradewindConfig` is the single entry point. Tradewind never reads files, environment
variables, or global state — the host owns configuration acquisition. Construction fails
loudly on invalid config, never at first use. Multiple independently configured `Tradewind`
instances may coexist in one process.

```python
from tradewind import Tradewind, TradewindConfig, NativeStoreConfig, TurnDefaults
from tradewind.domain.models import (
    ApiKeyAuth, ModelSpec, Profile, SubscriptionAuth,
)

config = TradewindConfig(
    profiles={
        "local": Profile(
            backend="claude",
            auth=SubscriptionAuth(),                # uses the machine's logged-in Claude Code
            models={
                "strong":   ModelSpec(model="claude-opus-5", effort="high"),
                "standard": ModelSpec(model="claude-sonnet-5"),
            },
        ),
        "cloud": Profile(
            backend="langchain",
            auth=ApiKeyAuth(api_key="sk-..."),      # SecretStr; never repr'd or stored
            models={
                "strong":   ModelSpec(model="claude-opus-5"),
                "standard": ModelSpec(model="claude-sonnet-5"),
            },
        ),
    },
    default_profile="local",
    store=Path("./tradewind.db"),                   # a path, a SessionStorePort impl, or omitted
    permission_broker=my_broker,                    # optional default broker (see Tools)
    native_stores=NativeStoreConfig(),              # isolation_mode=True to relocate native stores
    defaults=TurnDefaults(tier="standard"),
    on_event=lambda e: log.debug("event %r", e),    # observability tap; exceptions are swallowed
    secret_refs={"github_token": "ghp_..."},        # resolves "ref:github_token" in MCP defs
)
```

Key fields:

| Field | Meaning |
|---|---|
| `profiles` / `default_profile` | Backend + auth + tier→model mapping; per-session override via `SessionOptions.profile`. |
| `store` | One union field: a `Path` (built-in sqlite store there), a caller-built `SessionStorePort` (e.g. Postgres later), or **omitted/`None` for an ephemeral in-memory store** — sessions and history work normally for the life of the instance, nothing touches disk, everything is gone at exit. SDK backends still persist natively either way; a `langchain` session's context then lives only as long as the instance. |
| `permission_broker` | Default broker consulted before tool execution. **Absent broker = all caller-registered tools allowed.** |
| `native_stores` | `isolation_mode=True` relocates Codex/Cursor native stores (cloud hosts); default off preserves vendor-CLI interop. |
| `defaults` | `TurnDefaults`: default tier, the ENFORCED `request_timeout_s` (600s), `CompactionSettings` (auto/reserve/keep_recent), and `RetrySettings` (attempts/backoff). Option layering: defaults < session options < per-call overrides. |
| `on_event` | Fire-and-forget tap on the normalized event stream for logging/metrics. Cannot alter control flow. |
| `secret_refs` | Values substituted for `"ref:<key>"` placeholders in MCP server definitions at connect time — secrets never enter the store. |

## Sessions

The caller mints session ids and states its intent explicitly:

```python
s = await tw.create(sid, options)        # strict: raises SessionExists if it exists
s = await tw.resume(sid, options=None)   # strict: raises SessionNotFound if missing
s = await tw.ensure(sid, options)        # get-or-create (idempotent) — the common case
dst = await tw.fork(src_sid, dst_sid)    # branch: copies history into a new session with lineage
child = await s.spawn("summarize the findings", tier="light")   # subagent: fresh child session
```

- Declarative options (system prompt, tier, tool names/schemas, MCP definitions, cwd) are
  snapshotted into the store; **live objects (tool handlers, the broker) cannot be serialized**
  and must be re-supplied on `resume` — Tradewind validates re-supplied tool names against the
  snapshot and raises `ToolMismatch` rather than silently running with different tools.
- Child sessions (forks, subagents) are their own rows with `parent_session_id` lineage;
  retrieve a whole tree with `tw.history(sid, include_children=True)`.
- One in-flight turn per session: a concurrent `run()`/`stream()` raises `TurnInProgress`.

## Running turns

```python
# Collected:
result = await session.run("Refactor the parser", tier="strong")
# result: TurnResult(turn_id, status, end_reason, final_text, usage, cost_usd)

# Cap the number of tool-execution rounds for one call:
result = await session.run("Investigate, max two probes", max_tool_rounds=2)
# hitting the cap is an HONEST partial: status="completed", end_reason="max_tool_rounds"

# Streamed:
async for event in session.stream("Refactor the parser"):
    match event:
        case TextDelta(text=t): print(t, end="")
        case ItemCompleted(message=m): ...        # the item also lands in the mirror
        case PermissionRequested(): ...           # a broker verdict happened
        case TurnCompleted(result=r): ...
        case TurnFailed(error=e): ...

# Interrupt from another task:
await session.stop()                              # turn finalizes with status "interrupted"
```

The frozen event taxonomy (`tradewind.domain.events`): `TurnStarted`, `TextDelta`,
`ItemCompleted` (carries a `NormalizedMessage` — the unit the mirror stores), `PermissionRequested`
(emitted on deny), `TurnCompleted`, `TurnFailed`. Per-call overrides accepted by
`run`/`stream`: `tier`, `system_prompt`, `output_schema`, `max_tool_rounds`,
`history_scope`, `request_timeout_s`.

### What context the turn sees — `history_scope`

```python
result = await session.run("wrap up", history_scope="tree")   # include subagent transcripts
```

| `history_scope` | Meaning |
|---|---|
| `flat` | This session's own stored history is replayed (default on `langchain`, which rebuilds every request from the mirror). |
| `tree` | Additionally, each child session's transcript (subagents/forks) is folded in as one wrapped plain-text block, positioned after the parent turn that spawned it — text verbatim, tool activity as one-liners. |
| `none` | Nothing stored is fed — a stateless one-shot (default on `claude`/`codex`/`cursor`, whose engines replay their own native history; tradewind feeds them nothing either way). |

Defaults are per-backend and truthful, so you only ever set this to *change* something.
Loading is lazy: a backend that never consumes stored context costs zero history queries per
turn. An explicit `flat`/`tree` on a native-resume backend cannot reach the model and raises
`Unsupported` rather than silently doing nothing.

## Long sessions — compaction and model metadata

Give a tier optional metadata and tradewind manages the mirror-fed context budget for you
(design ported from [Pi](https://github.com/earendil-works/pi), MIT — see
`docs/research/` for the source-verified notes):

```python
from tradewind.domain.models import ModelCost, ModelMeta, ModelSpec

ModelSpec(
    model="claude-sonnet-5",
    meta=ModelMeta(
        context_window=200_000,
        max_tokens=64_000,
        cost=ModelCost(input=3.0, output=15.0, cache_read=0.3, cache_write=3.75),  # $/Mtok
    ),
)
```

- **Automatic compaction** (`langchain` only — SDK engines manage their own context): when
  the estimated context approaches `context_window - reserve_tokens`, older transcript is
  summarized into a structured checkpoint, recorded in the mirror as a `kind="compaction"`
  message, and later requests are rebuilt from `[checkpoint + retained tail]`. Cut points
  never split a tool call from its result; a mid-turn cut summarizes the turn prefix
  separately. **The mirror never loses a row** — compaction changes what is *fed*, not what
  is *stored*; `tw.history()` always returns everything.
- **Manual compaction**: `await session.compact("focus on the auth work")` — works without
  metadata and regardless of the `auto` setting; raises `Unsupported` on SDK backends.
- Config: `TradewindConfig(defaults=TurnDefaults(compaction=CompactionSettings(auto=True,
  reserve_tokens=16384, keep_recent_tokens=20000)))`. `auto=False` disables only the
  automatic trigger.
- A summarizer failure (truncated output) is loud, never a broken checkpoint: manual raises
  `CompactionFailed`; automatic emits an event item and proceeds uncompacted.
- **Session accounting**: `await tw.usage(session_id)` rolls up a session's token totals,
  turn costs, and summarizer spend (reported separately — the two sum to the session total
  exactly once). The mirror's message shapes are versioned (`CONTENT_SHAPE_VERSION`); a store
  written by a newer tradewind is refused loudly instead of corrupted.
- **Computed cost**: with a `cost` table, `TurnResult.cost_usd` is computed from token usage
  on `langchain`/`codex`/`cursor` (claude's own reported cost always wins). On a
  subscription profile the figure is the **API-equivalent price** of the tokens used — a
  budgeting aid, not billed spend. Without a table it stays `None`, honestly.

### Why the turn ended — `end_reason`

`TurnResult.end_reason` states machine-readably *why* the turn ended, so truncated output can
never be mistaken for a clean finish:

| `end_reason` | Meaning |
|---|---|
| `end_turn` | The model finished cleanly. |
| `max_tokens` | The provider truncated the output — an honest partial, not a clean finish. |
| `max_tool_rounds` | Your per-call cap stopped the tool loop — honest partial. |
| `interrupted` | `stop()` cut the turn off. |

`max_tool_rounds` (non-negative int; `0` = one model response, no tool execution) is enforced
only where an adapter can do so honestly — `supports_tool_round_cap` in the capability matrix.
`langchain` caps its own loop exactly; `claude` maps it to the SDK's native `max_turns`.
`codex`/`cursor` run their loops engine-side with no cap surface and raise `Unsupported` when
one is requested — never a timer-faked cap.

## Tools — plug in your own

Applications register tools as **plain Python callables, per session, at runtime** — no
registration files, no subclassing, no packaging step:

```python
from tradewind.domain.models import SessionOptions, Tool

async def lookup_order(order_id: str) -> str:
    return await my_db.fetch_order(order_id)

tools = [
    Tool(
        name="lookup_order",
        description="Fetch an order by id from the application database.",
        input_schema={
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
        handler=lookup_order,          # a live closure — may capture your app's state
    )
]

session = await tw.ensure(sid, SessionOptions(tools=tools))
```

How the same `Tool` reaches each backend (you never care, but it's good to know):

- **langchain / claude / cursor** — executed in-process; the handler runs directly in your
  application's event loop.
- **codex** — Codex has no in-process tool API, so Tradewind spawns a tiny stdio MCP shim
  (`python -m tradewind.toolproxy`) that proxies every call back over a `0o700` unix socket
  into the live registry in your process. Your handler still runs in *your* process with full
  access to your application state; nothing is serialized or imported by the subprocess.

### Permission broker

Gate tool execution by supplying a broker — any object with an async
`decide(tool_name, tool_input) -> "allow" | "deny"`:

```python
class ConfirmingBroker:
    async def decide(self, tool_name: str, tool_input: dict) -> str:
        if tool_name.startswith("read_"):
            return "allow"
        return "allow" if await ask_the_human(tool_name, tool_input) else "deny"
```

Set it per config (`TradewindConfig.permission_broker`) or per session
(`SessionOptions.permission_broker`). "Ask the user" semantics live inside your broker — it may
block as long as it needs; the turn waits. A deny produces a `PermissionRequested` event and an
error tool-result the model sees. **With no broker configured, all caller-registered tools are
allowed** (you registered them, after all) — see Security defaults below for what that means on
each backend.

### External MCP servers

Beyond in-process tools, attach whole MCP servers (stdio or HTTP) per session:

```python
from tradewind.domain.models import McpServerDef

SessionOptions(mcp_servers=[
    McpServerDef(name="github", transport="stdio",
                 command=["uvx", "mcp-server-github"],
                 env={"GITHUB_TOKEN": "ref:github_token"}),   # resolved from config.secret_refs
    McpServerDef(name="meridian", transport="http",
                 url="https://mini.tailnet.ts.net/mcp",
                 headers={"Authorization": "ref:meridian_token"}),
])
```

Their tools appear to the model namespaced `mcp__<server>__<tool>`, pass through the same
broker, and secret-bearing fields are redacted to `ref:` placeholders in the stored snapshot —
resolved only at connect time from `secret_refs`.

## History

```python
msgs = await tw.history(sid)                                   # flat, this conversation only
tree = await tw.history(sid, include_children=True)            # + forks/subagents via lineage
full = await tw.history(sid, include_raw=True)                 # + verbatim native payloads
```

Flat retrieval is one indexed query; `raw_json` (the verbatim native event payloads) is
excluded by default and never fetched unless asked for.

## Backend notes

- **langchain** — the mirror is the system of record; every request rebuilds the messages
  array from the store (thinking blocks omitted by design). Inject any `BaseChatModel` via the
  adapter's `chat_model_factory` seam (how the test suite runs on fakes, and how Groq/Ollama
  slot in without code changes).
- **claude** — runs the real Claude Code engine; your `system_prompt` is *appended* to the
  Claude Code preset persona, not a replacement. Sessions land in `~/.claude/projects/…` and
  can be resumed from the CLI: `claude --resume <native id>` (the native id is on the session
  row). Built-in CLI dev tools are disabled; only tools you register exist. CLI-added turns are
  reconciled back into the mirror on next contact.
- **codex** — threads land in `~/.codex/sessions/…`; `codex resume <thread id>` works.
  Reasoning `effort` from the tier's `ModelSpec` is honored; `output_schema` (structured
  output) is supported. Known limitation (ARCHITECTURE P-6): out-of-band CLI turns are not
  backfilled into the mirror after first contact.
- **cursor** — EXPERIMENTAL, never live-verified (no subscription; P-5). System prompt is
  emulated one layer up via a namespaced `.cursor/rules/tradewind-session.mdc` in the session
  `cwd` (first-turn prompt folding as fallback); your original prompts are always what the
  store records.

## Capability matrix

Each backend declares what it natively supports (`Backend.capabilities()`); Tradewind never
emulates silently — an unsupported call raises `Unsupported`.

| Capability | langchain | claude | codex | cursor (EXPERIMENTAL) |
|---|---|---|---|---|
| `supports_system_prompt` | yes | yes | yes | no (emulated above the port) |
| `supports_structured_output` | no | no | yes | no |
| `supports_interactive_permissions` | yes | yes | yes | no |
| `supports_in_process_tools` | yes | yes | no (MCP shim) | yes |
| `supports_native_resume` | no (mirror rebuild) | yes | yes | yes |
| `supports_fork` | no (`tw.fork` covers it) | yes | yes | no |
| `supports_transcript_read` | no | yes | yes | no |
| `supports_tool_round_cap` | yes (own loop) | yes (native `max_turns`) | no | no |
| `supports_turn_retry` | yes (retry unit = one model call) | no (pre-turn connect retry only) | no (same) | no (same) |

**NATIVE→REPLAY degrade is not implemented** (ARCHITECTURE §7 P-7): a reaped or expired native
session on `claude`/`codex`/`cursor` currently surfaces as `TurnFailed` rather than degrading
to a mirror-replay turn.

## Resilience

- **Retry** (config: `TurnDefaults.retry`, default 3 attempts, 2s doubling backoff): transient
  model-call failures (429/5xx/transport) retry automatically on `langchain` — the retry unit
  is one model call, so completed tool executions are never re-run; a context-overflow error
  compacts once and retries. SDK backends retry only the pre-turn connect/spawn step (their
  engines retry API errors internally; re-running a started turn could duplicate tool side
  effects — `supports_turn_retry` declares this honestly). Every scheduled retry is visible
  as a `retry_scheduled` event item in the stream and the mirror.
- **Turn timeout** (`TurnDefaults.request_timeout_s`, default 600s, per-call override): a turn
  exceeding the deadline is interrupted via the backend's own `interrupt()` and ends
  `status="interrupted"`, `end_reason="timeout"` — on all four backends.

## Testing your integration — no network

Embedders' test suites can script whole turns without any network or credentials, through the
public constructor: `Tradewind(config, backend_factories=...)` takes per-instance factory
overrides, consulted before the built-in registry (names you don't override keep their real
factories; the override never touches other instances or module state).

```python
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from tradewind import Tradewind
from tradewind.adapters.langchain_backend import LangchainBackend


class ScriptedModel(FakeMessagesListChatModel):
    """FakeMessagesListChatModel replays scripted AIMessages verbatim (tool_calls included);
    the base class has no bind_tools, so a no-op override is needed when tools are registered."""

    def bind_tools(self, tools, **kwargs):
        return self


scripted = ScriptedModel(responses=[
    AIMessage(content="", tool_calls=[{"name": "probe", "args": {"q": "one"}, "id": "t1"}]),
    AIMessage(content="done"),
])

tw = Tradewind(config, backend_factories={
    "langchain": lambda profile, native: LangchainBackend(
        profile, native, chat_model_factory=lambda _spec: scripted
    ),
})
# session.run(...) now drives the real tool loop, broker, and mirror against the script.
```

Two fake-model gotchas (both fail loudly if hit): a model without `bind_tools` plus registered
tools is rejected with a message naming the model — hence the two-line subclass above; and
`GenericFakeChatModel` cannot script tool-call turns at all (it streams by splitting message
*content*, so a content-empty tool-call message produces zero chunks) — use
`FakeMessagesListChatModel` as shown.

## Security defaults

What "no configuration" actually means:

- **No broker configured anywhere** means every caller-registered tool call is *allowed*.
  Configure a broker if you want tool calls gated at all.
- **Built-in provider tools**: `claude` disables the CLI's own built-in dev tools entirely.
  `codex` leaves Codex's built-in shell/`apply_patch` enabled but broker-gated — and when no
  broker is configured anywhere, `codex` defaults its sandbox to `read-only` instead of
  `workspace-write`, so an unconfigured caller never gets ungated filesystem writes. An
  explicit `Profile.backend_options["sandbox"]` always wins.
- **The toolproxy unix socket** has no authentication of its own — filesystem permissions
  (`0o700` socket directory) and the same-machine, same-OS-user assumption are the boundary.

See [docs/RUNBOOK.md](docs/RUNBOOK.md) for the full detail behind each of these.

## Integration-test environment variables

None of these are read by the library itself — only by the test suite, and only when opted in:

| Variable | Enables |
|---|---|
| `TRADEWIND_RUN_CLAUDE_INTEGRATION=1` | Live conformance + smoke tests against a real Claude Code subscription. |
| `TRADEWIND_RUN_CODEX_INTEGRATION=1` | Live conformance against a real Codex/ChatGPT subscription. |
| `ANTHROPIC_API_KEY` | Live langchain tests against the real Anthropic API. |
| `GROQ_API_KEY` | Live langchain tests against the free-tier Groq API (`ChatGroq`). |
| `ANTHROPIC_API_KEY` (same var) | Also gates the Phase-2c live compaction validation (`tests/integration/test_compaction_live.py`): a long real session that must compact twice and recall a summarized-away fact. |

Cursor's live conformance suite self-skips unconditionally until a Cursor subscription exists.

## Development

```bash
uv sync
bash scripts/check.sh   # ruff format, ruff lint, mypy strict, import-linter, pytest
```

`scripts/check.sh` is hermetic — no network calls, no credentials required. See
[docs/RUNBOOK.md](docs/RUNBOOK.md) for the full conformance matrix (all four backends × seven
scenarios) and known live-run flakiness.
