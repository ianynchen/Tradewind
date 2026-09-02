"""EXPERIMENTAL -- never live-verified (no Cursor subscription; P-5 open).

Cursor adapter: the fourth `Backend` (ARCHITECTURE §3.1), over the official
async `cursor-sdk` (task-15 brief). Ships as `experimental` (see the first
line above) because Step 1 of the task-15 brief (a live spike proving
whether `Agent.resume()` can pick up an agent's own `cursor-agent` CLI
session -- ARCHITECTURE §7 P-5) and Step 4 (a live conformance run) BOTH
require a Cursor subscription that does not exist on this machine. This
module is written directly against the pinned SDK's installed source
(`cursor_sdk` 1.0.30 -- every type/behavior claim below was read off that
package, not off documentation), and its own unit/mapping tests run with no
network access, but nothing here has ever executed against a real Cursor
backend. `docs/ARCHITECTURE.md` §7 P-5 stays open until that changes.

**SDK-vs-brief discrepancy (flagged per the task-15 brief's own "Before You
Begin" instruction):** the brief describes turn construction as
`Agent.create(LocalAgentOptions(custom_tools=..., mcp=<caller defs
mapped>))`. The installed SDK's `LocalAgentOptions` (`cursor_sdk.types`) has
no `mcp` field at all -- `custom_tools` lives there, but MCP server
registration (`mcp_servers: Mapping[str, McpServerConfig]`) is a field of
the *outer* `AgentOptions`, not `LocalAgentOptions`. This module follows the
installed SDK's actual shape: `AgentOptions(local=LocalAgentOptions(cwd=...,
custom_tools=...), tools=[])`, built by `_agent_options` below.

**In-process tool bridge (`supports_in_process_tools=True`).** Every tool
`ctx.tools` (a fully-wired `ToolHost`) exposes -- local `Tool`s and whatever
`McpServerDef`s the caller declared, both already unified behind
`ToolHost.schemas()`/`ToolHost.call()` -- is rendered as one Cursor
`CustomTool` per schema (`_build_custom_tools`), each `execute` callback
dispatching straight into `ToolHost.call()`, mirroring `ClaudeBackend`'s own
in-process SDK-MCP-server bridge (`claude_backend.py`'s module docstring).
As there, `TurnContext` never exposes a caller's raw `SessionOptions.
mcp_servers` list to a backend, so a second, direct Cursor-level MCP
passthrough (`AgentOptions.mcp_servers=`, as opposed to bridging everything
through the one `ToolHost`) isn't wired here either -- same structural gap
`claude_backend.py`/`codex_backend.py` flag in their own module docstrings,
carried forward rather than re-litigated.

`AgentOptions(tools=[])` disables Cursor's own built-in toolset (shell,
edit, ...) entirely, mirroring `ClaudeAgentOptions.tools=[]` in
`claude_backend.py` and for the identical reason: Tradewind's whole premise
is a caller-declared, broker-gated tool set, and leaving Cursor's built-in
tools reachable by default would grant host access no `SessionOptions.
tools`/`mcp_servers` ever asked for.

**No permission gating (`supports_interactive_permissions=False`).**
Research (`docs/research/2026-09-01-agent-backends.md` §1.5: "Cursor has
file-based hooks.json only") and ARCHITECTURE's own component table ("...;
declared unavailable on Cursor") agree there is no programmatic
approval-callback mechanism on Cursor analogous to Claude's `can_use_tool`
or Codex's `approval_handler`. Because every tool this adapter exposes is
already Tradewind's own `CustomTool` bridge (never a Cursor built-in, which
are disabled above), this adapter COULD, in principle, still consult
`ctx.broker.decide()` itself inside `_build_custom_tools`'s `execute`
callback before calling `ToolHost.call()`, the same way `LangchainBackend`'s
own self-owned tool loop does. It deliberately does not: the capability
flag says False, and `turn_runner.py`'s own `tool_host_broker = broker if
backend.name == "codex" else None` line already only wires a broker into
`ToolHost` itself for Codex, so leaving this adapter's own bridge
broker-silent keeps the flag and the actual behavior in agreement (R-1:
"honest capability over false uniformity") rather than gating quietly
behind an unadvertised flag. No `PermissionRequested` event is ever
produced by this backend as a result.

**Per-turn bridge process lifecycle**, mirroring `CodexBackend`'s own choice
(module docstring there): a fresh `cursor-sdk-bridge` subprocess
(`AsyncClient.launch_bridge(workspace=ctx.session.cwd, ...)`) per turn,
closed (`client.aclose()`, which sends the bridge's own `Shutdown` RPC and
terminates the process) in a `finally` once the turn ends. `agent.close()`
(an RPC telling the *about-to-die* bridge process to release its live
handle on this one agent) is skipped: it would be redundant work performed
a moment before the whole process is killed anyway, so this adapter relies
on `client.aclose()` alone for teardown.

**Agent id capture + native resume (FR-3.2).** `ctx.session.native_session_id`
present -> `AsyncAgent.resume(native_session_id, options, client=client)`;
absent -> `AsyncAgent.create(options, client=client)`. Either way,
`custom_tools` (and every other per-agent option) is re-passed on *every*
call, per the SDK's own documented resume contract ("Must re-pass on
resume: inline MCP servers, `custom_tools`, ... else e.g. `agent.model is
None`" -- research doc §2) -- there is no persistent "session" object on
the SDK side to mutate across turns, exactly the same shape `ClaudeBackend`/
`CodexBackend` document for their own per-turn client construction. The
resulting `agent.agent_id` is captured into a per-session dict immediately
(mirroring `CodexBackend._native_ids`'s own "record_native_id" timing, not
deferred to the end of the turn) and `take_native_session_id` pops it --
same per-session-dict-plus-pop contract and rationale as
`ClaudeBackend.take_native_session_id`'s own docstring (one `CursorBackend`
instance is cached and reused across every session on its profile, so a
value must never be read by more than the one turn that produced it).

**Interrupt** (`run.cancel()`, per-session `AsyncRun` registry): a run
already in a terminal status raises `UnsupportedRunOperationError` when
cancelled (confirmed in the installed SDK's `_async_run.py`), which
`interrupt()` swallows and logs -- the same "no turn in flight is a no-op"
contract `ClaudeBackend.interrupt`'s own `CLIConnectionError` catch
documents for the identical race.

**Event mapping** (`sdk_message_items`, pure function, tested in
`tests/unit/test_cursor_mapping.py` against hand-built SDK dataclasses --
the installed SDK's actual `SDKMessage` union, not the research doc's
sketch, since that sketch and the installed types agree closely but the
types are what's authoritative here): `SDKAssistantMessage`'s `TextBlock`
content -> `text`; its `ToolUseBlock` content is SKIPPED (see below);
`SDKThinkingMessage` -> `thinking`; `SDKToolUseMessage` -> ONE combined
call+result record per the installed SDK (`call_id, name, args, status,
result` all on one dataclass, confirmed directly in `cursor_sdk/types.py`,
matching the research doc §4.2 "same record as the call -- split into two
rows" note) -- mapped into a `tool_use` + `tool_result` pair, but ONLY once
`status` reaches a terminal value (`"completed"`/`"error"`); a `"running"`
status update for the same `call_id` (the SDK streams status transitions
for one call over time) produces nothing, matching the research doc's
"store completed items, not deltas" recommendation. Assistant-message
`ToolUseBlock` content is skipped rather than *also* mapped to a `tool_use`
row: it carries no `status`/`result` of its own to pair against, and would
duplicate the authoritative `SDKToolUseMessage`-derived row under a
different id scheme. `SDKUserMessageEvent` is skipped (the turn runner
already mirrors `ctx.prompt` itself -- same precedent as `claude_backend.
user_message_items`/`codex_backend.thread_item_to_messages`). Every other
message kind (`SDKSystemMessage`, `SDKStatusMessage`, `SDKTaskMessage`,
`SDKRequestMessage`, `SDKUsageMessage`, and the SDK's own `Mapping[str,
Any]` fallback for anything it doesn't recognize either) -> `kind="event"`
with the raw payload preserved in `.raw` (research doc's own recommendation
for Cursor's `SDKStatusMessage`/`SDKTaskMessage`/`SDKRequestMessage`: "store
as kind='event' with raw payload").

`native_id`: `SDKToolUseMessage.call_id` for the `tool_use`/`tool_result`
pair it produces; `None` for `text`/`thinking` items -- unlike Claude/Codex,
the installed SDK's `SDKAssistantMessage`/`SDKThinkingMessage` carry no
stable per-item id at all (only `agent_id`/`run_id`, shared by every item in
a run), so there is nothing honest to put there. This is consistent with
`capabilities().supports_transcript_read=False`: nothing in this adapter or
`turn_runner.py`'s reconcile path ever keys off a Cursor `native_id` for
resume-time backfill the way `ResumePlanner.reconcile()` does for
Claude/Codex.

**DR-3 isolation (`_launch_local_options`)**: mirrors `codex_backend.
_codex_env`'s `CODEX_HOME` gate. `NativeStoreConfig.isolation_mode` on
requires `NativeStoreConfig.cursor_store` to be set (an opaque
`LocalAgentStoreConfig`/custom store handler, per that field's own
docstring) and passes it through as the whole bridge subprocess's local
agent store -- relocating every agent this adapter creates away from
Cursor's own default on-disk store, for hosts where no human runs the
`cursor-agent` CLI (cloud). Off by default, matching Codex's own choice and
reasoning.

**Deferred, matching `ClaudeBackend`/`LangchainBackend`'s own precedent**:
`ctx.output_schema` raises `Unsupported` (`capabilities().
supports_structured_output` is False -- nothing in the installed SDK's
`AgentOptions`/`SendOptions` accepts a JSON-schema-shaped output
constraint). `ModelSpec.effort` is not wired to anything on `AgentOptions`
either -- the task-15 brief's options list does not name it, same
"unlisted option is out of scope" reasoning `claude_backend.py` states for
its own `effort` gap.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any, ClassVar, cast

from cursor_sdk import (
    AgentOptions,
    CustomTool,
    CustomToolContext,
    LocalAgentOptions,
    RunResult,
    SDKAssistantMessage,
    SDKMessage,
    SDKThinkingMessage,
    SDKToolUseMessage,
    SDKUserMessageEvent,
    TextBlock,
    TokenUsage,
    ToolUseBlock,
    UnsupportedRunOperationError,
)
from cursor_sdk.asyncio import AsyncAgent, AsyncClient, AsyncRun

from tradewind.application.config import NativeStoreConfig
from tradewind.application.ports import Backend, TurnContext
from tradewind.application.tool_host import ToolHost
from tradewind.domain.errors import ConfigError, Unsupported
from tradewind.domain.events import Event, ItemCompleted, TurnCompleted, TurnFailed, TurnStarted
from tradewind.domain.models import (
    BackendName,
    Capabilities,
    NormalizedMessage,
    Profile,
    SessionRow,
)
from tradewind.domain.models import (
    TurnResult as DomainTurnResult,
)

_logger = logging.getLogger(__name__)

# `RunResult.status` values `run.wait()` returns once a run reaches a
# terminal state (`cursor_sdk.types.RunResultStatus`). `"cancelled"` is the
# one status `run.cancel()` (this adapter's own `interrupt()`) produces --
# handled the same way `ClaudeBackend`/`CodexBackend` handle their own
# aborted/interrupted terminal states (module docstring).
_CANCELLED_STATUS = "cancelled"
_FINISHED_STATUS = "finished"


# --- pure mapping: SDKMessage -> NormalizedMessage ----------------------


def _tool_result_text(result: object) -> str:
    """Render `SDKToolUseMessage.result` (`Any` on the SDK's own type) into
    the `tool_result` item's `content` string. Tradewind's own bridge tools
    (`_build_custom_tools`) always report back an MCP-result-shaped dict
    (`{"content": [{"type": "text", ...}], ...}`, matching `ToolHost.call()`'s
    own output rendered through `_normalize_custom_tool_result`, confirmed in
    the installed SDK's `_tool_callback.py`) -- this reads that shape when
    present, and falls back to `str`/`json.dumps` for anything else (a
    result from a Cursor built-in tool this adapter never actually enables,
    or an undocumented shape)."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, Mapping):
        content = result.get("content")
        if isinstance(content, list):
            parts = [
                str(block.get("text", ""))
                for block in content
                if isinstance(block, Mapping) and block.get("type") == "text"
            ]
            joined = "".join(parts)
            if joined:
                return joined
    return json.dumps(result, default=str)


def _tool_use_and_result(message: SDKToolUseMessage) -> list[NormalizedMessage]:
    args = message.args if isinstance(message.args, Mapping) else {}
    return [
        NormalizedMessage(
            role="assistant",
            kind="tool_use",
            content={"id": message.call_id, "name": message.name, "input": dict(args)},
            native_id=message.call_id,
        ),
        NormalizedMessage(
            role="tool",
            kind="tool_result",
            content={
                "tool_use_id": message.call_id,
                "content": _tool_result_text(message.result),
                "is_error": message.status == "error",
            },
            native_id=message.call_id,
        ),
    ]


def _assistant_message_items(message: SDKAssistantMessage) -> list[NormalizedMessage]:
    items: list[NormalizedMessage] = []
    for block in message.message.content:
        if isinstance(block, TextBlock):
            if block.text:
                items.append(
                    NormalizedMessage(role="assistant", kind="text", content={"text": block.text})
                )
        elif isinstance(block, ToolUseBlock):
            # Skipped: `SDKToolUseMessage` (module docstring) is the
            # authoritative source for this same call's tool_use/tool_result
            # pair -- this block carries no `status`/`result` of its own to
            # pair against, and mapping it too would duplicate that row
            # under a different id scheme.
            continue
        # Any other dict-shaped content block: no further structure to
        # normalize without guessing at an undocumented shape; skipped.
    return items


def _event_message(message: SDKMessage) -> NormalizedMessage:
    if isinstance(message, Mapping):
        message_type = str(message.get("type", ""))
        raw = dict(message)
    else:
        message_type = str(getattr(message, "type", ""))
        raw = dataclasses.asdict(cast(Any, message))
    return NormalizedMessage(
        role="assistant", kind="event", content={"type": message_type}, raw=raw
    )


def sdk_message_items(message: SDKMessage) -> list[NormalizedMessage]:
    """Normalize one `SDKMessage` from a run's event stream into the
    `ItemCompleted` items this turn persists (module docstring's event
    mapping section)."""
    if isinstance(message, SDKAssistantMessage):
        return _assistant_message_items(message)
    if isinstance(message, SDKThinkingMessage):
        if not message.text:
            return []
        return [
            NormalizedMessage(role="assistant", kind="thinking", content={"text": message.text})
        ]
    if isinstance(message, SDKToolUseMessage):
        if message.status not in ("completed", "error"):
            return []
        return _tool_use_and_result(message)
    if isinstance(message, SDKUserMessageEvent):
        # The turn runner already mirrors `ctx.prompt` itself -- re-emitting
        # it here would duplicate it (module docstring).
        return []
    return [_event_message(message)]


def _usage_dict(usage: TokenUsage) -> dict[str, int]:
    result = {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "total_tokens": usage.total_tokens,
    }
    if usage.reasoning_tokens is not None:
        result["reasoning_tokens"] = usage.reasoning_tokens
    return result


# --- tool bridge: ToolHost -> Cursor CustomTool --------------------------


def _make_execute(host: ToolHost, name: str) -> Any:
    async def execute(args: Mapping[str, Any], _context: CustomToolContext) -> dict[str, Any]:
        outcome = await host.call(name, dict(args))
        return {
            "content": [{"type": "text", "text": outcome.content}],
            "isError": outcome.is_error,
        }

    return execute


def _build_custom_tools(host: ToolHost) -> dict[str, CustomTool]:
    """Bridge every tool `host` exposes (local `Tool`s plus caller-declared
    `McpServerDef`s, already unified behind `ToolHost.schemas()`/`.call()`)
    into one Cursor `CustomTool` per schema (module docstring)."""
    tools: dict[str, CustomTool] = {}
    for schema in host.schemas():
        name = cast(str, schema["name"])
        tools[name] = CustomTool(
            execute=_make_execute(host, name),
            description=cast(str, schema.get("description", "")),
            input_schema=cast("dict[str, Any]", schema.get("input_schema", {})),
        )
    return tools


def _agent_options(
    model: str, cwd: str | None, custom_tools: dict[str, CustomTool]
) -> AgentOptions:
    """Build the `AgentOptions` passed to both `AsyncAgent.create` and
    `AsyncAgent.resume` (module docstring: re-passed on every call, per
    FR-3.2). `tools=[]` disables Cursor's own built-in toolset."""
    return AgentOptions(
        model=model,
        local=LocalAgentOptions(cwd=cwd, custom_tools=custom_tools or None),
        tools=[],
    )


def _launch_local_options(native_config: NativeStoreConfig) -> LocalAgentOptions | None:
    """DR-3 isolation-mode opt-in for `AsyncClient.launch_bridge(local=...)`
    (module docstring)."""
    if not native_config.isolation_mode:
        return None
    if native_config.cursor_store is None:
        raise ConfigError(
            "NativeStoreConfig.isolation_mode requires cursor_store to be set for "
            "CursorBackend (DR-3) -- without it there is nowhere isolated to point "
            "Cursor's local agent store at."
        )
    return LocalAgentOptions(store=native_config.cursor_store)


# --- native (subprocess-driven) turn execution ---------------------------


class CursorBackend(Backend):
    """`Backend` for Cursor via the official async `cursor-sdk` (see module
    docstring for the per-turn bridge-process/tool-bridge design, and its
    first line for this adapter's EXPERIMENTAL status)."""

    name: ClassVar[BackendName] = "cursor"

    def __init__(self, profile: Profile, native_config: NativeStoreConfig) -> None:
        super().__init__(profile, native_config)
        # Per-session, matching `ClaudeBackend`/`CodexBackend`'s own
        # precedent and its stated rationale: one `CursorBackend` instance
        # is cached and reused across every session on its profile
        # (`Tradewind._resolve_backend`), so both dicts are keyed by
        # tradewind `session_id`, never a single shared attribute.
        self._native_ids: dict[str, str] = {}
        self._runs: dict[str, AsyncRun] = {}

    def take_native_session_id(self, session_id: str) -> str | None:
        """Pop and return the native agent id `session_id`'s most recent
        turn recorded (`AsyncAgent.agent_id`, captured immediately after
        create/resume in `_run_turn`), or None if no turn has recorded one
        since the last call -- same per-session-pop contract as
        `ClaudeBackend.take_native_session_id` (see its docstring for why
        popping, not just reading, matters)."""
        return self._native_ids.pop(session_id, None)

    def capabilities(self) -> Capabilities:
        # Exactly the table in the task-15 brief/controller ruling.
        return Capabilities(
            supports_system_prompt=False,
            supports_structured_output=False,
            supports_interactive_permissions=False,
            supports_in_process_tools=True,
            supports_native_resume=True,
            supports_fork=False,
            supports_transcript_read=False,
        )

    async def probe_native(self, session: SessionRow) -> bool:
        # Cheap probe (matching `ClaudeBackend`/`CodexBackend`'s own
        # choice, documented there): a truthy `native_session_id` only
        # means a resume was *recorded*, not that `Agent.resume()` will
        # still succeed (deleted/archived agent, ...).
        return session.native_session_id is not None

    async def read_native_transcript(
        self,
        session: SessionRow,  # noqa: ARG002 -- part of the `Backend` interface; unused, see below
        after_native_id: str | None,  # noqa: ARG002 -- same
    ) -> list[NormalizedMessage]:
        raise Unsupported(
            "CursorBackend has no readable native transcript (supports_transcript_read=False)"
        )

    async def interrupt(self, session_id: str) -> None:
        run = self._runs.get(session_id)
        if run is None:
            return
        try:
            await run.cancel()
        except UnsupportedRunOperationError:
            # No turn in flight is a documented no-op (`Backend.interrupt`'s
            # contract) -- `AsyncRun.cancel()` raises this when the run
            # already reached a terminal status (confirmed in the installed
            # SDK's `_async_run.py`), the same kind of "nothing to
            # interrupt anymore" race `ClaudeBackend.interrupt`'s own
            # `CLIConnectionError` catch documents.
            _logger.info(
                "AsyncRun.cancel() called on a run already in a terminal status; "
                "treated as a no-op",
                extra={"session_id": session_id},
            )

    async def run(self, ctx: TurnContext) -> AsyncIterator[Event]:
        if ctx.output_schema is not None:
            # Raised before `TurnStarted`, matching `LangchainBackend.run`'s/
            # `ClaudeBackend.run`'s own capability-mismatch handling.
            raise Unsupported("structured output not yet implemented for cursor backend")
        async for event in self._run_turn(ctx):
            yield event

    async def _run_turn(self, ctx: TurnContext) -> AsyncIterator[Event]:
        yield TurnStarted(turn_id=ctx.turn_id)
        session_id = ctx.session.session_id
        try:
            client = await AsyncClient.launch_bridge(
                workspace=ctx.session.cwd, local=_launch_local_options(self.native_config)
            )
        except Exception as exc:
            yield TurnFailed(turn_id=ctx.turn_id, error=str(exc))
            return
        try:
            custom_tools = _build_custom_tools(ctx.tools)
            options = _agent_options(ctx.model_spec.model, ctx.session.cwd, custom_tools)
            native_session_id = ctx.session.native_session_id
            if native_session_id is not None:
                agent = await AsyncAgent.resume(native_session_id, options, client=client)
            else:
                agent = await AsyncAgent.create(options, client=client)
            self._native_ids[session_id] = agent.agent_id

            run = await agent.send(ctx.prompt)
            self._runs[session_id] = run
            try:
                async for event in self._consume(ctx.turn_id, run):
                    yield event
            finally:
                # Identity-checked pop, same reasoning as `ClaudeBackend.
                # _run_turn`'s own `_clients` cleanup: a second `run()` for
                # this `session_id` started before this `finally` runs
                # would already have overwritten `self._runs[session_id]`
                # with its own run.
                if self._runs.get(session_id) is run:
                    self._runs.pop(session_id, None)
        except Exception as exc:
            yield TurnFailed(turn_id=ctx.turn_id, error=str(exc))
        finally:
            try:
                await client.aclose()
            except Exception:
                # Cleanup-only failure (subprocess already gone, etc.): must
                # never mask a result already yielded above, so it is
                # logged, not raised (GUIDELINES §9).
                _logger.exception("AsyncClient.aclose() failed", extra={"turn_id": ctx.turn_id})

    async def _consume(self, turn_id: str, run: AsyncRun) -> AsyncIterator[Event]:
        async for event in run:
            if event.sdk_message is not None:
                for message in sdk_message_items(event.sdk_message):
                    yield ItemCompleted(message=message)
        result: RunResult = await run.wait()
        if result.status == _CANCELLED_STATUS:
            # `Backend.run`'s contract (ports.py): an interrupted turn ends
            # with neither `TurnCompleted` nor `TurnFailed` -- the turn
            # runner assigns `interrupted` itself once this generator ends
            # with no terminal event (same as `ClaudeBackend._drive_client`'s
            # `is_aborted_result` handling).
            return
        if result.status == _FINISHED_STATUS:
            yield TurnCompleted(
                result=DomainTurnResult(
                    turn_id=turn_id,
                    status="completed",
                    final_text=result.result,
                    usage=_usage_dict(result.usage) if result.usage is not None else {},
                    # `RunResult` carries no per-run dollar cost (unlike
                    # Claude's `ResultMessage.total_cost_usd`) -- billed
                    # cost is only available via `Agent.get_usage()`, a
                    # separate, eventually-consistent RPC this adapter does
                    # not wire into every turn. `None` is honest absence,
                    # not an unwired field.
                    cost_usd=None,
                )
            )
            return
        yield TurnFailed(
            turn_id=turn_id,
            error=result.result or f"cursor run ended with status={result.status!r}",
        )
