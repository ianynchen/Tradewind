"""Domain models: pydantic v2 value objects plus plain dataclasses.

Domain is pure — no provider SDK imports, no sqlite3 (enforced by
import-linter, see pyproject.toml `[tool.importlinter]`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, SecretStr

BackendName = Literal["claude", "codex", "cursor", "langchain"]
TierName = str
EffortLevel = Literal["low", "medium", "high", "xhigh"]
Role = Literal["user", "assistant", "tool", "system"]
Kind = Literal[
    "text",
    "thinking",
    "tool_use",
    "tool_result",
    "command_execution",
    "file_change",
    "plan",
    "web_search",
    "compaction",
    "event",
]
TurnStatus = Literal["completed", "interrupted", "cancelled", "failed", "in_progress"]
# Why a `TurnResult` ended, machine-readably (FR-6.5): `status` alone cannot
# distinguish "model finished cleanly" from "output truncated at max_tokens"
# from "tradewind stopped the tool loop at the caller's cap". Deliberately
# NOT mirroring `TurnStatus`'s "cancelled": no backend distinguishes a cancel
# from an interrupt (each SDK has exactly one abort primitive, and the turn
# runner normalizes all of them to status "interrupted"), so a "cancelled"
# member would be a value nothing can emit. Grows if that ever changes —
# adding a Literal member later is backward-compatible for consumers
# ("timeout" was added exactly this way: FR-6.6, the enforced
# request_timeout_s deadline).
EndReason = Literal[
    "end_turn", "max_tokens", "max_tool_rounds", "interrupted", "timeout", "broker_terminated"
]
# What stored context tradewind feeds to a turn (FR-9.3): "flat" replays
# this session's own history; "tree" additionally folds each descendant
# session's transcript in as one wrapped block positioned after the parent
# turn that spawned it; "none" feeds nothing. The DEFAULT is per-backend
# and states what actually happens: "flat" on a mirror-rebuilding backend
# (langchain), "none" on a native-resume backend (claude/codex/cursor --
# their engines replay their own native history; tradewind feeds nothing).
# An EXPLICIT "flat"/"tree" on a native-resume backend raises `Unsupported`
# (FR-1.2: no silent dropping); an explicit "none" is accepted anywhere --
# on a native-resume backend it merely states the truth.
HistoryScope = Literal["none", "flat", "tree"]
SpawnKind = Literal["fork", "subagent"]

# Version of the per-kind `content` JSON shapes stored in the mirror
# (FR-5.9; the v1 shape table lives in docs/components/02-session-store.md).
# Any change to those shapes bumps this AND ships a shape migration in the
# same commit; a store recorded at a HIGHER version than this constant is
# refused loudly (a newer tradewind wrote it) rather than risk corruption.
CONTENT_SHAPE_VERSION = 1


class Capabilities(BaseModel):
    """What a backend adapter can and cannot do; adapters raise `Unsupported`
    for capabilities their flags deny (R-1)."""

    supports_system_prompt: bool
    supports_structured_output: bool
    supports_interactive_permissions: bool
    supports_in_process_tools: bool
    supports_native_resume: bool
    supports_fork: bool
    supports_transcript_read: bool
    # Whether the adapter can honestly enforce `max_tool_rounds` (FR-6.5):
    # True only where the tool loop is cappable — an in-process loop
    # (langchain) or a native SDK cap (claude `max_turns`). Codex/Cursor run
    # their loops inside their own engines with no cap surface; they raise
    # `Unsupported` when a cap is requested rather than fake one with timers.
    supports_tool_round_cap: bool
    # Whether MID-TURN model-call retry is honest here (FR-6.6): True only
    # where tradewind owns the turn loop (langchain) so a retry provably
    # re-runs no tool. SDK backends are False — they get pre-turn
    # connect/spawn retry only, and their engines retry API errors
    # internally.
    supports_turn_retry: bool
    # Whether a broker `Denial.reason` REACHES THE MODEL here (FR-4.4):
    # langchain writes it into the synthesized error tool_result; claude
    # passes it natively (`PermissionResultDeny.message`). Codex's approval
    # protocol has no reason channel (verified against the shipped SDK) and
    # cursor has no interception at all — False there; the reason is still
    # recorded in `PermissionRequested.reason` and the mirror everywhere a
    # broker is consulted.
    supports_deny_reason: bool


class ModelCostTier(BaseModel):
    """One request-wide pricing tier (FR-10.5): applies when the request's
    total input-side tokens (input + cache_read + cache_write) exceed
    `input_tokens_above`. The highest matched threshold wins and its rates
    apply to the WHOLE request (no marginal/blended pricing) — ported from
    Pi's `calculateCost` semantics (docs/research/2026-09-03-pi-
    implementation-notes.md §3)."""

    input_tokens_above: int
    input: float
    output: float
    cache_read: float
    cache_write: float


class ModelCost(BaseModel):
    """Per-model price table in $/Mtok (FR-10.5)."""

    input: float
    output: float
    cache_read: float
    cache_write: float
    tiers: list[ModelCostTier] = []


class ModelMeta(BaseModel):
    """Optional per-tier model metadata (FR-10.5). Absent by default —
    absence keeps every consumer honest: no `cost` means computed
    `TurnResult.cost_usd` stays None; no `context_window` means automatic
    compaction (FR-5.8) stays off. When `cost` is present, computed
    cost is the API price of the tokens used regardless of auth mode; on
    a subscription profile that figure is the API-EQUIVALENT price, a
    budgeting aid — not billed spend."""

    context_window: int
    max_tokens: int
    cost: ModelCost | None = None


def calculate_cost(
    cost: ModelCost,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Dollar cost of one request's token usage against `cost` (FR-10.5).

    Tier selection per Pi's semantics: total input-side tokens
    (input + cache_read + cache_write) select the tier with the highest
    `input_tokens_above` they exceed, else base rates; the chosen rates
    apply to the whole request. Each adapter maps its own usage field
    names onto these keyword arguments; absent fields are zero. The
    Anthropic 1h-cache-write 2x rule is deliberately omitted — no adapter
    surfaces the 1h split (documented in the Phase-1 spec).

    Returns dollars (float, >= 0). Never raises on zero usage.
    """
    rates: ModelCostTier | ModelCost = cost
    total_input = input_tokens + cache_read_tokens + cache_write_tokens
    best_threshold = -1
    for tier in cost.tiers:
        if total_input > tier.input_tokens_above and tier.input_tokens_above > best_threshold:
            rates = tier
            best_threshold = tier.input_tokens_above
    return (
        rates.input * input_tokens
        + rates.output * output_tokens
        + rates.cache_read * cache_read_tokens
        + rates.cache_write * cache_write_tokens
    ) / 1e6


class ModelSpec(BaseModel):
    model: str
    effort: EffortLevel | None = None
    meta: ModelMeta | None = None


class SubscriptionAuth(BaseModel):
    kind: Literal["subscription"] = "subscription"


class ApiKeyAuth(BaseModel):
    kind: Literal["api_key"] = "api_key"
    api_key: SecretStr


class Profile(BaseModel):
    backend: BackendName
    auth: SubscriptionAuth | ApiKeyAuth
    models: dict[TierName, ModelSpec]
    backend_options: dict[str, Any] = {}


class Tool(BaseModel):
    """A live, callable tool. Never serialized whole (I-2): `snapshot()`
    emits only name/description/input_schema; the handler is re-supplied by
    the embedder at resume."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]


class McpServerDef(BaseModel):
    name: str
    transport: Literal["stdio", "http"]
    command: list[str] | None = None
    url: str | None = None
    env: dict[str, str] = {}
    headers: dict[str, str] = {}


Verdict = Literal["allow", "deny"]


@dataclass(frozen=True)
class Denial:
    """A rich broker deny (FR-4.4). `reason` is shown to the MODEL where
    the backend has a channel for it (`Capabilities.supports_deny_reason`)
    and always recorded in the `PermissionRequested` event; `terminate=True`
    ends the turn after this denial is delivered
    (`end_reason="broker_terminated"`; approximate on codex — documented).
    Returning the plain `"deny"` string remains exactly equivalent to
    `Denial()` — existing brokers never need to change."""

    reason: str | None = None
    terminate: bool = False


BrokerDecision = Verdict | Denial


def normalize_decision(decision: BrokerDecision) -> tuple[Verdict, str | None, bool]:
    """(verdict, reason, terminate) from either broker return form."""
    if isinstance(decision, Denial):
        return "deny", decision.reason, decision.terminate
    return decision, None, False


@runtime_checkable
class PermissionBroker(Protocol):
    async def decide(self, tool_name: str, tool_input: dict[str, Any]) -> BrokerDecision:
        """ "ask" semantics: the broker itself blocks on the human; the
        adapter just awaits the result."""
        ...


_REDACTED_SECRET = "ref:missing"


def _redact_secret_map(values: dict[str, str]) -> tuple[dict[str, str], bool]:
    """Redact a McpServerDef env/headers map for `SessionOptions.snapshot()`
    (I-2: secrets never land in the store).

    A value already prefixed `ref:` is a config reference, not a live
    secret, and is kept verbatim. Any other value is replaced with the
    sentinel `"ref:missing"`. Returns the redacted map and whether any
    redaction happened.
    """
    redacted: dict[str, str] = {}
    has_unrefed = False
    for key, value in values.items():
        if value.startswith("ref:"):
            redacted[key] = value
        else:
            redacted[key] = _REDACTED_SECRET
            has_unrefed = True
    return redacted, has_unrefed


class SessionOptions(BaseModel):
    """Declarative parts snapshot into `options_json` (I-2); `tools` and
    `permission_broker` carry live objects re-supplied by the embedder at
    resume and are never part of the snapshot."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    profile: str | None = None
    system_prompt: str | None = None
    tools: list[Tool] = []
    mcp_servers: list[McpServerDef] = []
    tier: TierName | None = None
    output_schema: dict[str, Any] | None = None
    permission_broker: PermissionBroker | None = None
    cwd: Path | None = None

    def snapshot(self) -> dict[str, Any]:
        """Declarative parts only (I-2): tool names+schemas, redacted MCP
        defs, system_prompt, tier, cwd. Excludes live objects (tool
        handlers, permission_broker) entirely."""
        has_unrefed_secrets = False
        mcp_servers_snapshot: list[dict[str, Any]] = []
        for server in self.mcp_servers:
            env, env_unrefed = _redact_secret_map(server.env)
            headers, headers_unrefed = _redact_secret_map(server.headers)
            has_unrefed_secrets = has_unrefed_secrets or env_unrefed or headers_unrefed
            mcp_servers_snapshot.append(
                {
                    "name": server.name,
                    "transport": server.transport,
                    "command": server.command,
                    "url": server.url,
                    "env": env,
                    "headers": headers,
                }
            )
        return {
            "profile": self.profile,
            "system_prompt": self.system_prompt,
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in self.tools
            ],
            "mcp_servers": mcp_servers_snapshot,
            "tier": self.tier,
            "output_schema": self.output_schema,
            "cwd": str(self.cwd) if self.cwd is not None else None,
            "has_unrefed_secrets": has_unrefed_secrets,
        }


@dataclass
class NormalizedMessage:
    role: Role
    kind: Kind
    content: dict[str, Any]
    native_id: str | None = None
    parent_native_id: str | None = None
    agent_path: str | None = None
    model: str | None = None
    raw: dict[str, Any] | None = None


@dataclass
class StoredMessage(NormalizedMessage):
    seq: int = 0
    session_id: str = ""
    turn_id: str | None = None
    created_at: str = ""


@dataclass
class TurnResult:
    turn_id: str
    status: TurnStatus
    # Required, no default (FR-6.5): every producer must state why the turn
    # ended — "end_turn" (model finished cleanly), "max_tokens" (provider
    # truncated the output), "max_tool_rounds" (the caller's cap stopped the
    # tool loop; output is an honest partial), "interrupted" (cut off by
    # `stop()`). A default would let truncation silently masquerade as a
    # clean finish, the exact lie this field exists to prevent.
    end_reason: EndReason
    final_text: str | None
    usage: dict[str, int]
    cost_usd: float | None


@dataclass
class TurnUsage:
    """One turn's accounting row (FR-5.9 companion verb): what
    `SessionStorePort.turn_usages()` returns, ordered by turn seq. `usage`
    is the turn's token counts exactly as finalized (int fields only);
    `cost_usd` is the turn's own cost — reported (claude) or computed
    (FR-10.5), None when neither exists."""

    turn_id: str
    status: TurnStatus
    usage: dict[str, int]
    cost_usd: float | None


class SessionUsage(BaseModel):
    """`Tradewind.usage()`'s rollup for one session (flat scope).

    `usage` sums each token field across the session's turns; `cost_usd`
    sums the non-None turn costs and is None only when EVERY turn's cost
    is None — a zero-cost session and an unknown-cost session must not
    look alike. Summarizer spend (compaction, FR-5.8) is reported
    SEPARATELY from `kind="compaction"` records — turns and records never
    overlap by construction, so `cost_usd` + `summarizer_cost_usd` is the
    session total without double counting."""

    turns: int
    usage: dict[str, int]
    cost_usd: float | None
    summarizer_usage: dict[str, int]
    summarizer_cost_usd: float | None


@dataclass
class SessionRow:
    session_id: str
    backend: BackendName
    profile: str
    options_snapshot: dict[str, Any]
    native_session_id: str | None = None
    parent_session_id: str | None = None
    spawn_kind: SpawnKind | None = None
    spawned_by_message_id: int | None = None
    title: str | None = None
    cwd: str | None = None
    system_prompt: str | None = None
    status: str = "active"
    native_meta: dict[str, Any] | None = None
    native_history: list[dict[str, Any]] = field(default_factory=list)


def retry_notice(
    *, phase: str, attempt: int, max_attempts: int, delay_s: float, error: str
) -> NormalizedMessage:
    """The mirrored `kind="event"` visibility item for one scheduled retry
    (FR-6.6): every retry is visible in the event stream and the mirror,
    with zero event-taxonomy growth (the compaction precedent). `phase` is
    "model_call" (supports_turn_retry backends), "connect" (SDK pre-turn
    transport retry), or "overflow_recovery"."""
    return NormalizedMessage(
        role="assistant",
        kind="event",
        content={
            "type": "retry_scheduled",
            "phase": phase,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "delay_s": delay_s,
            "error": error,
        },
    )


def resume_degraded_notice(*, reason: str) -> NormalizedMessage:
    """The mirrored `kind="event"` visibility item for a NATIVE->REPLAY
    degrade or cross-backend continuation (FR-6.1): the conversation
    survived, and the record says why the native path was abandoned.
    Zero event-taxonomy growth (the compaction/retry precedent)."""
    return NormalizedMessage(
        role="assistant",
        kind="event",
        content={"type": "resume_degraded", "policy": "replay", "reason": reason},
    )


def engine_compaction_record(
    *,
    summary: str | None = None,
    trigger: str | None = None,
    native_id: str | None = None,
) -> NormalizedMessage:
    """The mirrored `kind="compaction"` record for a compaction the BACKEND
    ENGINE performed on its own context (FR-5.8 observability), as opposed
    to one tradewind performed itself on the mirror.

    Carries only what the caller cannot derive: no `source` and no
    `backend` field, because a caller reading `session.stream()` knows the
    session and one reading `tw.history(session_id)` queried by it -- the
    profile, and therefore the backend, follows either way (user decision,
    2026-09-03).

    Fidelity differs by engine and is documented rather than flattened:
    `cursor` supplies the real summary text; `claude`'s `PreCompact` hook
    fires beforehand and supplies only its trigger; `codex` encrypts its
    summary and supplies only a native turn id. Success/failure is never
    recorded -- no engine exposes it through its typed API.
    """
    content: dict[str, Any] = {}
    if summary is not None:
        content["summary"] = summary
    if trigger is not None:
        content["trigger"] = trigger
    return NormalizedMessage(
        role="assistant", kind="compaction", content=content, native_id=native_id
    )
