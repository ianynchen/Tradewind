"""Codex adapter: the third `Backend` (ARCHITECTURE §3.1), over the official
`openai-codex` Python SDK (task-14 brief; approval mechanics verified live in
`docs/research/2026-09-02-codex-approvals-spike.md`, task 13).

Talks to Codex through the bundled `codex app-server` subprocess. Unlike
`ClaudeBackend`/`LangchainBackend`, the friendly `Codex`/`AsyncCodex`/
`AsyncCodexClient` wrappers cannot be used at all: only the low-level,
synchronous `openai_codex.client.CodexClient` accepts an `approval_handler`,
and only hand-built `ThreadStartParams`/`ThreadResumeParams`/`TurnStartParams`
(not the convenience `ApprovalMode` enum, which only reaches Codex's own
internal auto-reviewer) can set `approvals_reviewer=ApprovalsReviewer.user`,
the one setting that routes approval requests to *our* callback at all
(spike §1-§2). This adapter therefore drives `CodexClient` directly rather
than through `Codex`/`Thread`, while still reusing the SDK's own
`openai_codex.api.TurnHandle` (a plain `@dataclass(slots=True)`,
constructible by hand) for its `.stream()`/`.interrupt()` methods.

**Sync-to-async bridge.** `CodexClient` is entirely synchronous (blocking
subprocess I/O, its own internal reader thread) and `approval_handler` runs
on *that* reader thread, not on any asyncio loop -- so this adapter runs one
dedicated `threading.Thread` per turn (`_drive_turn`) that starts the client,
opens/resumes the thread, starts the turn, and pumps `TurnHandle.stream()`'s
notifications onto a plain thread-safe `queue.Queue`. The async `run()` side
(`_consume`) drains that queue via `anyio.to_thread.run_sync(queue.get)` in a
loop -- the standard blocking-iterator-to-async-generator bridge. The
approval_handler (invoked on `CodexClient`'s own reader thread, a *third*
thread distinct from both the event loop and `_drive_turn`'s worker) bridges
into `ctx.broker.decide()` (an `async def`) via
`asyncio.run_coroutine_threadsafe(..., loop).result()` (spike §1), and pushes
any resulting `PermissionRequested` directly onto the same shared queue --
safe because `queue.Queue` is thread-safe, and because Codex's own JSON-RPC
protocol is a blocking round trip (the reader thread cannot read the next
line -- e.g. the tool call's own `item/completed` -- until this callback
returns), so a denial's `PermissionRequested` is always enqueued before the
corresponding tool-result item.

**Approval routing (spike §4-§5, controller ruling).** `_make_approval_handler`
branches on the JSON-RPC method:
  - `item/commandExecution/requestApproval` / `item/fileChange/requestApproval`
    (Codex's own built-in shell/apply_patch tools -- left enabled, not
    disabled the way `ClaudeBackend` disables Claude Code's built-ins,
    because the brief asks for these to be broker-*gated*, not removed):
    broker-mapped 1:1 (`allow`->accept, `deny`->reject +
    `PermissionRequested`).
  - `mcpServer/elicitation/request` with
    `params["_meta"]["codex_approval_kind"] == "mcp_tool_call"` (every
    tradewind-tool call Codex makes through the stdio shim, spike §4 --
    *not* the `item/*/requestApproval` shape despite the naming symmetry
    with the two above): when `params["serverName"]` is tradewind's own
    shim server (`_SHIM_SERVER_NAME`), auto-accept -- `ToolHost.call()`
    itself gates authoritatively for this path (`ToolHost.__init__`'s
    docstring; wired in by `turn_runner.py` for this backend specifically),
    so no second broker consult is made here (see that docstring for why
    asking the same "ask"-semantics broker twice is a real, not just
    theoretical, correctness concern). For any *other* MCP server's
    elicitation (a caller could in principle register more via
    `config_overrides` some other way, even though this adapter itself only
    ever registers the one shim server -- defense in depth), the bare tool
    name is extracted via a regex on the human-readable `message` field
    (spike §4.2: not a documented, stable field -- flagged there for
    controller confirmation, carried forward here unresolved) and
    broker-mapped; **extraction failure denies fail-closed** without
    consulting the broker at all (nothing to ask it about).
  - Every other method: `{}`, matching `CodexClient`'s own
    `_default_approval_handler`'s fallback for anything it doesn't
    recognize either -- an implicit no-op/deny shape on the wire, not
    something this adapter invents.

**MCP tool wiring.** Since `capabilities().supports_in_process_tools` is
False (Codex has no in-process tool bridge at all, unlike Claude's SDK-level
MCP server), the *only* way tradewind tools reach Codex is
`ToolHost.shim_server_def()` (task-12), registered into the spawned `codex
app-server` subprocess via `CodexConfig.config_overrides` (`--config
mcp_servers.<name>.<field>=<TOML value>` CLI flags -- spike §4, confirmed
live against a real MCP server). Same structural gap as `ClaudeBackend`'s own
module docstring flags: `TurnContext` never exposes a caller's raw
`SessionOptions.mcp_servers` to a backend, so a second, direct pass-through
of caller-declared MCP servers (as opposed to bridging all of `ctx.tools`
through the one shim) isn't wired here either -- same flag, same reasoning,
carried forward rather than re-litigated.

**Per-turn client lifecycle**, matching `ClaudeBackend`'s own choice: a fresh
`CodexClient` (== a fresh `codex app-server` subprocess) per turn, with
`ctx.session.native_session_id` (when present) driving `thread_resume`
instead of `thread_start` -- `Thread.id`, captured at whichever of those two
calls succeeds, is what `take_native_session_id` later hands back to
`turn_runner.py` for `rehome_native` (same per-session-dict-plus-pop pattern
as `ClaudeBackend`, same rationale: one instance is cached and reused across
every session on its profile).

**Approval/sandbox defaults.** `ThreadStartParams`/`ThreadResumeParams` are
always built with `approvals_reviewer=ApprovalsReviewer.user` (the only
setting that reaches this adapter's own `approval_handler` at all -- spike
§2, not optional) and `approval_policy=on-request` (the exact combo verified
live in the spike). `sandbox` defaults to `workspace-write` -- deliberately
*not* the spike's own `read-only` (which the spike chose specifically to
force every write through an approval prompt for its own verification
purposes), since the brief calls for "a mode that permits tool use with
approvals": `workspace-write` lets ordinary in-workspace operations proceed
while destructive/out-of-workspace ones still escalate. **Except when no
broker is actually configured anywhere** (`SessionOptions.permission_broker`
and `TradewindConfig.permission_broker` both unset -- `turn_runner.py` then
hands every backend `_AllowAllBroker`, which permits every call with no
human/policy backstop at all): final review wave ruling ("disclose AND safe
default") -- `workspace-write` with nothing gating tool calls would grant
Codex unrestricted filesystem writes by default, so `_run_turn` detects that
exact case (`ctx.broker`'s duck-typed `is_default_allow_all` marker, see
`turn_runner._AllowAllBroker`) and defaults `sandbox` to `read-only`
instead. Both `sandbox`/`approval_policy` are overridable per profile via
`Profile.backend_options["sandbox"]`/`["approval_policy"]` (raw
`SandboxMode`/`AskForApprovalValue` wire-value strings -- e.g. `"read-only"`,
`"never"`), since bypassing the `Sandbox`/`ApprovalMode` convenience enums
(spike §2) means this adapter talks the SDK's raw wire vocabulary directly
rather than inventing a second one -- an explicit `backend_options["sandbox"]`
always wins over either default, broker configured or not. See README.md's
"Security defaults" section for the caller-facing summary and
`docs/RUNBOOK.md` for more detail.

**Event mapping** (`thread_item_to_messages`, pure functions, tested in
`tests/unit/test_codex_mapping.py` against hand-built SDK dataclasses):
`agentMessage`->`text`, `reasoning`->`thinking`, `mcpToolCall`/
`dynamicToolCall`->one `tool_use` + one `tool_result` (Codex bundles a call
and its own result into a single completed `ThreadItem`, unlike Claude's
separate `ToolUseBlock`/`ToolResultBlock` -- both messages are synthesized
here from the one item, sharing its `id` as `tool_use_id` so tradewind's own
tool_use/tool_result pairing convention holds), `commandExecution`->
`command_execution`, `fileChange`->`file_change`, `webSearch`->`web_search`,
`userMessage`->skipped (the turn runner already mirrors `ctx.prompt` itself
-- re-emitting it here would duplicate it, same reasoning as
`ClaudeBackend.user_message_items`'s own skip of plain-text `UserMessage`),
anything else (plan, subAgentActivity, collab tool calls, ...) -> `kind=
"event"` with the raw item preserved in `.raw` (brief: "unknown->event with
raw"). Every produced item's `native_id` is the `ThreadItem`'s own `.id`
(finer-grained than Claude's one-uuid-per-whole-message, but consistent:
`import_native_items`'s dedup and `ResumePlanner`'s `after_native_id` cursor
both key on it either way).

`take_native_session_id`/`capabilities().supports_fork`: `thread_fork` exists
on the SDK (`Codex.thread_fork`/`CodexClient.thread_fork`) but nothing in
`Tradewind`'s own client-level `fork()` (a separate, higher-level concept --
copying the mirror history into a new session, `SessionStorePort.copy_
history`) calls into a backend's native fork at all today. `True` here
documents that Codex's *own* thread-fork primitive exists and could back a
native-fork path in a later task -- not that this adapter wires one up now
(same "document even if unused" pattern the brief calls for).
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import re
import threading
from collections.abc import AsyncIterator, Callable
from typing import Any, ClassVar, Final, cast

import anyio
from openai_codex.api import TurnHandle
from openai_codex.client import ApprovalHandler, CodexClient, CodexConfig
from openai_codex.generated.v2_all import (
    AgentMessageThreadItem,
    ApprovalsReviewer,
    AskForApproval,
    AskForApprovalValue,
    CommandExecutionThreadItem,
    DynamicToolCallStatus,
    DynamicToolCallThreadItem,
    FileChangeThreadItem,
    InputTextDynamicToolCallOutputContentItem,
    ItemCompletedNotification,
    McpToolCallStatus,
    McpToolCallThreadItem,
    MessagePhase,
    ReasoningEffort,
    ReasoningThreadItem,
    SandboxMode,
    TextUserInput,
    ThreadItem,
    ThreadReadResponse,
    ThreadResumeParams,
    ThreadStartParams,
    ThreadTokenUsage,
    ThreadTokenUsageUpdatedNotification,
    TurnCompletedNotification,
    TurnStartParams,
    TurnStatus,
    UserInput,
    UserMessageThreadItem,
    WebSearchThreadItem,
)
from openai_codex.models import JsonObject, Notification

from tradewind.application.config import NativeStoreConfig
from tradewind.application.ports import Backend, TurnContext
from tradewind.domain.errors import ConfigError, Unsupported
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
    EffortLevel,
    McpServerDef,
    NormalizedMessage,
    PermissionBroker,
    Profile,
    SessionRow,
    TurnResult,
    Verdict,
)

_logger = logging.getLogger(__name__)

# `ToolHost.shim_server_def()`'s own hardcoded `McpServerDef.name`
# (`tool_host.py`) -- the discriminator this adapter uses to tell tradewind's
# own tool bridge apart from any other MCP server's elicitation (module
# docstring's approval-routing section).
_SHIM_SERVER_NAME = "toolproxy"

# Sentinel for `_extract_tool_name`'s failure: no PermissionRequested for a
# tool this adapter genuinely could not name (fail-closed, not a real tool).
_UNKNOWN_TOOL_NAME = "<unknown>"

# tradewind's own established denied-tool-result sentinel -- `ToolHost.
# call()`'s broker-deny branch (`tool_host.py`) and `LangchainBackend`'s tool
# loop (`langchain_backend.py`) both use this exact string, deliberately, as
# the `tool_result` content for a denied call. `_denied_shim_permission_event`
# below recognizes it to synthesize the `PermissionRequested` event FR-4.1
# requires (task-14 fix round 1: found live -- see that function's docstring
# for why the approval_handler itself can't emit it for shim calls).
_DENIED_TOOL_RESULT_CONTENT = "permission denied"

# `mcpServer/elicitation/request`'s `message` field is exactly
# `Allow the {server} MCP server to run tool "{tool}"?` (spike §4, verified
# live against `codex app-server` 0.147.0) -- not a documented, stable
# contract; flagged for controller confirmation same as the spike itself.
_TOOL_NAME_FROM_MESSAGE_RE = re.compile(r'run tool "(.*)"\?$')

# Verified live in the spike (§2-§3): the one combo that both reaches this
# adapter's own `approval_handler` (`approvals_reviewer=user`, not the
# `auto_review`/`guardian_subagent` alternatives) and lets ordinary tool use
# proceed while still gating destructive/patch actions (module docstring).
_DEFAULT_SANDBOX = SandboxMode.workspace_write
_DEFAULT_APPROVAL_POLICY_VALUE = AskForApprovalValue.on_request

# Safe default (module docstring's "Approval/sandbox defaults" section, item
# 3a): used instead of `_DEFAULT_SANDBOX` only when the effective broker is
# `turn_runner._AllowAllBroker` (no broker configured anywhere) -- with
# nothing gating tool calls at all, `workspace-write` would mean
# unrestricted filesystem writes by default.
_SAFE_DEFAULT_SANDBOX_NO_BROKER = SandboxMode.read_only

# Sentinel `_consume` recognizes to mean "`_drive_turn` has nothing more to
# put on the queue" -- distinct from any `Notification`/`Event`/exception
# that could legitimately be queued, so `is` identity is unambiguous.
_DONE: Final[object] = object()


# --- pure mapping: ThreadItem -> NormalizedMessage ---------------------


def _mcp_result_text(item: McpToolCallThreadItem) -> str:
    if item.error is not None:
        return item.error.message
    if item.result is None:
        return ""
    parts: list[str] = []
    for block in item.result.content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _dynamic_tool_call_result_text(item: DynamicToolCallThreadItem) -> str:
    if item.content_items is None:
        return ""
    parts: list[str] = []
    for entry in item.content_items:
        root = entry.root
        if isinstance(root, InputTextDynamicToolCallOutputContentItem):
            parts.append(root.text)
    return "".join(parts)


def _tool_call_messages(
    *,
    native_id: str,
    tool_id: str,
    name: str,
    arguments: dict[str, object],
    is_error: bool,
    result_text: str,
) -> list[NormalizedMessage]:
    return [
        NormalizedMessage(
            role="assistant",
            kind="tool_use",
            content={"id": tool_id, "name": name, "input": arguments},
            native_id=native_id,
        ),
        NormalizedMessage(
            role="tool",
            kind="tool_result",
            content={"tool_use_id": tool_id, "content": result_text, "is_error": is_error},
            native_id=native_id,
        ),
    ]


def _denied_shim_permission_event(item: ThreadItem) -> PermissionRequested | None:
    """`PermissionRequested` for a completed `McpToolCallThreadItem`/
    `DynamicToolCallThreadItem` that `ToolHost.call()`'s own broker gate
    denied, or `None` for anything else (found live, task-14 fix round 1).

    **Why this exists here and not in the `approval_handler`**: this
    adapter's `mcpServer/elicitation/request` branch (`_decide_mcp_
    elicitation`) auto-accepts every call to tradewind's own shim server
    without consulting the broker at all -- deliberately, per the
    controller ruling, since `ToolHost.call()` (reached moments later, over
    the toolproxy socket, once Codex's own MCP client actually issues
    `tools/call`) gates authoritatively for that path. That division of
    labor means the approval_handler layer never learns whether the call
    was actually denied -- only `ToolHost.call()`'s own return value
    (`ToolOutcome(content="permission denied", is_error=True)`) carries
    that fact, and it flows back to this adapter only as the completed
    item's own result content, by the time `item/completed` arrives here.
    FR-4.1 requires `PermissionRequested` fire on every deny regardless of
    which layer made it, so this recognizes tradewind's own denied-tool
    sentinel (`_DENIED_TOOL_RESULT_CONTENT`) on a tool_call item and
    synthesizes the event from it, rather than leaving deny-via-`ToolHost`
    silently unobserved. Explicitly scoped to `root.server ==
    _SHIM_SERVER_NAME` (mirroring `_decide_mcp_elicitation`'s own
    `server_name == _SHIM_SERVER_NAME` check) -- item-6a fix: without that
    guard, any OTHER MCP server's failed call whose result text happens to
    equal `_DENIED_TOOL_RESULT_CONTENT` (a plain, unnamespaced string, not
    something only `ToolHost` could produce) would misfire this synthesis
    for a tool tradewind's own broker was never even consulted about.
    """
    root = item.root
    if isinstance(root, McpToolCallThreadItem):
        if (
            root.server == _SHIM_SERVER_NAME
            and root.status != McpToolCallStatus.completed
            and _mcp_result_text(root) == _DENIED_TOOL_RESULT_CONTENT
        ):
            arguments = root.arguments if isinstance(root.arguments, dict) else {}
            return PermissionRequested(
                tool_name=root.tool, tool_input=cast("dict[str, Any]", arguments), verdict="deny"
            )
        return None
    if isinstance(root, DynamicToolCallThreadItem):
        if (
            root.status != DynamicToolCallStatus.completed
            and _dynamic_tool_call_result_text(root) == _DENIED_TOOL_RESULT_CONTENT
        ):
            arguments = root.arguments if isinstance(root.arguments, dict) else {}
            return PermissionRequested(
                tool_name=root.tool, tool_input=cast("dict[str, Any]", arguments), verdict="deny"
            )
        return None
    return None


def thread_item_to_messages(item: ThreadItem) -> list[NormalizedMessage]:
    """Normalize one completed `ThreadItem` into the `ItemCompleted` items
    this turn persists (module docstring's event-mapping section)."""
    root = item.root
    native_id = root.id
    if isinstance(root, UserMessageThreadItem):
        # The turn runner already mirrors `ctx.prompt` itself -- re-emitting
        # it here would duplicate it (see module docstring).
        return []
    if isinstance(root, AgentMessageThreadItem):
        if not root.text:
            return []
        return [
            NormalizedMessage(
                role="assistant", kind="text", content={"text": root.text}, native_id=native_id
            )
        ]
    if isinstance(root, ReasoningThreadItem):
        text = "\n".join(root.summary or root.content or [])
        if not text:
            return []
        return [
            NormalizedMessage(
                role="assistant", kind="thinking", content={"text": text}, native_id=native_id
            )
        ]
    if isinstance(root, McpToolCallThreadItem):
        arguments = root.arguments if isinstance(root.arguments, dict) else {}
        return _tool_call_messages(
            native_id=native_id,
            tool_id=root.id,
            name=root.tool,
            arguments=cast("dict[str, object]", arguments),
            is_error=root.status != McpToolCallStatus.completed,
            result_text=_mcp_result_text(root),
        )
    if isinstance(root, DynamicToolCallThreadItem):
        arguments = root.arguments if isinstance(root.arguments, dict) else {}
        return _tool_call_messages(
            native_id=native_id,
            tool_id=root.id,
            name=root.tool,
            arguments=cast("dict[str, object]", arguments),
            is_error=root.status != DynamicToolCallStatus.completed,
            result_text=_dynamic_tool_call_result_text(root),
        )
    if isinstance(root, CommandExecutionThreadItem):
        return [
            NormalizedMessage(
                role="assistant",
                kind="command_execution",
                content={
                    "command": root.command,
                    "cwd": root.cwd.root,
                    "status": root.status.value,
                    "exit_code": root.exit_code,
                    "output": root.aggregated_output,
                },
                native_id=native_id,
            )
        ]
    if isinstance(root, FileChangeThreadItem):
        return [
            NormalizedMessage(
                role="assistant",
                kind="file_change",
                content={
                    "status": root.status.value,
                    "changes": [
                        {"path": change.path, "kind": change.kind.root.type, "diff": change.diff}
                        for change in root.changes
                    ],
                },
                native_id=native_id,
            )
        ]
    if isinstance(root, WebSearchThreadItem):
        return [
            NormalizedMessage(
                role="assistant",
                kind="web_search",
                content={"query": root.query, "results": root.results},
                native_id=native_id,
            )
        ]
    return [
        NormalizedMessage(
            role="assistant",
            kind="event",
            content={"type": root.type},
            native_id=native_id,
            raw=item.model_dump(mode="json", by_alias=True),
        )
    ]


def thread_read_items(
    response: ThreadReadResponse, after_native_id: str | None
) -> list[NormalizedMessage]:
    """Map `thread/read(includeTurns=True)`'s full turn history into
    `NormalizedMessage`s (`Backend.read_native_transcript`'s contract:
    `after_native_id=None` reads from the start).

    **Deliberately the opposite rule from `ClaudeBackend.
    native_transcript_items` for a cursor miss**: there, `after_native_id`
    not found among the entries returns everything (untested-but-plausible
    "not present yet"), because Claude's live-stream item ids and its
    transcript-file ids are the *same* value (`ClaudeBackend`'s own
    docstring, confirmed empirically). **Confirmed empirically this task
    that Codex's are NOT**: `item/completed`'s live `ThreadItem.id` is the
    underlying model-provider's own id (`msg_...`/`rs_...`/...), while the
    *same logical item* read back later via `thread_read` carries a
    completely different, sequentially-renumbered id (`item-1`, `item-2`,
    ...) -- and `thread_read` was also observed dropping reasoning items
    from the persisted view entirely. `ResumePlanner.reconcile()`
    (`resume.py`) calls this with `after_native_id=store.last_native_id(...)`
    -- always a *live* id once any turn has run -- so with Claude's "not
    found -> everything" rule, a cursor miss would fire on essentially
    *every* reconcile call (not just a genuinely new session), and since the
    returned "item-N" ids were never seen before either, `import_native_
    items`'s own dedup can't catch them: the mirror's history grows
    unbounded, duplicating on every resumed turn (task-14 report: reproduced
    live). A cursor miss here therefore returns nothing instead -- this
    trades away this backend's ability to backfill genuinely new
    out-of-band native activity (DR-3) when the cursor can't be resolved,
    in favor of never corrupting the mirror; flagged as a known limitation
    in the task-14 report, not a silent behavior gap.
    """
    items: list[ThreadItem] = [item for turn in response.thread.turns for item in turn.items]
    if after_native_id is not None:
        index = next((i for i, item in enumerate(items) if item.root.id == after_native_id), None)
        items = items[index + 1 :] if index is not None else []
    result: list[NormalizedMessage] = []
    for item in items:
        result.extend(thread_item_to_messages(item))
    return result


def _as_agent_message(item: ThreadItem) -> AgentMessageThreadItem | None:
    root = item.root
    return root if isinstance(root, AgentMessageThreadItem) else None


def _final_text_from_items(agent_messages: list[AgentMessageThreadItem]) -> str | None:
    """The turn's final response text, mirroring `openai_codex._run`'s own
    private `_final_assistant_response_from_items` selection rule (last
    `phase==final_answer` message; else the last phase-less one) --
    reimplemented rather than imported since that helper is private to the
    SDK's own `Thread.run()` convenience path, which this adapter bypasses
    (module docstring)."""
    fallback: str | None = None
    for message in reversed(agent_messages):
        if message.phase == MessagePhase.final_answer:
            return message.text
        if message.phase is None and fallback is None:
            fallback = message.text
    return fallback


def _usage_dict(usage: ThreadTokenUsage) -> dict[str, int]:
    total = usage.total
    return {
        "input_tokens": total.input_tokens,
        "cached_input_tokens": total.cached_input_tokens,
        "output_tokens": total.output_tokens,
        "reasoning_output_tokens": total.reasoning_output_tokens,
        "total_tokens": total.total_tokens,
    }


def _reasoning_effort(effort: EffortLevel | None) -> ReasoningEffort | None:
    return ReasoningEffort(effort) if effort is not None else None


# --- config_overrides: McpServerDef -> `--config` TOML assignments ------


def _toml_string(value: str) -> str:
    # TOML basic-string escaping matches JSON's closely enough for the
    # controlled values this adapter ever passes here (executable paths,
    # module names, a generated socket path) -- no exotic characters.
    return json.dumps(value)


def _toml_array(values: list[str]) -> str:
    return json.dumps(values)


def _toml_inline_table(mapping: dict[str, str]) -> str:
    # TOML inline-table syntax (`key = value`, comma-separated) -- NOT
    # JSON's `"key": value` -- so this is hand-built, not `json.dumps`.
    # Only ever used for tradewind's own env var names (alnum + underscore),
    # which are valid bare TOML keys needing no quoting.
    pairs = ", ".join(f"{key} = {_toml_string(value)}" for key, value in mapping.items())
    return "{ " + pairs + " }"


def mcp_server_config_overrides(server: McpServerDef) -> tuple[str, ...]:
    """`CodexConfig.config_overrides` entries (`--config key=value` CLI
    flags) registering `server` as one of Codex's `mcp_servers.<name>.*`
    config entries (spike §4). Only `stdio` transport is supported -- the
    only shape `ToolHost.shim_server_def()` ever returns, and the only one
    this adapter registers (module docstring)."""
    if server.transport != "stdio" or not server.command:
        raise ConfigError(
            "CodexBackend only wires stdio MCP servers into config_overrides, got "
            f"transport={server.transport!r} command={server.command!r}"
        )
    command, *args = server.command
    overrides = [
        f"mcp_servers.{server.name}.command={_toml_string(command)}",
        f"mcp_servers.{server.name}.args={_toml_array(args)}",
    ]
    if server.env:
        overrides.append(f"mcp_servers.{server.name}.env={_toml_inline_table(server.env)}")
    return tuple(overrides)


# --- approval handler: JSON-RPC server-request -> broker verdict --------


def _extract_tool_name(params: JsonObject) -> str | None:
    message = params.get("message")
    if not isinstance(message, str):
        return None
    match = _TOOL_NAME_FROM_MESSAGE_RE.search(message)
    return match.group(1) if match else None


def _decide(
    broker: PermissionBroker,
    loop: asyncio.AbstractEventLoop,
    tool_name: str,
    tool_input: dict[str, object],
) -> Verdict:
    """Bridge the sync `approval_handler` callback (running on `CodexClient`'s
    own reader thread) into `broker.decide()` (an `async def`) via
    `run_coroutine_threadsafe` against the loop captured before the client
    started (spike §1) -- blocks this (non-loop) thread until the broker
    decides, exactly mirroring `PermissionBroker.decide`'s own "ask"
    semantics contract."""
    future = asyncio.run_coroutine_threadsafe(
        broker.decide(tool_name, cast("dict[str, Any]", tool_input)), loop
    )
    return future.result()


def _decide_exec_or_patch(
    broker: PermissionBroker,
    loop: asyncio.AbstractEventLoop,
    out_queue: queue.Queue[object],
    tool_name: str,
    tool_input: dict[str, object],
) -> JsonObject:
    verdict = _decide(broker, loop, tool_name, tool_input)
    if verdict == "deny":
        out_queue.put(
            PermissionRequested(
                tool_name=tool_name,
                tool_input=cast("dict[str, Any]", tool_input),
                verdict="deny",
            )
        )
        return {"decision": "reject"}
    return {"decision": "accept"}


def _decide_mcp_elicitation(
    broker: PermissionBroker,
    loop: asyncio.AbstractEventLoop,
    out_queue: queue.Queue[object],
    params: JsonObject,
) -> JsonObject:
    meta = params.get("_meta")
    meta_dict = meta if isinstance(meta, dict) else {}
    if meta_dict.get("codex_approval_kind") != "mcp_tool_call":
        # Some other MCP elicitation kind (spike §4's own caveat) -- not a
        # tool-call permission decision, so this handler has nothing to say
        # about it (matches `CodexClient`'s own unrecognized-method default).
        return {}
    server_name = params.get("serverName")
    tool_input = cast("dict[str, object]", meta_dict.get("tool_params", {}))
    if server_name == _SHIM_SERVER_NAME:
        # Every tradewind-tool call Codex makes goes through this branch.
        # `ToolHost.call()` itself gates authoritatively for this path
        # (`ToolHost.__init__`'s docstring; `turn_runner.py` wires the
        # broker into this backend's `ToolHost` specifically) -- consulting
        # the broker a second time here would ask an "ask"-semantics broker
        # the same question twice, so this branch does not.
        return {"action": "accept", "content": {}}
    bare_name = _extract_tool_name(params)
    if bare_name is None:
        # Fail-closed (controller ruling): can't identify which tool this
        # is, so there is nothing to ask the broker about -- deny rather
        # than guess.
        out_queue.put(
            PermissionRequested(
                tool_name=_UNKNOWN_TOOL_NAME,
                tool_input=cast("dict[str, Any]", tool_input),
                verdict="deny",
            )
        )
        return {"action": "decline", "content": {}}
    verdict = _decide(broker, loop, bare_name, tool_input)
    if verdict == "deny":
        out_queue.put(
            PermissionRequested(
                tool_name=bare_name, tool_input=cast("dict[str, Any]", tool_input), verdict="deny"
            )
        )
        return {"action": "decline", "content": {}}
    return {"action": "accept", "content": {}}


def make_approval_handler(
    broker: PermissionBroker,
    loop: asyncio.AbstractEventLoop,
    out_queue: queue.Queue[object],
) -> ApprovalHandler:
    """Build the sync `CodexClient(approval_handler=...)` callback (module
    docstring's approval-routing section)."""

    def handler(method: str, params: JsonObject | None) -> JsonObject:
        params_dict: JsonObject = params or {}
        if method == "item/commandExecution/requestApproval":
            return _decide_exec_or_patch(
                broker, loop, out_queue, "shell", {"command": params_dict.get("command")}
            )
        if method == "item/fileChange/requestApproval":
            return _decide_exec_or_patch(
                broker, loop, out_queue, "apply_patch", {"changes": params_dict.get("changes")}
            )
        if method == "mcpServer/elicitation/request":
            return _decide_mcp_elicitation(broker, loop, out_queue, params_dict)
        return {}

    return handler


# --- native (subprocess-driven) turn execution ---------------------------


def _codex_env(native_config: NativeStoreConfig) -> dict[str, str] | None:
    if not native_config.isolation_mode:
        return None
    if native_config.codex_home is None:
        raise ConfigError(
            "NativeStoreConfig.isolation_mode requires codex_home to be set for CodexBackend "
            "(DR-3) -- without it there is nowhere isolated to point CODEX_HOME at."
        )
    return {"CODEX_HOME": str(native_config.codex_home)}


def _sandbox_mode_from_options(
    backend_options: dict[str, Any], *, allow_all_broker: bool
) -> SandboxMode:
    """`backend_options["sandbox"]` always wins when set. Otherwise: safe
    default (module docstring, item 3a) -- `read-only` when `allow_all_broker`
    is True (no broker configured anywhere, nothing else would gate tool
    calls), else the existing `workspace-write` default."""
    default = _SAFE_DEFAULT_SANDBOX_NO_BROKER if allow_all_broker else _DEFAULT_SANDBOX
    value = backend_options.get("sandbox", default.value)
    return SandboxMode(value)


def _approval_policy_value_from_options(backend_options: dict[str, Any]) -> AskForApprovalValue:
    value = backend_options.get("approval_policy", _DEFAULT_APPROVAL_POLICY_VALUE.value)
    return AskForApprovalValue(value)


def _drive_turn(
    client: CodexClient,
    session: SessionRow,
    prompt: str,
    model: str,
    effort: ReasoningEffort | None,
    output_schema: JsonObject | None,
    system_prompt: str | None,
    sandbox: SandboxMode,
    approval_policy_value: AskForApprovalValue,
    out_queue: queue.Queue[object],
    record_native_id: Callable[[str], None],
    record_handle: Callable[[TurnHandle], None],
) -> None:
    """Runs entirely on its own `threading.Thread` (module docstring): starts
    `client`, opens/resumes the thread, starts the turn, and pumps every
    notification from `TurnHandle.stream()` onto `out_queue` until the
    stream ends (clean finish or exception) -- `_consume` (on the event
    loop) is the other half of this bridge."""
    try:
        client.start()
        client.initialize()
        approval_policy = AskForApproval(root=approval_policy_value)
        if session.native_session_id is not None:
            resumed = client.thread_resume(
                session.native_session_id,
                ThreadResumeParams(
                    thread_id=session.native_session_id,
                    cwd=session.cwd,
                    model=model,
                    sandbox=sandbox,
                    approval_policy=approval_policy,
                    approvals_reviewer=ApprovalsReviewer.user,
                    base_instructions=system_prompt,
                ),
            )
            thread_id = resumed.thread.id
        else:
            started = client.thread_start(
                ThreadStartParams(
                    cwd=session.cwd,
                    model=model,
                    sandbox=sandbox,
                    approval_policy=approval_policy,
                    approvals_reviewer=ApprovalsReviewer.user,
                    base_instructions=system_prompt,
                )
            )
            thread_id = started.thread.id
        record_native_id(thread_id)

        turn_started = client.turn_start(
            thread_id,
            # `prompt` (the positional `input_items` arg) is what actually
            # reaches the wire: `CodexClient.turn_start` builds its payload
            # as `{**_params_dict(params), "threadId": ..., "input":
            # self._normalize_input_items(input_items)}` (client.py) --
            # the trailing dict-literal key always wins, so this positional
            # `prompt` (a bare `str`, which `_normalize_input_items` wraps
            # as `[{"type": "text", "text": prompt}]`) overwrites whatever
            # `TurnStartParams.input` below produces, every time.
            prompt,
            params=TurnStartParams(
                thread_id=thread_id,
                # Required by `TurnStartParams` (no default) but never
                # actually sent -- see the comment above. Built from the
                # same `prompt` purely to satisfy pydantic validation, not
                # because its value matters.
                input=[UserInput(TextUserInput(type="text", text=prompt))],
                model=model,
                effort=effort,
                output_schema=output_schema,
            ),
        )
        handle = TurnHandle(client, thread_id, turn_started.turn.id)
        record_handle(handle)
        for notification in handle.stream():
            out_queue.put(notification)
    except BaseException as exc:  # forwarded across the thread boundary, never swallowed
        out_queue.put(exc)
    finally:
        client.close()
        out_queue.put(_DONE)


class CodexBackend(Backend):
    """`Backend` for Codex via the official `openai-codex` SDK (see module
    docstring for the sync-driving/approval-bridge design)."""

    name: ClassVar[BackendName] = "codex"

    def __init__(self, profile: Profile, native_config: NativeStoreConfig) -> None:
        super().__init__(profile, native_config)
        # Per-session, matching `ClaudeBackend`'s own `_native_ids`/
        # `_clients` precedent and its stated rationale: one `CodexBackend`
        # instance is cached and reused across every session on its profile
        # (`Tradewind._resolve_backend`), so both dicts are keyed by
        # tradewind `session_id`, never a single shared attribute.
        self._turn_handles: dict[str, TurnHandle] = {}
        self._native_ids: dict[str, str] = {}

    def take_native_session_id(self, session_id: str) -> str | None:
        """Pop and return the native thread id `session_id`'s most recent
        turn recorded (its `Thread.id`, captured at `thread_start`/
        `thread_resume` in `_drive_turn`), or None if no turn has recorded
        one since the last call -- same per-session-pop contract as
        `ClaudeBackend.take_native_session_id` (see its docstring for why
        popping, not just reading, matters)."""
        return self._native_ids.pop(session_id, None)

    def capabilities(self) -> Capabilities:
        # Exactly the table in the task-14 brief/controller ruling.
        return Capabilities(
            supports_system_prompt=True,
            supports_structured_output=True,
            supports_interactive_permissions=True,
            supports_in_process_tools=False,
            supports_native_resume=True,
            supports_fork=True,
            supports_transcript_read=True,
            # The agentic loop runs inside the `codex app-server` engine,
            # which exposes no round/turn cap (`TurnStartParams` has none) --
            # a cap could only be faked (counting socket calls, then
            # interrupting), so the flag stays honest and `run()` raises
            # `Unsupported` when `ctx.max_tool_rounds` is set (FR-6.5).
            supports_tool_round_cap=False,
        )

    async def probe_native(self, session: SessionRow) -> bool:
        # Cheap probe (matching `ClaudeBackend.probe_native`'s own choice,
        # documented there): a truthy `native_session_id` only means a
        # resume was *recorded*, not that `thread_resume` will still
        # succeed (deleted thread, ...) -- a full existence probe would mean
        # spinning up a whole `codex app-server` subprocess just to ask,
        # which is `read_native_transcript`'s job when it's actually needed,
        # not this cheap check's.
        return session.native_session_id is not None

    def _reader_config(self) -> CodexConfig:
        return CodexConfig(env=_codex_env(self.native_config))

    def _read_thread_sync(self, native_session_id: str) -> ThreadReadResponse:
        client = CodexClient(config=self._reader_config())
        try:
            client.start()
            client.initialize()
            return client.thread_read(native_session_id, include_turns=True)
        finally:
            client.close()

    async def read_native_transcript(
        self, session: SessionRow, after_native_id: str | None
    ) -> list[NormalizedMessage]:
        """Read `session`'s native `codex` thread via `thread/read
        (includeTurns=true)` and map it into `NormalizedMessage`s
        (`Backend.read_native_transcript`'s contract).

        **Known limitation (FR-6.4, confirmed live, task-14 fix round 1):**
        once `after_native_id` is given (i.e. after the first turn), this
        will return **nothing**, not a genuine backfill. Codex's
        live-streamed `item/completed` ids and its `thread/read`-returned
        item ids are two *different* id schemes for the same logical item
        (confirmed empirically -- see `thread_read_items`'s docstring for
        the full reproduction and reasoning), so `after_native_id` -- always
        a live-scheme id, from `store.last_native_id()` -- can never match
        an item this call returns; `thread_read_items` treats that cursor
        miss as "nothing new" rather than "everything" (the opposite of
        `ClaudeBackend`'s own rule) specifically to avoid corrupting the
        mirror with duplicate history, at the cost of this method never
        actually surfacing a human's out-of-band `codex` CLI activity on the
        same thread (DR-3) until a real fix lands (id normalization or a
        content-hash-based dedup). This is a data-completeness gap, not a
        capability lie: `read_native_transcript` itself works correctly
        (confirmed: `after_native_id=None`, the fresh-session case, reads
        the full transcript fine) and every other consumer of
        `supports_transcript_read`/`supports_native_resume` -- `probe_native`,
        `thread_resume`-based context continuity, single-turn reads --
        is unaffected, so the capability flags stay `True` rather than being
        flipped to hide this.
        """
        native_session_id = session.native_session_id
        if native_session_id is None:
            return []
        response = await anyio.to_thread.run_sync(self._read_thread_sync, native_session_id)
        return thread_read_items(response, after_native_id)

    async def interrupt(self, session_id: str) -> None:
        handle = self._turn_handles.get(session_id)
        if handle is None:
            return
        try:
            await anyio.to_thread.run_sync(handle.interrupt)
        except Exception:
            # No turn in flight is a documented no-op (`Backend.interrupt`'s
            # contract); the SDK does not document a specific exception type
            # for "the turn already ended" the way `ClaudeSDKClient.
            # interrupt()`'s `CLIConnectionError` message is (`ClaudeBackend.
            # interrupt`'s own comment) -- caught broadly and logged instead
            # of narrowly matched, since a race with the turn finishing on
            # its own is exactly the kind of "nothing to interrupt anymore"
            # case that should also end up a no-op.
            _logger.exception("TurnHandle.interrupt() failed", extra={"session_id": session_id})

    async def run(self, ctx: TurnContext) -> AsyncIterator[Event]:
        if ctx.max_tool_rounds is not None:
            # Raised before `TurnStarted`, matching `LangchainBackend.run`'s
            # own capability-mismatch handling for `ctx.output_schema`
            # (`supports_tool_round_cap` is False -- see `capabilities()`).
            raise Unsupported("max_tool_rounds is not enforceable on the codex backend")
        async for event in self._run_turn(ctx):
            yield event

    async def _run_turn(self, ctx: TurnContext) -> AsyncIterator[Event]:
        yield TurnStarted(turn_id=ctx.turn_id)
        session_id = ctx.session.session_id
        out_queue: queue.Queue[object] = queue.Queue()
        loop = asyncio.get_running_loop()
        approval_handler = make_approval_handler(ctx.broker, loop, out_queue)
        try:
            shim_def = await ctx.tools.shim_server_def()
            config = CodexConfig(
                config_overrides=mcp_server_config_overrides(shim_def),
                env=_codex_env(self.native_config),
            )
            client = CodexClient(config=config, approval_handler=approval_handler)
            backend_options = self.profile.backend_options
            # Duck-typed marker check (module docstring, item 3a) -- see
            # `turn_runner._AllowAllBroker.is_default_allow_all`'s own
            # comment for why this is `getattr`, not an `isinstance` check
            # against a class this adapter never imports.
            allow_all_broker = cast(bool, getattr(ctx.broker, "is_default_allow_all", False))
            sandbox = _sandbox_mode_from_options(backend_options, allow_all_broker=allow_all_broker)
            approval_policy_value = _approval_policy_value_from_options(backend_options)
            effort = _reasoning_effort(ctx.model_spec.effort)

            handle_ref: list[TurnHandle] = []

            def record_handle(handle: TurnHandle) -> None:
                handle_ref.append(handle)
                self._turn_handles[session_id] = handle

            def record_native_id(native_thread_id: str) -> None:
                self._native_ids[session_id] = native_thread_id

            worker = threading.Thread(
                target=_drive_turn,
                args=(
                    client,
                    ctx.session,
                    ctx.prompt,
                    ctx.model_spec.model,
                    effort,
                    ctx.output_schema,
                    ctx.system_prompt,
                    sandbox,
                    approval_policy_value,
                    out_queue,
                    record_native_id,
                    record_handle,
                ),
                daemon=True,
            )
            worker.start()
            terminal_event_observed = False
            try:
                async for event in self._consume(ctx.turn_id, out_queue):
                    if isinstance(event, (TurnCompleted, TurnFailed)):
                        terminal_event_observed = True
                    yield event
            finally:
                # Identity-checked pop, same reasoning as `ClaudeBackend.
                # _run_turn`'s own `_clients` cleanup: a second `run()` for
                # this `session_id` started before this `finally` runs would
                # already have overwritten `self._turn_handles[session_id]`
                # with its own handle.
                if handle_ref and self._turn_handles.get(session_id) is handle_ref[0]:
                    self._turn_handles.pop(session_id, None)
                if not terminal_event_observed and handle_ref:
                    # Item-4 fix: reached when the caller abandoned
                    # `session.stream()` before this turn ever reached
                    # `TurnCompleted`/`TurnFailed` (`aclosing` propagates
                    # `GeneratorExit` into the `async for` above from an
                    # early `break`/`aclose()`) -- `_drive_turn`'s worker
                    # thread is then still blocked inside `TurnHandle.
                    # stream()` waiting on a notification that will never
                    # come, so `worker.join()` below would hang the process
                    # indefinitely without first asking Codex to actually
                    # stop the turn. Guarded try/except, same reasoning as
                    # `interrupt()`'s own comment above: a race with the
                    # turn finishing on its own between the
                    # `terminal_event_observed` check and this call is
                    # exactly the kind of "nothing to interrupt anymore"
                    # case that must stay a no-op, not escape a `finally`.
                    try:
                        await anyio.to_thread.run_sync(handle_ref[0].interrupt)
                    except Exception:
                        _logger.exception(
                            "best-effort interrupt on an abandoned turn failed",
                            extra={"session_id": session_id},
                        )
                await anyio.to_thread.run_sync(worker.join)
        except Exception as exc:
            # Only reachable for a failure *before* `_consume` starts
            # draining the queue (e.g. `shim_server_def()` itself raising) --
            # `_consume` maps every worker-thread failure to `TurnFailed`
            # itself and returns rather than raising, so this does not
            # double-handle those.
            yield TurnFailed(turn_id=ctx.turn_id, error=str(exc))

    async def _consume(self, turn_id: str, out_queue: queue.Queue[object]) -> AsyncIterator[Event]:
        agent_messages: list[AgentMessageThreadItem] = []
        usage: ThreadTokenUsage | None = None
        while True:
            item = await anyio.to_thread.run_sync(out_queue.get)
            if item is _DONE:
                return
            if isinstance(item, PermissionRequested):
                yield item
                continue
            if isinstance(item, BaseException):
                yield TurnFailed(turn_id=turn_id, error=str(item))
                return
            notification = cast(Notification, item)
            payload = notification.payload
            if isinstance(payload, ItemCompletedNotification):
                agent = _as_agent_message(payload.item)
                if agent is not None:
                    agent_messages.append(agent)
                permission_event = _denied_shim_permission_event(payload.item)
                if permission_event is not None:
                    yield permission_event
                for message in thread_item_to_messages(payload.item):
                    yield ItemCompleted(message=message)
            elif isinstance(payload, ThreadTokenUsageUpdatedNotification):
                usage = payload.token_usage
            elif isinstance(payload, TurnCompletedNotification):
                turn = payload.turn
                if turn.status == TurnStatus.interrupted:
                    # `Backend.run`'s contract (ports.py): an interrupted
                    # turn ends with neither `TurnCompleted` nor
                    # `TurnFailed` -- the turn runner assigns `interrupted`
                    # itself once this generator ends with no terminal
                    # event (same as `ClaudeBackend._drive_client`'s
                    # `is_aborted_result` handling).
                    return
                if turn.status == TurnStatus.completed:
                    yield TurnCompleted(
                        result=TurnResult(
                            turn_id=turn_id,
                            status="completed",
                            # The engine reports `completed` only when the
                            # model genuinely ended its turn (truncation or
                            # a mid-loop stop surfaces as `failed` with a
                            # `TurnError` instead), and `max_tool_rounds`
                            # can never be the reason here -- `run()` raises
                            # `Unsupported` before any cap could apply.
                            end_reason="end_turn",
                            final_text=_final_text_from_items(agent_messages),
                            usage=_usage_dict(usage) if usage is not None else {},
                            # Codex's own `Turn`/`TurnCompletedNotification`
                            # carries no per-turn cost figure (unlike
                            # Claude's `ResultMessage.total_cost_usd`) --
                            # `None` is honest absence, not an unwired field.
                            cost_usd=None,
                        )
                    )
                else:
                    error = (
                        turn.error.message
                        if turn.error is not None
                        else f"codex turn ended with status={turn.status.value}"
                    )
                    yield TurnFailed(turn_id=turn_id, error=error)
                return
