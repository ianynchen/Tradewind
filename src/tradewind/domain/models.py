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
SpawnKind = Literal["fork", "subagent"]


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


class ModelSpec(BaseModel):
    model: str
    effort: EffortLevel | None = None


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


@runtime_checkable
class PermissionBroker(Protocol):
    async def decide(self, tool_name: str, tool_input: dict[str, Any]) -> Verdict:
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
    final_text: str | None
    usage: dict[str, int]
    cost_usd: float | None


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
