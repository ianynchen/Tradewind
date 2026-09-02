"""Claude adapter: the second `Backend` (ARCHITECTURE §3.1), over the
official `claude-agent-sdk` (task-10 brief).

Talks to Claude through the bundled Claude Code CLI subprocess via
`claude_agent_sdk.ClaudeSDKClient` -- chosen over the package's simpler
`query()` function because interrupt (FR-6.2) is a method on the client
(`ClaudeSDKClient.interrupt()`), not a `query()`-level primitive. One
client is created per `run()` call (per turn): `ClaudeAgentOptions` are a
per-call construct in this SDK (there is no persistent "session" object to
mutate across turns), so a fresh client with `resume=<native id>` is how a
later turn continues an earlier one -- the CLI's own on-disk transcript
(`~/.claude/projects/<encoded-cwd>/`) is the durable state here, mirrored
into Tradewind's own store the same way every other backend's is.

Unlike `LangchainBackend`, this adapter does not run its own tool loop: the
Claude Code CLI subprocess owns that (model call, tool dispatch, follow-up
call, ...) internally. Tradewind's own broker and tool set still gate and
serve every call, through two SDK-provided seams:
  - `ctx.tools` (a fully-wired `ToolHost` -- local `Tool`s plus whatever
    `SessionOptions.mcp_servers` the caller declared, already connected by
    `turn_runner`) is exposed to Claude as ONE in-process SDK MCP server
    (`create_sdk_mcp_server`/`@tool`, task-10 brief), built fresh from
    `ToolHost.schemas()` each turn and dispatching every call straight back
    into `ToolHost.call()`. This is read as satisfying the brief's "mcp_servers
    passthrough for caller McpServerDefs" too: `ToolHost` already proxies
    the caller's own `McpServerDef`s (connected as MCP *clients* by
    `turn_runner`, task-7 brief) into its own `schemas()`/`call()`, so
    bridging all of `ctx.tools` in one server carries them through
    transitively. `TurnContext` never exposes the raw `McpServerDef` list to
    a backend, so a second, direct SDK-level passthrough path (the CLI
    subprocess connecting to those servers itself) isn't wired -- flagged
    for controller confirmation in the task-10 report.
  - `can_use_tool` (`ClaudeAgentOptions.can_use_tool`) bridges every tool
    call the CLI is about to make -- built-in or MCP -- to `ctx.broker.
    decide()`, and `PermissionRequested` fires on "deny" exactly like
    `LangchainBackend`'s tool loop does (FR-4.1).
  - `ClaudeAgentOptions.tools=[]` disables the CLI's own built-in dev tools
    (Bash, Read, Edit, ...) entirely: Tradewind's whole premise (module
    docstring, `tradewind/__init__.py`) is a caller-declared, broker-gated
    tool set, and leaving Claude Code's full built-in toolbox reachable by
    default -- auto-allowed for any caller that doesn't wire a broker, per
    the `_AllowAllBroker` default (turn_runner.py) -- would grant host
    filesystem/shell access no `SessionOptions.tools`/`mcp_servers` ever
    asked for. Flagged for controller confirmation alongside the point
    above; nothing in the task-10 brief states this explicitly.

`NormalizedMessage.content` shapes this adapter reads/writes mirror
`LangchainBackend`'s own convention (its module docstring) since both track
Anthropic's native block vocabulary:
    kind="text"/"thinking":  {"text": str}
    kind="tool_use":         {"id": str, "name": str, "input": dict}
    kind="tool_result":      {"tool_use_id": str, "content": str, "is_error": bool}
`kind="thinking"` additionally carries the block's `signature` in `raw`
(`{"signature": str}`) -- unlike the langchain adapter, which never sees a
signature at all, Claude's own thinking blocks carry one straight from the
API, so there's no `_rebuild_messages`-time "always drop" story here; that
decision (how the mirror rebuild replays it, if at all) is unmade and out
of scope for this adapter (it only ever reads `ctx.load_history()` through
`resume=<native id>`'s own transcript, never manually reconstructing
messages from `StoredMessage`s the way `LangchainBackend` does).

Deferred, matching `LangchainBackend`'s own precedent: `ctx.output_schema`
raises `Unsupported` (`capabilities().supports_structured_output` is
False -- the SDK has no `output_schema`-shaped request parameter to wire it
to). `ModelSpec.effort` is also not wired to `ClaudeAgentOptions.effort`:
the task-10 brief's options list does not name it, and GUIDELINES §1.1
treats an unlisted option as out of scope rather than a silent addition;
flagged in the task-10 report for controller follow-up alongside the two
points above.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any, ClassVar, cast

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    CanUseTool,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    McpSdkServerConfig,
    PermissionResult,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SdkMcpTool,
    SessionMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    get_session_messages,
)

from tradewind.application.config import NativeStoreConfig
from tradewind.application.ports import Backend, TurnContext
from tradewind.application.tool_host import ToolHost
from tradewind.domain.errors import Unsupported
from tradewind.domain.events import (
    Event,
    ItemCompleted,
    PermissionRequested,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from tradewind.domain.models import (
    BackendName,
    Capabilities,
    NormalizedMessage,
    PermissionBroker,
    Profile,
    SessionRow,
    TurnResult,
)

_logger = logging.getLogger(__name__)

# `ResultMessage.terminal_reason` values that mean "this turn was ended by
# `ClaudeSDKClient.interrupt()`, not a genuine completion or failure"
# (`ResultMessage.terminal_reason`'s own docstring; confirmed empirically
# this session -- see task-10 report). `Backend.run`'s contract (ports.py)
# requires an interrupted turn end with neither `TurnCompleted` nor
# `TurnFailed`, so `_drive_client` swallows a `ResultMessage` carrying one of
# these instead of mapping it.
_ABORTED_TERMINAL_REASONS = frozenset({"aborted_streaming", "aborted_tools"})

# The name this adapter registers its `ToolHost` bridge under
# (`create_sdk_mcp_server(name=...)`); Claude's own wire naming for a tool
# from it is `mcp__<_MCP_SERVER_NAME>__<tool name>` (confirmed empirically
# this session), which `_strip_server_prefix` undoes before consulting
# `ctx.broker`/`ToolHost.call()` so both see the same bare tool names the
# caller declared.
_MCP_SERVER_NAME = "tradewind"


def _strip_server_prefix(tool_name: str) -> str:
    return tool_name.removeprefix(f"mcp__{_MCP_SERVER_NAME}__")


def assistant_message_items(message: AssistantMessage) -> list[NormalizedMessage]:
    """Normalize one `AssistantMessage`'s content blocks into the
    `ItemCompleted` items a turn persists (task-10 brief: TextBlock->text,
    ThinkingBlock->thinking with `signature` into `raw`, ToolUseBlock->
    tool_use). `ServerToolUseBlock`/`ServerToolResultBlock` (server-executed
    tools such as web_search) are out of the brief's mapping scope and are
    skipped rather than guessed at."""
    items: list[NormalizedMessage] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            if block.text:
                items.append(
                    NormalizedMessage(role="assistant", kind="text", content={"text": block.text})
                )
        elif isinstance(block, ThinkingBlock):
            items.append(
                NormalizedMessage(
                    role="assistant",
                    kind="thinking",
                    content={"text": block.thinking},
                    raw={"signature": block.signature},
                )
            )
        elif isinstance(block, ToolUseBlock):
            items.append(
                NormalizedMessage(
                    role="assistant",
                    kind="tool_use",
                    content={"id": block.id, "name": block.name, "input": dict(block.input)},
                )
            )
    return items


def _tool_result_text(content: str | list[dict[str, Any]] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(
        cast(str, part.get("text", "")) for part in content if part.get("type") == "text"
    )


def user_message_items(message: UserMessage) -> list[NormalizedMessage]:
    """Normalize one `UserMessage`'s `ToolResultBlock`s into `ItemCompleted`
    items (task-10 brief). A plain-text `UserMessage` (this adapter's own
    submitted prompt echoed back, or CLI-synthesized text such as
    "[Request interrupted by user]") carries nothing this adapter mirrors
    itself -- the prompt is the turn runner's job (`TurnContext.prompt`),
    not an event this backend emits -- so only `ToolResultBlock`s produce
    an item here."""
    if isinstance(message.content, str):
        return []
    items: list[NormalizedMessage] = []
    for block in message.content:
        if isinstance(block, ToolResultBlock):
            items.append(
                NormalizedMessage(
                    role="tool",
                    kind="tool_result",
                    content={
                        "tool_use_id": block.tool_use_id,
                        "content": _tool_result_text(block.content),
                        "is_error": bool(block.is_error),
                    },
                )
            )
    return items


def is_aborted_result(result: ResultMessage) -> bool:
    """Whether `result` ends a turn `ClaudeSDKClient.interrupt()` cut short
    (see `_ABORTED_TERMINAL_REASONS`)."""
    return result.terminal_reason in _ABORTED_TERMINAL_REASONS


def _result_error(result: ResultMessage) -> str:
    if result.errors:
        return "; ".join(result.errors)
    if result.result:
        return result.result
    return f"claude turn failed: subtype={result.subtype!r}"


def _usage_ints(usage: dict[str, Any] | None) -> dict[str, int]:
    if usage is None:
        return {}
    return {key: value for key, value in usage.items() if isinstance(value, int)}


def turn_result_from_result_message(result: ResultMessage, turn_id: str) -> TurnResult:
    """Build the `TurnCompleted.result` a clean-finish `ResultMessage` maps
    to (task-10 brief: usage from `.usage`, cost_usd from
    `.total_cost_usd`, final_text from `.result`). Not called for an
    aborted result (`is_aborted_result`) -- those end the turn with no
    `TurnCompleted` at all, per `Backend.run`'s contract."""
    return TurnResult(
        turn_id=turn_id,
        status="completed",
        final_text=result.result,
        usage=_usage_ints(result.usage),
        cost_usd=result.total_cost_usd,
    )


def _wire_block_items(entry_role: str, block: dict[str, Any]) -> list[NormalizedMessage]:
    """One raw Anthropic-wire content block (`SessionMessage.message["content"]`
    entries -- a plain dict per that field's own `Any` typing, not an SDK
    dataclass) normalized the same way `assistant_message_items`/
    `user_message_items` do for the live stream."""
    block_type = block.get("type")
    if block_type == "text":
        text = cast(str, block.get("text", ""))
        if not text:
            return []
        role = "assistant" if entry_role == "assistant" else "user"
        return [NormalizedMessage(role=cast(Any, role), kind="text", content={"text": text})]
    if block_type == "thinking":
        return [
            NormalizedMessage(
                role="assistant",
                kind="thinking",
                content={"text": cast(str, block.get("thinking", ""))},
                raw={"signature": block.get("signature")},
            )
        ]
    if block_type == "tool_use":
        return [
            NormalizedMessage(
                role="assistant",
                kind="tool_use",
                content={
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input": block.get("input", {}),
                },
            )
        ]
    if block_type == "tool_result":
        return [
            NormalizedMessage(
                role="tool",
                kind="tool_result",
                content={
                    "tool_use_id": block.get("tool_use_id"),
                    "content": _tool_result_text(cast(Any, block.get("content"))),
                    "is_error": bool(block.get("is_error", False)),
                },
            )
        ]
    return []


def native_transcript_items(
    entries: Sequence[SessionMessage], after_native_id: str | None
) -> list[NormalizedMessage]:
    """Map `get_session_messages()`'s raw transcript entries into
    `NormalizedMessage`s (task-10 brief), each carrying `entry.uuid` as its
    `native_id` (task-9's `import_native_items` dedupes on it).

    `entry.message` is typed `Any` by the SDK itself (`SessionMessage`'s own
    docstring: "Raw Anthropic API message dict") -- it is Anthropic's wire
    format (`{"role": ..., "content": str | [block, ...]}`), not one of the
    SDK's own `ContentBlock` dataclasses, so this is a second, independent
    mapping from `assistant_message_items`/`user_message_items` rather than
    a thin wrapper over them.

    `after_native_id`, when given, drops every entry up to and including
    the one whose `uuid` matches it (None reads from the start, matching
    `Backend.read_native_transcript`'s contract); an `after_native_id` not
    found among `entries` is treated as "not present yet" and nothing is
    dropped.
    """
    if after_native_id is not None:
        index = next((i for i, e in enumerate(entries) if e.uuid == after_native_id), None)
        if index is not None:
            entries = entries[index + 1 :]
    items: list[NormalizedMessage] = []
    for entry in entries:
        message = entry.message if isinstance(entry.message, dict) else {}
        content = message.get("content")
        entry_items: list[NormalizedMessage] = []
        if isinstance(content, str):
            if content:
                role = "assistant" if entry.type == "assistant" else "user"
                entry_items.append(
                    NormalizedMessage(role=cast(Any, role), kind="text", content={"text": content})
                )
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    entry_items.extend(_wire_block_items(entry.type, block))
        # `native_id` is the same for every item one transcript line
        # produced -- stamped here rather than threaded through
        # `_wire_block_items` so that helper stays entry-agnostic and
        # independently testable.
        for item in entry_items:
            item.native_id = entry.uuid
        items.extend(entry_items)
    return items


def _sdk_tool_from_schema(host: ToolHost, schema: dict[str, object]) -> SdkMcpTool[Any]:
    name = cast(str, schema["name"])
    description = cast(str, schema.get("description", ""))
    input_schema = cast("dict[str, Any]", schema.get("input_schema", {}))

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        outcome = await host.call(name, cast("dict[str, object]", args))
        return {
            "content": [{"type": "text", "text": outcome.content}],
            "is_error": outcome.is_error,
        }

    return SdkMcpTool(
        name=name, description=description, input_schema=input_schema, handler=handler
    )


def _build_mcp_servers(host: ToolHost) -> dict[str, McpSdkServerConfig]:
    schemas = host.schemas()
    if not schemas:
        return {}
    tools = [_sdk_tool_from_schema(host, schema) for schema in schemas]
    return {_MCP_SERVER_NAME: create_sdk_mcp_server(name=_MCP_SERVER_NAME, tools=tools)}


def _make_can_use_tool(broker: PermissionBroker, pending_events: list[Event]) -> CanUseTool:
    async def can_use_tool(
        tool_name: str, tool_input: dict[str, Any], _context: ToolPermissionContext
    ) -> PermissionResult:
        bare_name = _strip_server_prefix(tool_name)
        input_dict = cast("dict[str, object]", tool_input)
        verdict = await broker.decide(bare_name, input_dict)
        if verdict == "deny":
            pending_events.append(
                PermissionRequested(tool_name=bare_name, tool_input=input_dict, verdict="deny")
            )
            return PermissionResultDeny(message="permission denied")
        return PermissionResultAllow()

    return can_use_tool


class ClaudeBackend(Backend):
    """`Backend` for Claude via the official `claude-agent-sdk` (see module
    docstring for the tool-bridging/interrupt design)."""

    name: ClassVar[BackendName] = "claude"

    def __init__(self, profile: Profile, native_config: NativeStoreConfig) -> None:
        super().__init__(profile, native_config)
        self._clients: dict[str, ClaudeSDKClient] = {}
        # Set during `run()`'s `ResultMessage` handling (task-10 brief's
        # literal ruling: a single instance attribute, not a per-session
        # map). Concurrent turns on different sessions racing this one
        # attribute -- and wiring it into `SessionStorePort.rehome_native` at
        # all -- is Task 11's reconcile concern, not this adapter's; see the
        # task-10 report.
        self.last_native_session_id: str | None = None

    def capabilities(self) -> Capabilities:
        # Exactly the table in the task-10 brief. `supports_structured_
        # output=False`: the SDK has no `output_schema`-shaped request
        # parameter (mirrors `LangchainBackend`'s own deferral, module
        # docstring).
        return Capabilities(
            supports_system_prompt=True,
            supports_structured_output=False,
            supports_interactive_permissions=True,
            supports_in_process_tools=True,
            supports_native_resume=True,
            supports_fork=True,
            supports_transcript_read=True,
        )

    async def probe_native(self, session: SessionRow) -> bool:
        # Cheap probe (controller ruling, task-10 orchestration): a truthy
        # `native_session_id` only means a resume was *recorded*, not that
        # `~/.claude/projects/.../<id>.jsonl` still resolves (deleted,
        # moved cwd, ...) -- a full existence probe is Task 11's reconcile
        # concern.
        return session.native_session_id is not None

    async def read_native_transcript(
        self, session: SessionRow, after_native_id: str | None
    ) -> list[NormalizedMessage]:
        native_session_id = session.native_session_id
        if native_session_id is None:
            return []
        entries = await anyio.to_thread.run_sync(
            get_session_messages, native_session_id, session.cwd
        )
        return native_transcript_items(entries, after_native_id)

    async def interrupt(self, session_id: str) -> None:
        client = self._clients.get(session_id)
        if client is not None:
            await client.interrupt()

    def _build_options(self, ctx: TurnContext, pending_events: list[Event]) -> ClaudeAgentOptions:
        system_prompt = (
            {"type": "preset", "preset": "claude_code", "append": ctx.system_prompt}
            if ctx.system_prompt is not None
            else None
        )
        return ClaudeAgentOptions(
            tools=[],
            mcp_servers=cast("dict[str, Any]", _build_mcp_servers(ctx.tools)),
            can_use_tool=_make_can_use_tool(ctx.broker, pending_events),
            system_prompt=cast(Any, system_prompt),
            model=ctx.model_spec.model,
            cwd=ctx.session.cwd,
            resume=ctx.session.native_session_id,
        )

    async def run(self, ctx: TurnContext) -> AsyncIterator[Event]:
        if ctx.output_schema is not None:
            # Raised before `TurnStarted`, matching `LangchainBackend.run`'s
            # own capability-mismatch handling (module docstring).
            raise Unsupported("structured output not yet implemented for claude backend")
        async for event in self._run_turn(ctx):
            yield event

    async def _run_turn(self, ctx: TurnContext) -> AsyncIterator[Event]:
        yield TurnStarted(turn_id=ctx.turn_id)
        session_id = ctx.session.session_id
        pending_events: list[Event] = []
        client = ClaudeSDKClient(self._build_options(ctx, pending_events))
        self._clients[session_id] = client
        try:
            async for event in self._drive_client(client, ctx, pending_events):
                yield event
        except Exception as exc:
            yield TurnFailed(turn_id=ctx.turn_id, error=str(exc))
        finally:
            # Identity-checked pop, same reasoning as `LangchainBackend.run`'s
            # `_scopes` cleanup: a second `run()` for this `session_id`
            # (started before this `finally` runs) would already have
            # overwritten `self._clients[session_id]` with its own client,
            # and an unconditional pop here would drop that newer one out of
            # the registry, leaving its own future `interrupt()` a no-op.
            if self._clients.get(session_id) is client:
                self._clients.pop(session_id, None)
            try:
                await client.disconnect()
            except Exception:
                # Cleanup-only failure (subprocess already gone, etc.): must
                # never mask a result already yielded above, so it is
                # logged, not raised (GUIDELINES §9).
                _logger.exception(
                    "ClaudeSDKClient.disconnect() failed", extra={"turn_id": ctx.turn_id}
                )

    async def _drive_client(
        self, client: ClaudeSDKClient, ctx: TurnContext, pending_events: list[Event]
    ) -> AsyncIterator[Event]:
        await client.connect(ctx.prompt)
        async for message in client.receive_response():
            for event in pending_events:
                yield event
            pending_events.clear()
            if isinstance(message, AssistantMessage):
                for item in assistant_message_items(message):
                    yield ItemCompleted(message=item)
            elif isinstance(message, UserMessage):
                for item in user_message_items(message):
                    yield ItemCompleted(message=item)
            elif isinstance(message, ResultMessage):
                self.last_native_session_id = message.session_id
                if is_aborted_result(message):
                    # `Backend.run`'s contract (ports.py): an interrupted
                    # turn ends with neither `TurnCompleted` nor
                    # `TurnFailed` -- the turn runner assigns `interrupted`
                    # itself once this generator ends with no terminal
                    # event.
                    return
                if message.is_error:
                    yield TurnFailed(turn_id=ctx.turn_id, error=_result_error(message))
                else:
                    yield TurnCompleted(
                        result=turn_result_from_result_message(message, ctx.turn_id)
                    )
                return
        for event in pending_events:
            yield event
