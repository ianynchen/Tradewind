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
    call the CLI is about to make -- built-in or MCP -- to `ctx.policy.
    gate()` as a `caller_tool` action (05-turn-governance §2.1), which asks
    the broker with today's name and input when the rules say `ask`;
    `PermissionRequested` fires on "deny" exactly like `LangchainBackend`'s
    tool loop does (FR-4.1).
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

Structured output (FR-1.3): `ctx.output_schema` rides the SDK's native
`ClaudeAgentOptions.output_format` (`{"type": "json_schema", "schema": ...}`,
rendered by the SDK as the CLI's `--json-schema`). The CLI adds its own
`StructuredOutput` tool, validates the model's call against the schema, and
reports the result as `ResultMessage.structured_output`; this adapter ADDS
it as `TurnResult.structured_output` (the field every structured-output
backend sets), while `final_text` stays the model's prose
(`ResultMessage.result`). Additive means the turn's events and mirror are
exactly a plain turn's: the CLI's `StructuredOutput` `tool_use`/`tool_result`
pair is engine plumbing, hidden from the live stream AND from
`native_transcript_items` (so reconcile never backfills it); the tool is
auto-allowed without consulting the broker; and it does not cost the caller
a tool round -- `_build_options` gives the CLI's `max_turns` headroom for it,
and `_CallerRoundCap` keeps `max_tool_rounds` exact for caller tools (on
every capped turn, schema or not). A schema turn that ends without a valid
result -- a clean finish with no `structured_output` (the
CLI silently disables structured output when it rejects a schema at init),
or the CLI's own `error_max_structured_output_retries` -- still COMPLETES,
keeping its text, with `structured_output=None`; an honest partial
(`max_tool_rounds`, `max_tokens`) keeps its `end_reason`.

`ModelSpec.effort` is not wired to `ClaudeAgentOptions.effort`:
the task-10 brief's options list does not name it, and GUIDELINES §1.1
treats an unlisted option as out of scope rather than a silent addition;
flagged in the task-10 report for controller follow-up alongside the two
points above.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any, ClassVar, cast

import anyio
import claude_agent_sdk._cli_version
from claude_agent_sdk import (
    TERMINAL_TASK_STATUSES,
    AssistantMessage,
    CanUseTool,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLIConnectionError,
    HookCallback,
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
    McpSdkServerConfig,
    PermissionResult,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SdkMcpTool,
    SessionMessage,
    StreamEvent,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
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
from tradewind.application.engine_env import engine_env
from tradewind.application.policy_gate import PolicyGate, caller_tool_source
from tradewind.application.ports import Backend, TurnContext
from tradewind.application.supervisor import WorkSupervisor
from tradewind.application.tool_host import ToolHost
from tradewind.application.workspace_scan import CustomizationPattern
from tradewind.domain.compaction import compose_replay_prompt
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
    EndReason,
    NormalizedMessage,
    Profile,
    SessionRow,
    TurnResult,
    engine_compaction_record,
    resume_degraded_notice,
    retry_notice,
)
from tradewind.domain.policy import Action, TurnVars, floor_tradewind

_logger = logging.getLogger(__name__)


def _engine_pid(client: ClaudeSDKClient) -> int | None:
    """The CLI process's PID, for the process report (05 §3.4). The SDK
    keeps it private (`ClaudeSDKClient._transport._process`, pinned by a
    unit test); when it is missing, one WARNING and no report."""
    transport = getattr(client, "_transport", None)
    pid = getattr(getattr(transport, "_process", None), "pid", None)
    if not isinstance(pid, int):
        _logger.warning("process report unavailable: claude SDK has no _transport._process.pid")
        return None
    return pid


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

# The CLI's own structured-output tool (added by `--json-schema`). Built-in
# tools are disabled (`tools=[]`) and every caller tool arrives prefixed
# `mcp__tradewind__`, so this bare name can only ever be that tool.
_STRUCTURED_OUTPUT_TOOL = "StructuredOutput"


def _without_structured_output(
    items: list[NormalizedMessage], hidden_ids: set[str]
) -> list[NormalizedMessage]:
    """Drop the CLI's `StructuredOutput` `tool_use` items (recording their
    ids in `hidden_ids`) and the `tool_result` items answering them: engine
    plumbing for structured output, never a caller-visible tool event
    (FR-1.3, module docstring). `hidden_ids` spans messages -- the result
    arrives in a later message than the call."""
    kept: list[NormalizedMessage] = []
    for item in items:
        if item.kind == "tool_use" and item.content.get("name") == _STRUCTURED_OUTPUT_TOOL:
            hidden_ids.add(cast(str, item.content.get("id")))
            continue
        if item.kind == "tool_result" and item.content.get("tool_use_id") in hidden_ids:
            continue
        kept.append(item)
    return kept


def _strip_server_prefix(tool_name: str) -> str:
    return tool_name.removeprefix(f"mcp__{_MCP_SERVER_NAME}__")


def assistant_message_items(message: AssistantMessage) -> list[NormalizedMessage]:
    """Normalize one `AssistantMessage`'s content blocks into the
    `ItemCompleted` items a turn persists (task-10 brief: TextBlock->text,
    ThinkingBlock->thinking with `signature` into `raw`, ToolUseBlock->
    tool_use). `ServerToolUseBlock`/`ServerToolResultBlock` (server-executed
    tools such as web_search) are out of the brief's mapping scope and are
    skipped rather than guessed at.

    Every produced item carries `message.uuid` as its `native_id` (task-11
    fix: confirmed empirically this task that the live SDK stream, not only
    `get_session_messages()`'s transcript-file read, populates this field --
    it is the same id the corresponding line in the native transcript file
    carries). Without this, `store.last_native_id()` never advances past a
    live-driven turn, and `ResumePlanner.reconcile()`'s next
    `read_native_transcript(after=None)` would re-import that same turn's
    content from the transcript file as brand-new, duplicating it in the
    mirror (task-11 report: caught live against a real session).
    """
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
    for item in items:
        item.native_id = message.uuid
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
    an item here.

    Every produced item carries `message.uuid` as its `native_id` -- see
    `assistant_message_items`'s docstring for why."""
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
    for item in items:
        item.native_id = message.uuid
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


def is_max_turns_result(result: ResultMessage) -> bool:
    """Whether `result` ends a turn the CLI's `max_turns` cap stopped
    (FR-6.5). The CLI reports it as an *error* result (`subtype`
    "error_max_turns", `terminal_reason` "max_turns"), but tradewind only
    ever sets `max_turns` from the caller's own `ctx.max_tool_rounds`
    (`_build_options`), so hitting it is the caller's requested cap doing
    its job -- an honest `max_tool_rounds` completion, not a failure."""
    return result.terminal_reason == "max_turns" or result.subtype == "error_max_turns"


def is_structured_output_exhausted_result(result: ResultMessage) -> bool:
    """Whether `result` is the CLI's own "no attempt produced schema-valid
    structured output" error result. The CLI flags it as an error, but the
    turn simply ended without a valid result -- FR-1.3 completes it with
    `structured_output=None`, keeping its text, rather than failing it."""
    return (
        result.subtype == "error_max_structured_output_retries"
        or result.terminal_reason == "structured_output_retry_exhausted"
    )


def _end_reason(result: ResultMessage) -> EndReason:
    if is_max_turns_result(result):
        return "max_tool_rounds"
    if result.stop_reason == "max_tokens":
        return "max_tokens"
    # Includes `stop_reason=None` (older CLIs report none): a clean-finish
    # `ResultMessage` with no contrary signal ended the turn itself.
    return "end_turn"


def turn_result_from_result_message(result: ResultMessage, turn_id: str) -> TurnResult:
    """Build the `TurnCompleted.result` a clean-finish `ResultMessage` maps
    to (task-10 brief: usage from `.usage`, cost_usd from
    `.total_cost_usd`, final_text from `.result`; FR-6.5: `end_reason`
    from `terminal_reason`/`stop_reason` via `_end_reason`). Not called
    for an aborted result (`is_aborted_result`) -- those end the turn with
    no `TurnCompleted` at all, per `Backend.run`'s contract."""
    return TurnResult(
        turn_id=turn_id,
        status="completed",
        end_reason=_end_reason(result),
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
    # StructuredOutput call ids from EVERY entry, before the cut: the cut can
    # fall right after the call, leaving only its result to drop.
    hidden_ids: set[str] = {
        cast(str, block.get("id"))
        for entry in entries
        if isinstance(entry.message, dict) and isinstance(entry.message.get("content"), list)
        for block in cast("list[Any]", entry.message["content"])
        if isinstance(block, dict)
        and block.get("type") == "tool_use"
        and block.get("name") == _STRUCTURED_OUTPUT_TOOL
    }
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
        # The live stream hides the CLI's StructuredOutput pair, so the
        # transcript read must too -- else reconcile would backfill it.
        items.extend(_without_structured_output(entry_items, hidden_ids))
    return items


def _normalize_input_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Ensure `schema` reaches `create_sdk_mcp_server`'s literal-JSON-Schema
    fast path instead of its `{param_name: python_type}` shorthand path
    (`_build_input_schema`, `claude_agent_sdk/__init__.py` -- read directly
    off the installed 0.2.151 package this session, fix-round-1 report):

        if (
            "type" in tool_def.input_schema
            and "properties" in tool_def.input_schema
            and isinstance(tool_def.input_schema["type"], str)
        ):
            return tool_def.input_schema  # literal pass-through
        properties = {
            param_name: _python_type_to_json_schema(param_type)
            for param_name, param_type in tool_def.input_schema.items()
        }
        ...                                # shorthand: every top-level key,
                                            # "type" included, becomes a
                                            # bogus parameter name

    A `Tool.input_schema` of `{"type": "object"}` (a legitimate, if
    minimal, JSON Schema for "any object") has no `"properties"` key, so
    without this normalization it silently falls into the shorthand branch
    -- its own `"type": "object"` entry gets reinterpreted as a parameter
    literally named `type` (confirmed empirically against the installed SDK
    this session: `_build_input_schema` turned it into `{"properties":
    {"type": {"type": "string"}}, "required": ["type"]}`), and the model
    then correctly, faithfully calls the tool with that bogus shape
    (`{"type": "x"}`) -- the task-10 report's `tool_allow_deny` root cause,
    corrected here rather than in the report's own wording.

    Only the exact gap in the SDK's own guard is patched (`"type"` present
    and a `str`, `"properties"` absent) -- matched intentionally, not just
    "close enough", so this never second-guesses a schema that was already
    going to hit the literal path, or one that was always meant to use the
    shorthand form (no `"type"` key at all).
    """
    if "type" in schema and isinstance(schema["type"], str) and "properties" not in schema:
        return {**schema, "properties": {}}
    return schema


def _sdk_tool_from_schema(host: ToolHost, schema: dict[str, object]) -> SdkMcpTool[Any]:
    name = cast(str, schema["name"])
    description = cast(str, schema.get("description", ""))
    input_schema = _normalize_input_schema(cast("dict[str, Any]", schema.get("input_schema", {})))

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


def _make_precompact_hook(pending_events: list[Event]) -> HookCallback:
    """A `PreCompact` hook that records the engine's own compaction
    (FR-5.8 observability).

    The CLI fires this BEFORE compacting, so the summary does not exist
    yet and the record carries only the trigger (`"auto"`/`"manual"`) --
    honest about what it is: "the engine is about to compact its own
    context". Queued on the same `pending_events` list `_make_can_use_tool`
    uses, drained by `_drive_client` on the next message. Returns an empty
    output: this hook observes, it never steers the CLI.
    """

    async def on_pre_compact(
        hook_input: HookInput, _tool_use_id: str | None, _context: HookContext
    ) -> HookJSONOutput:
        trigger = cast("str | None", cast("dict[str, Any]", hook_input).get("trigger"))
        pending_events.append(ItemCompleted(message=engine_compaction_record(trigger=trigger)))
        return {}

    return on_pre_compact


class _CallerRoundCap:
    """Exact caller-tool-round cap for any capped turn (FR-6.5).

    The CLI's own `max_turns` cannot do this: the bundled CLI runs response
    k's tools BEFORE its `max_turns` check, so `max_turns = N + 1` still
    let `max_tool_rounds=0` run the first response's tools; and a schema
    turn needs extra `max_turns` headroom for the StructuredOutput call
    (FR-1.3), which would admit one more caller round. So the adapter
    enforces the cap itself and `max_turns` is only a backstop
    (`_build_options`). `_drive_client` feeds this every `AssistantMessage`
    (`observe`); a round is one model response (`message_id` -- the CLI
    streams one `AssistantMessage` per content block, all sharing it)
    holding at least one caller tool call; StructuredOutput calls never
    count. The permission bridge asks `is_over_cap` per call and refuses
    the first call past the cap with an interrupt. Per-turn state, touched
    only from this turn's event loop.

    A single response holding BOTH a StructuredOutput call and a caller call
    past the cap ends the turn as `max_tool_rounds` with
    `structured_output=None`: the caller call is refused with an interrupt,
    which aborts the whole response, StructuredOutput included.
    """

    def __init__(self, cap: int) -> None:
        self.cap = cap
        # Set once a call was refused for the cap; `_drive_client` then maps
        # the CLI's aborted result to an honest `max_tool_rounds` completion.
        self.hit = False
        self._round_of_response: dict[str, int] = {}
        self._round_of_call: dict[str, int] = {}
        self._changed = anyio.Event()

    def observe(self, message: AssistantMessage) -> None:
        caller_ids = [
            block.id
            for block in message.content
            if isinstance(block, ToolUseBlock) and block.name != _STRUCTURED_OUTPUT_TOOL
        ]
        if not caller_ids:
            return
        response = message.message_id or message.uuid or caller_ids[0]
        round_number = self._round_of_response.setdefault(
            response, len(self._round_of_response) + 1
        )
        for call_id in caller_ids:
            self._round_of_call[call_id] = round_number
        self._changed.set()
        self._changed = anyio.Event()

    async def is_over_cap(self, tool_use_id: str | None) -> bool:
        """Whether the call `tool_use_id` belongs to a round past the cap.

        Waits for the stream to show the call: the SDK may start this
        permission handler before `_drive_client` has read the message
        holding it (it queues that message first, but a slow
        `session.stream()` consumer paces `_drive_client`). There is
        deliberately NO deadline: one would turn a slow consumer into a
        false `max_tool_rounds` stop. The wait is bounded by the turn
        itself -- the SDK cancels the handler on `close()` or a
        `control_cancel_request`, and the turn's watchdog/disconnect ends
        everything.

        A missing `tool_use_id` (the wire protocol always sends one) cannot
        be placed in a round: it is logged and NOT counted -- the broker
        decides the call, as on an uncapped turn -- rather than treated as
        a cap stop."""
        if tool_use_id is None:
            _logger.warning(
                "permission request without tool_use_id; not counted against max_tool_rounds"
            )
            return False
        while tool_use_id not in self._round_of_call:
            await self._changed.wait()
        return self._round_of_call[tool_use_id] > self.cap


def message_prompt(prompt: str) -> str:
    """05 §5 (owner M11): a prompt is always a message to the model. The
    bundled CLI expands a leading `/` as a slash command, and its only
    switch, `--disable-slash-commands` ("Disable all skills"), still
    intercepts built-in commands (live check, CLI 2.1.258: `-p
    --disable-slash-commands "/cost"` answers "/cost isn't available in this
    environment." with no model call). So a prompt whose first
    non-whitespace character is `/` is sent as `Message: <prompt>`; the
    mirror keeps the caller's original (the runner records `ctx.prompt`'s
    source, never this)."""
    return f"Message: {prompt}" if prompt.lstrip().startswith("/") else prompt


def report_work(work: WorkSupervisor, message: object) -> None:
    """Map one SDK message onto work signals (05 §7, Claude row). Signals
    only -- nothing here becomes an event or a mirror item.

    - `StreamEvent` (`include_partial_messages`) -> `model_active`;
    - tool-use block -> `started(id, "tool")`; tool-result block ->
      `ended(tool_use_id)`;
    - `TaskStartedMessage` -> `started(task_id, "background")`;
      `TaskProgressMessage` -> `active`; `TaskNotificationMessage`, or a
      `TaskUpdatedMessage` whose `patch.status` is terminal -> `ended`;
    - a message with `parent_tool_use_id` (a subagent's traffic) ->
      `active` of that spawning call;
    - `ResultMessage` -> `engine_done`.
    Claude streams no tool output, so work silence on Claude is time since
    the last tool start or end (05 §7)."""
    if isinstance(message, StreamEvent):
        work.model_active()
        return
    if isinstance(message, TaskStartedMessage):
        work.started(message.task_id, "background", message.description)
    elif isinstance(message, TaskProgressMessage):
        work.active(message.task_id)
    elif isinstance(message, TaskNotificationMessage):
        work.ended(message.task_id)
    elif isinstance(message, TaskUpdatedMessage):
        if message.patch.get("status") in TERMINAL_TASK_STATUSES:
            work.ended(message.task_id)
    elif isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, ToolUseBlock):
                work.started(block.id, "tool", block.name)
    elif isinstance(message, UserMessage):
        if not isinstance(message.content, str):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    work.ended(block.tool_use_id)
    elif isinstance(message, ResultMessage):
        work.engine_done()
    if isinstance(message, AssistantMessage | UserMessage) and message.parent_tool_use_id:
        work.active(message.parent_tool_use_id)


def _direct_policy(ctx: TurnContext, pending_events: list[Event]) -> PolicyGate:
    """The gate for a `TurnContext` built without the runner (`ctx.policy`
    unset -- direct construction, as in unit tests): broker mode with no
    caller or default rules, so every caller tool asks `ctx.broker`, as
    before turn governance. The runner always sets `ctx.policy`."""
    return PolicyGate(
        broker=ctx.broker,
        backend="claude",
        floor=floor_tradewind(),
        caller=[],
        defaults=[],
        turn=TurnVars(
            mode="broker",
            engine_mode=None,
            shim_server=None,
            workspace=ctx.session.cwd,
            result_tool=None,
        ),
        has_broker=not getattr(ctx.broker, "is_default_allow_all", False),
        emit=pending_events.append,
    )


def _make_can_use_tool(
    policy: PolicyGate,
    pending_events: list[Event],
    terminate_flag: list[bool],
    stop_reason: list[str | None],
    round_cap: _CallerRoundCap | None = None,
) -> CanUseTool:
    """The permission bridge: every caller tool call goes through
    `policy.gate` (05 §2.1) as a `caller_tool` action; the broker, when the
    rules say `ask`, gets today's bare tool name and input (05 §1.1).
    `stop_reason[0]` is set when the gate stops the turn (05 §2.6)."""

    async def can_use_tool(
        tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResult:
        input_dict = cast("dict[str, object]", tool_input)
        if tool_name == _STRUCTURED_OUTPUT_TOOL:
            # FR-1.3 / 05 §2.3: the CLI's own structured-output tool is the
            # result tool -- a structural pass-through, never asked.
            await policy.gate(
                _caller_tool_action(tool_name, input_dict),
                broker_name=tool_name,
                broker_args=input_dict,
                pass_through=True,
            )
            return PermissionResultAllow()
        if round_cap is not None and await round_cap.is_over_cap(context.tool_use_id):
            # FR-6.5: the caller's cap, enforced here exactly (not a
            # broker verdict, so no PermissionRequested). Interrupting
            # ends the CLI's loop; `_drive_client` reports the honest
            # `max_tool_rounds` completion.
            round_cap.hit = True
            return PermissionResultDeny(
                message=f"max_tool_rounds ({round_cap.cap}) reached", interrupt=True
            )
        bare_name = _strip_server_prefix(tool_name)
        decision = await policy.gate(
            _caller_tool_action(bare_name, input_dict),
            broker_name=bare_name,
            broker_args=input_dict,
        )
        if decision.outcome == "stop":
            # 05 §2.6: cancel and end the turn with `TurnFailed(reason)`;
            # `_drive_client` maps the aborted result.
            reason = decision.reason or f"turn stopped by policy at {bare_name!r}"
            stop_reason[0] = reason
            # FR-4.1: reported as a denial, as Codex's shim path does.
            pending_events.append(
                PermissionRequested(
                    tool_name=bare_name, tool_input=input_dict, verdict="deny", reason=reason
                )
            )
            return PermissionResultDeny(message=reason, interrupt=True)
        if decision.outcome == "deny":
            pending_events.append(
                PermissionRequested(
                    tool_name=bare_name,
                    tool_input=input_dict,
                    verdict="deny",
                    reason=decision.reason,
                )
            )
            if decision.terminate:
                # Native terminate (FR-4.4): `interrupt=True` ends the CLI's
                # query loop after this denial; `_drive_client` reclassifies
                # the resulting aborted ResultMessage as broker_terminated.
                terminate_flag[0] = True
            return PermissionResultDeny(
                message=decision.reason or "permission denied", interrupt=decision.terminate
            )
        return PermissionResultAllow()

    return can_use_tool


def _caller_tool_action(name: str, args: dict[str, object]) -> Action:
    return Action(
        kind="caller_tool",
        fields={"name": name, "args": args, "source": caller_tool_source(name)},
        backend="claude",
        native_name=name,
        native_args=args,
        summary=name,
    )


class ClaudeBackend(Backend):
    """`Backend` for Claude via the official `claude-agent-sdk` (see module
    docstring for the tool-bridging/interrupt design)."""

    name: ClassVar[BackendName] = "claude"

    def __init__(self, profile: Profile, native_config: NativeStoreConfig) -> None:
        super().__init__(profile, native_config)
        self._clients: dict[str, ClaudeSDKClient] = {}
        # Keyed by tradewind session_id, mirroring `_clients` (task-11
        # review, fix round 1): one `ClaudeBackend` instance is cached and
        # reused across every session on its profile
        # (`Tradewind._resolve_backend`), so the earlier single shared
        # attribute let one session's native id rehome ANOTHER session
        # sharing the profile -- e.g. session A completes
        # (`last_native_session_id = "native-A"`), session B's turn fails
        # before its own `ResultMessage` ever arrives, and `TurnRunner`
        # would read the stale "native-A" value and rehome B onto A's
        # transcript. Per-session scoping is deliberate; see
        # `take_native_session_id`'s docstring for why it also pops rather
        # than just reads.
        self._native_ids: dict[str, str] = {}
        # Set by `interrupt()` for the session's turn in flight, so a turn
        # waiting for open work to settle (05 §3.3) ends at a caller stop
        # even when the engine sends nothing more. Keyed like `_clients`.
        self._stop_requests: dict[str, asyncio.Event] = {}

    def take_native_session_id(self, session_id: str) -> str | None:
        """Pop and return the native session id `session_id`'s most recent
        turn recorded (its `ResultMessage.session_id`, set in
        `_drive_client`), or None if no turn has recorded one since the
        last call.

        Popping -- not just reading -- means a value can never be attributed
        to more than one turn: once `TurnRunner` consumes it for the turn
        that produced it, a later turn (on this or, before this fix, even
        another session) that didn't itself get a `ResultMessage` reads
        None rather than that stale value.
        """
        return self._native_ids.pop(session_id, None)

    def customization_patterns(self) -> list[CustomizationPattern]:
        # 06 §1. Skills, agents and commands are not flagged: `tools=[]`
        # reaches the CLI as `--tools ""` (claude_agent_sdk subprocess_cli.py
        # `_build_command`), which turns off every built-in tool.
        return [
            CustomizationPattern(".claude/settings.json", "claude-settings", False),
            CustomizationPattern(".claude/settings.local.json", "claude-settings", False),
            CustomizationPattern(".claude/hooks", "claude-hook", True),
            CustomizationPattern(".mcp.json", "claude-mcp", False),
        ]

    def auth_env_vars(self) -> frozenset[str]:
        # 06 §4 (CLI 2.1.258). Explicit names only: a `CLAUDE_CODE_USE_*`
        # prefix rule would also match feature switches.
        return frozenset(
            {
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_AUTH_TOKEN",
                "CLAUDE_CODE_USE_BEDROCK",
                "CLAUDE_CODE_USE_VERTEX",
                "CLAUDE_CODE_USE_FOUNDRY",
                "CLAUDE_CODE_USE_ANTHROPIC_AWS",
                "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
                "CLAUDE_CODE_USE_MANTLE",
                "CLAUDE_CODE_USE_GATEWAY",
            }
        )

    def engine_version(self) -> str | None:
        # 06 §5: the adapter runs the SDK's bundled CLI. Read per call.
        return claude_agent_sdk._cli_version.__cli_version__

    def capabilities(self) -> Capabilities:
        # Exactly the table in the task-10 brief, plus structured output
        # via the SDK's native `output_format` (module docstring).
        return Capabilities(
            supports_system_prompt=True,
            supports_structured_output=True,
            supports_interactive_permissions=True,
            supports_in_process_tools=True,
            supports_native_resume=True,
            supports_fork=True,
            supports_transcript_read=True,
            # Enforced exactly by `_CallerRoundCap` through the permission
            # bridge (FR-6.5), with `ClaudeAgentOptions.max_turns` as a
            # backstop (`_build_options`); `_drive_client` reports the honest
            # `max_tool_rounds` completion.
            supports_tool_round_cap=True,
            # The engine owns the turn; re-running a started turn could
            # duplicate tool side effects (FR-6.6): pre-turn connect retry
            # only, and the CLI retries API errors internally.
            supports_turn_retry=False,
            # FR-4.4: PermissionResultDeny.message is native.
            supports_deny_reason=True,
            # 05 §3/§7: tool, task and stream signals reach `ctx.work`, so
            # the inactivity limits apply and there is no default hard cap.
            supports_work_supervisor=True,
            # The SDK's auto mode bypasses `can_use_tool` (RUNBOOK, task-8
            # finding), so `run()` refuses engine_auto; caller tools go
            # through `can_use_tool` and the gate.
            supports_engine_auto=False,
            supports_action_policy=True,
        )

    async def probe_native(self, session: SessionRow) -> bool:
        # REAL probe since Phase 4 (FR-6.1): claude's native store is a
        # LOCAL jsonl, so existence is deterministically checkable via the
        # same `get_session_messages` path reconcile reads -- no
        # error-string matching. Empty and missing are indistinguishable
        # here and both answer False: an id whose transcript holds nothing
        # has nothing to natively resume, and the REPLAY it triggers
        # rebuilds the same (empty) context from the mirror harmlessly.
        native_session_id = session.native_session_id
        if native_session_id is None:
            return False

        def _exists() -> bool:
            try:
                return bool(get_session_messages(native_session_id, session.cwd))
            except Exception:
                return False

        return await anyio.to_thread.run_sync(_exists)

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
        stop_request = self._stop_requests.get(session_id)
        if stop_request is not None:
            stop_request.set()
        client = self._clients.get(session_id)
        if client is None:
            return
        try:
            await client.interrupt()
        except CLIConnectionError:
            # `self._clients[session_id]` is set as soon as `_run_turn`
            # constructs the client -- before `_drive_client` awaits
            # `client.connect(...)` -- so a `stop()` landing in that window
            # finds a registered-but-not-yet-connected client.
            # `ClaudeSDKClient.interrupt()` raises `CLIConnectionError` in
            # that state (`client.py`: "Not connected. Call connect()
            # first.") rather than queuing the interrupt. `Backend.interrupt`'s
            # contract (ports.py) is "a no-op when no turn is currently in
            # flight" -- nothing is running yet to cancel either, so this is
            # swallowed rather than propagated (fix round 1: caught here
            # over a connected-flag+retry, which would need to reach back
            # into `_drive_client` to honor a pending interrupt once
            # `connect()` resolves -- undone work for what should be a very
            # short window in practice). A `stop()` racing this tightly
            # against a still-connecting `run()` simply won't interrupt that
            # specific attempt; logged, not silently dropped.
            _logger.info(
                "ClaudeSDKClient.interrupt() called before connect() finished; treated as a no-op",
                extra={"session_id": session_id},
            )

    def _build_options(
        self,
        ctx: TurnContext,
        pending_events: list[Event],
        terminate_flag: list[bool],
        stop_reason: list[str | None],
        round_cap: _CallerRoundCap | None = None,
    ) -> ClaudeAgentOptions:
        system_prompt = (
            {"type": "preset", "preset": "claude_code", "append": ctx.system_prompt}
            if ctx.system_prompt is not None
            else None
        )
        policy = ctx.policy if ctx.policy is not None else _direct_policy(ctx, pending_events)
        # `permission_mode` is never set (and `run()` refuses engine_auto):
        # the SDK's `auto` mode lets a model classifier approve tool calls
        # without `can_use_tool` (`ClaudeAgentOptions.can_use_tool` docstring: not
        # invoked for calls "already permitted by ... permission_mode";
        # `ClaudeSDKClient.set_permission_mode`: "'auto': A model classifier
        # approves or denies each tool call"), which would bypass the gate
        # (task-8 finding, RUNBOOK). Built-ins stay off (`tools=[]`).
        return ClaudeAgentOptions(
            tools=[],
            mcp_servers=cast("dict[str, Any]", _build_mcp_servers(ctx.tools)),
            can_use_tool=_make_can_use_tool(
                policy, pending_events, terminate_flag, stop_reason, round_cap
            ),
            # 05 §7: `StreamEvent`s feed model activity only (`report_work`);
            # `_drive_client` never maps them to events or the mirror.
            include_partial_messages=True,
            hooks={"PreCompact": [HookMatcher(hooks=[_make_precompact_hook(pending_events)])]},
            system_prompt=cast(Any, system_prompt),
            model=ctx.model_spec.model,
            cwd=ctx.session.cwd,
            # FR-6.5: the cap itself is enforced exactly by
            # `_CallerRoundCap` (the CLI runs a response's tools before its
            # own `max_turns` check, so `max_turns` alone overran it by one
            # round). `max_turns` stays as a BACKSTOP only, bounding a
            # runaway if the permission path were ever bypassed. Its value
            # is the responses a well-behaved capped run needs: N tool
            # rounds take N + 1 responses (each round's response plus the
            # final one); a schema turn adds one more (FR-1.3) -- the CLI
            # counts its StructuredOutput call as a turn, and when the model
            # first ends with plain text it appends a reminder and asks
            # again. On a PLAIN turn it cannot fire before the exact cap (no
            # non-caller tools exist). On a SCHEMA turn it CAN fire first --
            # invalid StructuredOutput attempts and reminders also use CLI
            # turns -- and `_drive_client` then completes the turn as a
            # schema turn without a valid result (`end_turn`, no result),
            # never as a false `max_tool_rounds`. None (uncapped) stays None.
            max_turns=(
                None
                if ctx.max_tool_rounds is None
                else ctx.max_tool_rounds + (2 if ctx.output_schema is not None else 1)
            ),
            resume=ctx.session.native_session_id,
            # 06 §4: on a subscription profile, "" for each auth variable
            # the caller has set (the SDK cannot remove one). If
            # `session_store` is ever set, the SDK's `_copy_auth_files` reads
            # `opt_env.get(...) or os.environ.get(...)`: "" falls back to the
            # real key and skips the Keychain copy, so that path fails closed.
            env=engine_env(self.profile.auth, self.auth_env_vars(), os.environ).overrides,
            output_format=(
                {"type": "json_schema", "schema": ctx.output_schema}
                if ctx.output_schema is not None
                else None
            ),
        )

    async def run(self, ctx: TurnContext) -> AsyncIterator[Event]:
        if ctx.session.options_snapshot.get("permission_mode") == "engine_auto":
            # Raised before `TurnStarted` (`supports_engine_auto=False`).
            raise Unsupported(
                'permission_mode="engine_auto" is not supported on the claude backend: '
                "the SDK's auto mode approves tool calls without can_use_tool"
            )
        async for event in self._run_turn(ctx):
            yield event

    async def _run_turn(self, ctx: TurnContext) -> AsyncIterator[Event]:
        yield TurnStarted(turn_id=ctx.turn_id)
        session_id = ctx.session.session_id
        effective_prompt = ctx.prompt
        if ctx.force_replay and ctx.load_replay_history is not None:
            # FR-6.1 REPLAY: the runner suppressed a stale/lost native id;
            # this fresh native session opens with the rendered mirror
            # transcript ahead of the caller's prompt. The mirror records
            # only the caller's prompt (runner-side separation).
            replay_rows = await ctx.load_replay_history()
            budget = ctx.compaction.keep_recent_tokens if ctx.compaction is not None else 20000
            effective_prompt = compose_replay_prompt(replay_rows, budget, ctx.prompt)
            yield ItemCompleted(
                message=resume_degraded_notice(
                    reason="native session unavailable or cross-backend continuation; "
                    "rendered mirror transcript injected into a fresh native session"
                )
            )
        # 05 §5: after any replay preamble, so only a prompt that itself
        # leads with `/` is prefixed (the preamble never does).
        effective_prompt = message_prompt(effective_prompt)
        pending_events: list[Event] = []
        terminate_flag = [False]
        stop_reason: list[str | None] = [None]
        # FR-6.5: whenever a cap is set, the adapter enforces it exactly
        # (`_CallerRoundCap`); `max_turns` is only a backstop.
        round_cap = (
            _CallerRoundCap(ctx.max_tool_rounds) if ctx.max_tool_rounds is not None else None
        )
        client = ClaudeSDKClient(
            self._build_options(ctx, pending_events, terminate_flag, stop_reason, round_cap)
        )
        self._clients[session_id] = client
        stop_request = asyncio.Event()
        self._stop_requests[session_id] = stop_request
        try:
            async for event in self._drive_client(
                client,
                ctx,
                effective_prompt,
                pending_events,
                terminate_flag,
                round_cap,
                stop_reason=stop_reason,
                stop_request=stop_request,
            ):
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
            if self._stop_requests.get(session_id) is stop_request:
                self._stop_requests.pop(session_id, None)
            try:
                # 05 §3.4: the CLI's descendants, while they can still be
                # found; a cancel here still reaches the disconnect below.
                await asyncio.to_thread(ctx.engine_closing)
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    # Cleanup-only failure (subprocess already gone, etc.):
                    # must never mask a result already yielded above, so it
                    # is logged, not raised (GUIDELINES §9).
                    _logger.exception(
                        "ClaudeSDKClient.disconnect() failed", extra={"turn_id": ctx.turn_id}
                    )

    async def _drive_client(
        self,
        client: ClaudeSDKClient,
        ctx: TurnContext,
        prompt: str,
        pending_events: list[Event],
        terminate_flag: list[bool],
        round_cap: _CallerRoundCap | None = None,
        *,
        stop_reason: list[str | None] | None = None,
        stop_request: asyncio.Event | None = None,
    ) -> AsyncIterator[Event]:
        # FR-6.6 pre-turn retry: connect/spawn failures are the one class
        # provably free of duplicated tool side effects (nothing ran yet).
        # Any connect exception is retried (bounded by the budget) -- the
        # same client object is reused; its subprocess never spawned.
        connect_retries = 0
        while True:
            try:
                await client.connect(prompt)
                break
            except Exception as exc:
                retry_settings = ctx.retry
                if retry_settings is None or connect_retries >= retry_settings.max_attempts:
                    raise
                connect_retries += 1
                delay = retry_settings.base_delay_s * 2 ** (connect_retries - 1)
                yield ItemCompleted(
                    message=retry_notice(
                        phase="connect",
                        attempt=connect_retries,
                        max_attempts=retry_settings.max_attempts,
                        delay_s=delay,
                        error=str(exc),
                    )
                )
                await anyio.sleep(delay)
        # 05 §3.4: the runner records the CLI process for the process report.
        engine_pid = _engine_pid(client)
        if engine_pid is not None:
            await asyncio.to_thread(ctx.engine_started, engine_pid)
        # The CLI's StructuredOutput pair (FR-1.3) is hidden from the stream;
        # its ids carry from the call's message to the result's.
        hidden_ids: set[str] = set()
        # The last VISIBLE response's text (langchain `_final_text`
        # semantics): an error result often carries no `result` text, and
        # FR-1.3 keeps the turn's text on a completion. It resets only when a
        # new model response yields a visible item (text or a caller tool) --
        # a hidden StructuredOutput-only response (e.g. after the CLI's
        # reminder) is not a reply the caller saw. The CLI streams one
        # AssistantMessage per content block, all sharing the response's
        # `message_id`; a block with neither `message_id` nor `uuid` cannot
        # be placed, so it counts as the same response.
        last_text: str | None = None
        last_visible_response: str | None = None
        async for message in client.receive_response():
            for event in pending_events:
                yield event
            pending_events.clear()
            if ctx.work is not None:
                report_work(ctx.work, message)
            if isinstance(message, AssistantMessage):
                if round_cap is not None:
                    round_cap.observe(message)
                visible = _without_structured_output(assistant_message_items(message), hidden_ids)
                response = message.message_id or message.uuid
                if (
                    response is not None
                    and response != last_visible_response
                    and any(item.kind in ("text", "tool_use") for item in visible)
                ):
                    last_text = None
                    last_visible_response = response
                for item in visible:
                    if item.kind == "text":
                        last_text = cast(str, item.content["text"])
                    yield ItemCompleted(message=item)
            elif isinstance(message, UserMessage):
                for item in _without_structured_output(user_message_items(message), hidden_ids):
                    yield ItemCompleted(message=item)
            elif isinstance(message, ResultMessage):
                self._native_ids[ctx.session.session_id] = message.session_id
                if is_aborted_result(message):
                    if stop_reason is not None and stop_reason[0] is not None:
                        # 05 §2.6: OUR policy stop -- a failed turn naming
                        # the reason, never a caller interrupt.
                        yield TurnFailed(turn_id=ctx.turn_id, error=stop_reason[0])
                        return
                    if terminate_flag[0]:
                        # FR-4.4: this abort was OUR PermissionResultDeny(
                        # interrupt=True) -- the broker ending the turn, not
                        # a caller stop(). Honest completion, batch results
                        # already delivered.
                        yield TurnCompleted(
                            result=TurnResult(
                                turn_id=ctx.turn_id,
                                status="completed",
                                end_reason="broker_terminated",
                                final_text=message.result,
                                usage=_usage_ints(message.usage),
                                cost_usd=message.total_cost_usd,
                            )
                        )
                        return
                    if round_cap is not None and round_cap.hit:
                        # FR-6.5: this abort was OUR cap denial -- an honest
                        # `max_tool_rounds` partial, with no structured
                        # result.
                        yield TurnCompleted(
                            result=TurnResult(
                                turn_id=ctx.turn_id,
                                status="completed",
                                end_reason="max_tool_rounds",
                                final_text=message.result
                                if message.result is not None
                                else last_text,
                                usage=_usage_ints(message.usage),
                                cost_usd=message.total_cost_usd,
                            )
                        )
                        return
                    # `Backend.run`'s contract (ports.py): an interrupted
                    # turn ends with neither `TurnCompleted` nor
                    # `TurnFailed` -- the turn runner assigns `interrupted`
                    # itself once this generator ends with no terminal
                    # event.
                    return
                if (
                    message.is_error
                    and not is_max_turns_result(message)
                    and not is_structured_output_exhausted_result(message)
                ):
                    # `is_max_turns_result` is excluded from the failure
                    # path: the CLI flags its own `max_turns` stop as an
                    # error, but that cap only exists because the caller
                    # set `ctx.max_tool_rounds` -- it completes honestly
                    # with `end_reason="max_tool_rounds"` instead (FR-6.5).
                    # Never held for open work: the error reaches the
                    # caller now, not after a work-silence timeout.
                    yield TurnFailed(turn_id=ctx.turn_id, error=_result_error(message))
                    return
                result = turn_result_from_result_message(message, ctx.turn_id)
                backstop_on_schema_turn = (
                    ctx.output_schema is not None
                    and is_max_turns_result(message)
                    and not (round_cap is not None and round_cap.hit)
                )
                if backstop_on_schema_turn:
                    # FR-1.3 over FR-6.5: on a schema turn the exact caller
                    # cap is `_CallerRoundCap` (a cap stop arrives as an
                    # aborted result, handled above). A `max_turns` result
                    # here is the BACKSTOP firing because invalid
                    # StructuredOutput attempts or the CLI's reminder used
                    # up CLI turns -- a schema turn without a valid result,
                    # which completes as `end_turn`, text kept, not a false
                    # `max_tool_rounds`. After OUR cap denial (`hit`) the cap
                    # did end the turn, so `max_tool_rounds` stands.
                    result.end_reason = "end_turn"
                if result.final_text is None and (
                    is_structured_output_exhausted_result(message) or backstop_on_schema_turn
                ):
                    result.final_text = last_text
                if ctx.output_schema is not None and isinstance(message.structured_output, dict):
                    # FR-1.3, additive: `final_text` stays the model's prose
                    # (`ResultMessage.result`); the result is ADDED beside
                    # it. No valid result leaves it None -- still a
                    # completion, text kept (module docstring).
                    result.structured_output = cast("dict[str, object]", message.structured_output)
                terminal = TurnCompleted(result=result)
                # 05 §3.3: the turn ends when the engine is done AND no work
                # is open; meanwhile the engine is still read.
                settled = [True]
                async for event in self._settle(
                    client, ctx, pending_events, hidden_ids, stop_request, settled
                ):
                    yield event
                if settled[0]:
                    yield terminal
                return
        for event in pending_events:
            yield event

    async def _settle(
        self,
        client: ClaudeSDKClient,
        ctx: TurnContext,
        pending_events: list[Event],
        hidden_ids: set[str],
        stop_request: asyncio.Event | None,
        settled: list[bool],
    ) -> AsyncIterator[Event]:
        """Wait, before the terminal event, until no work is open (05 §3.3).

        A no-op unless background or subagent work is open at the engine's
        result (so for every plain turn), or when `ctx.work` is unset; never
        run for an error result, which fails at once. Otherwise reads on through
        `receive_messages()` -- `receive_response()` stops at the result,
        but a background task's end arrives after it -- reporting work
        signals and mirroring what arrives, until `ctx.work.settled()`
        wakes. `settled[0]` is then its answer: False (a limit fired, or
        the runner cancelled the supervisor) means the caller yields no
        terminal and the runner's timeout path records the turn. A caller
        `interrupt()` (`stop_request`) also ends the wait with
        `settled[0] = False`, so the runner records `interrupted`.

        Plain `asyncio` futures, raced with `asyncio.wait`, keep this
        generator free of cancel scopes across its yields (the 0.5.0
        pitfall class the runner's supervisor task also avoids).
        """
        work = ctx.work
        if work is None or not any(
            item.kind in ("background", "subagent") for item in work.open_items()
        ):
            # Only background or subagent work outlives the engine's result;
            # a tool item it left open (no tool result) must not hold a
            # finished turn for the work-silence limit.
            return
        messages = client.receive_messages()
        waits: dict[str, asyncio.Future[Any]] = {
            "settled": asyncio.ensure_future(work.settled()),
            "message": asyncio.ensure_future(anext(messages)),
        }
        if stop_request is not None:
            waits["stop"] = asyncio.ensure_future(stop_request.wait())
        try:
            while True:
                await asyncio.wait(waits.values(), return_when=asyncio.FIRST_COMPLETED)
                # A message that finished in the same step as `settled()` or
                # a stop is still reported and mirrored first.
                read_next = False
                message_wait = waits.get("message")
                if message_wait is not None and message_wait.done():
                    del waits["message"]
                    try:
                        message = message_wait.result()
                    except StopAsyncIteration:
                        # The engine's stream ended; only the supervisor (or
                        # a stop) can end the wait now.
                        message = None
                    if message is not None:
                        read_next = True
                        for event in pending_events:
                            yield event
                        pending_events.clear()
                        report_work(work, message)
                        for event in _settle_phase_events(message, hidden_ids):
                            yield event
                if "stop" in waits and waits["stop"].done():
                    settled[0] = False
                    return
                if waits["settled"].done():
                    settled[0] = cast(bool, waits["settled"].result())
                    return
                if read_next:
                    waits["message"] = asyncio.ensure_future(anext(messages))
        finally:
            # Cancelled, not awaited (as in `WorkSupervisor._wait`): awaiting
            # them and suppressing their CancelledError would also swallow a
            # cancellation of this task.
            for wait in waits.values():
                wait.cancel()


def _settle_phase_events(message: object, hidden_ids: set[str]) -> list[Event]:
    """The mirror items of a message read after the engine's result while
    open work settles (`ClaudeBackend._settle`): the same items the main
    stream yields, StructuredOutput still hidden. A later `ResultMessage`
    (an injected turn's) adds no terminal -- the turn has one already."""
    if isinstance(message, AssistantMessage):
        items = assistant_message_items(message)
    elif isinstance(message, UserMessage):
        items = user_message_items(message)
    else:
        return []
    return [ItemCompleted(message=item) for item in _without_structured_output(items, hidden_ids)]
