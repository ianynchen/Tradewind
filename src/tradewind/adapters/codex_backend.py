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

**Approval routing (spike §4-§5, controller ruling; 05-turn-governance
§2, §7).** `make_approval_handler` sends every request through
`ctx.policy.gate()` (the broker is asked when the rules say `ask`, with
today's names and arguments, 05 §1.1) and branches on the JSON-RPC method:
  - `item/commandExecution/requestApproval` / `item/fileChange/requestApproval`
    (Codex's own built-in shell/apply_patch tools -- left enabled, not
    disabled the way `ClaudeBackend` disables Claude Code's built-ins,
    because the brief asks for these to be broker-*gated*, not removed):
    a `shell` / `file_write` action; `allow`->accept, `deny`->reject +
    `PermissionRequested`, `stop`->reject + `PermissionRequested`, then the
    turn is interrupted and fails with the reason. A file-change request
    carries no changes on app-server 0.147.0, so the action takes them from
    the item's `item/started` (`ApprovalLedger`).
  - `mcpServer/elicitation/request` with
    `params["_meta"]["codex_approval_kind"] == "mcp_tool_call"` (every
    tradewind-tool call Codex makes through the stdio shim, spike §4 --
    *not* the `item/*/requestApproval` shape despite the naming symmetry
    with the two above): when `params["serverName"]` is tradewind's own
    shim server (`_SHIM_SERVER_NAME`), auto-accept as a structural
    pass-through (05 §2.3) -- `ToolHost.call()` itself gates
    authoritatively for this path (`ToolHost.__init__`'s
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
    gated as an `mcp` action; **extraction failure denies fail-closed**
    without consulting the broker at all (a missing critical field, which
    the floor denies).
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

**Approval/sandbox defaults.** Outside engine_auto (below),
`ThreadStartParams`/`ThreadResumeParams` are
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

**Engine auto (05 §2.5).** `permission_mode="engine_auto"` uses Codex's own
auto-reviewer, `approvals_reviewer=ApprovalsReviewer.auto_review` (the value
the SDK's `ApprovalMode.auto_review` maps to, `openai_codex/_approval_mode.py`),
with the workspace-write sandbox (`_thread_settings`); requests that still
reach this client go through the gate.

**Observe and work signals (05 §2.1, §3, §7).** A completed command or file
change that had no approval request is observed (record, or an after-the-
fact stop); `item/started` never is. `report_work` maps items, output and
progress, auto-review notices, terminal input and model deltas onto
`ctx.work`; none of it becomes an event. **Process report (05 §3.4): not
done here** -- the `codex app-server` subprocess is held only in
`CodexClient._proc` (private; `openai_codex/client.py`), so its PID is not
reachable through a public API.

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
import contextlib
import importlib.metadata
import json
import logging
import os
import queue
import re
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Final, cast

import anyio
from openai_codex.api import TurnHandle
from openai_codex.client import ApprovalHandler, CodexClient, CodexConfig
from openai_codex.generated.v2_all import (
    AgentMessageDeltaNotification,
    AgentMessageThreadItem,
    ApprovalsReviewer,
    AskForApproval,
    AskForApprovalValue,
    CommandExecutionOutputDeltaNotification,
    CommandExecutionThreadItem,
    ContextCompactedNotification,
    DynamicToolCallStatus,
    DynamicToolCallThreadItem,
    FileChangeThreadItem,
    FileUpdateChange,
    FindInPageWebSearchAction,
    InputTextDynamicToolCallOutputContentItem,
    ItemCompletedNotification,
    ItemGuardianApprovalReviewCompletedNotification,
    ItemGuardianApprovalReviewStartedNotification,
    ItemStartedNotification,
    McpToolCallProgressNotification,
    McpToolCallStatus,
    McpToolCallThreadItem,
    MessagePhase,
    OpenPageWebSearchAction,
    ReasoningEffort,
    ReasoningSummaryTextDeltaNotification,
    ReasoningTextDeltaNotification,
    ReasoningThreadItem,
    SandboxMode,
    SearchWebSearchAction,
    TerminalInteractionNotification,
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
from tradewind.application.engine_env import engine_env
from tradewind.application.policy_gate import GateDecision, PolicyGate, caller_tool_action
from tradewind.application.ports import Backend, TurnContext
from tradewind.application.supervisor import WorkSupervisor
from tradewind.application.workspace_scan import CustomizationPattern
from tradewind.domain.compaction import compose_replay_prompt
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
    ModelSpec,
    NormalizedMessage,
    Profile,
    SessionRow,
    TurnResult,
    WebAccess,
    calculate_cost,
    engine_compaction_record,
    resume_degraded_notice,
    retry_notice,
)
from tradewind.domain.policy import Action, ActionKind, TurnVars, floor_tradewind

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
        # Record the action and queries so every URL Codex opened is in the record (07 §6).
        action = root.action.root if root.action is not None else None
        return [
            NormalizedMessage(
                role="assistant",
                kind="web_search",
                content={
                    "query": root.query,
                    "results": root.results,
                    "action": action.model_dump(mode="json", by_alias=True) if action else None,
                    "queries": getattr(action, "queries", None),
                },
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


def _structured_output_from_text(text: str | None) -> dict[str, object] | None:
    """FR-1.3: a schema turn's structured result, parsed from the engine's
    final answer (which, under `output_schema`, IS the JSON text), or None
    when that answer is absent or not a JSON object -- the turn then still
    completes, with no result."""
    if text is None:
        return None
    try:
        parsed = cast(object, json.loads(text))
    except json.JSONDecodeError:
        return None
    return cast("dict[str, object]", parsed) if isinstance(parsed, dict) else None


def _computed_cost(model_spec: ModelSpec, usage: dict[str, int]) -> float | None:
    """`TurnResult.cost_usd` from the tier's cost table (FR-10.5); None
    without one. Field mapping per the Phase-1 spec: codex's
    `cached_input_tokens` is a subset of `input_tokens` billed at the
    cache-read rate (OpenAI usage semantics), and codex reports no
    cache-write figure."""
    meta = model_spec.meta
    if meta is None or meta.cost is None:
        return None
    cached = usage.get("cached_input_tokens", 0)
    return calculate_cost(
        meta.cost,
        input_tokens=max(usage.get("input_tokens", 0) - cached, 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_read_tokens=cached,
    )


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


def web_search_overrides(web: WebAccess | None) -> tuple[str, ...]:
    """`--config` entries for Codex's own web search (07 §4a). No web
    setting leaves Codex's default as it is (owner decision 11)."""
    if web is None:
        return ()
    return ('web_search="live"',) if web.search else ('web_search="disabled"',)


# --- approval handler: JSON-RPC server-request -> policy gate ----------


def _extract_tool_name(params: JsonObject) -> str | None:
    message = params.get("message")
    if not isinstance(message, str):
        return None
    match = _TOOL_NAME_FROM_MESSAGE_RE.search(message)
    return match.group(1) if match else None


# --- turn governance: engine messages -> actions and work signals -------
# (05-turn-governance §1, §7 Codex row). Pure mapping; the decisions are the
# policy gate's.


def _in_workspace(path: str, workspace: str | None) -> bool:
    """Whether a patch path is inside `workspace` -- a security field (05
    §1): a relative path is joined to `workspace` (the thread's cwd) and
    `..` is resolved lexically (symlinks are not followed), then one
    `commonpath` check applies to relative and absolute paths alike. An
    unknown workspace gives False, the narrower answer."""
    if workspace is None:
        return False
    root = os.path.normpath(workspace)
    target = os.path.normpath(os.path.join(root, path))
    return os.path.commonpath([root, target]) == root


def shell_action(params: JsonObject) -> Action:
    """`item/commandExecution/requestApproval` params -> a `shell` action
    (critical field `command`). `sandboxed` is not reported by the request,
    so it stays `None`."""
    command = params.get("command")
    return Action(
        kind="shell",
        fields={
            "command": command,
            "cwd": params.get("cwd"),
            "sandboxed": None,
            "interactive": False,
        },
        backend="codex",
        native_name="shell",
        native_args=dict(params),
        summary=command if isinstance(command, str) else "",
        missing_critical_field=not isinstance(command, str),
    )


def file_write_action(
    changes: list[FileUpdateChange] | None,
    *,
    workspace: str | None,
    native_args: dict[str, object],
) -> Action:
    """A Codex patch -> a `file_write` action: one `{path, change,
    in_workspace}` per changed file. `changes=None` (not known) marks the
    critical field missing, which the floor denies (owner M8)."""
    fields_changes = [
        {
            "path": change.path,
            "change": change.kind.root.type,
            "in_workspace": _in_workspace(change.path, workspace),
        }
        for change in changes or []
    ]
    return Action(
        kind="file_write",
        fields={"changes": fields_changes},
        backend="codex",
        native_name="apply_patch",
        native_args=native_args,
        summary=", ".join(str(c["path"]) for c in fields_changes),
        missing_critical_field=changes is None,
    )


def mcp_action(server: object, tool: str | None, args: dict[str, object]) -> Action:
    """An MCP call to a server the engine connects to itself -> an `mcp`
    action; an unreadable tool name marks the critical field missing
    (today's fail-closed rule, now the floor's)."""
    return Action(
        kind="mcp",
        fields={"server": server, "tool": tool, "args": args},
        backend="codex",
        native_name=tool or _UNKNOWN_TOOL_NAME,
        native_args=args,
        summary=f"{server}:{tool}",
        missing_critical_field=tool is None or not isinstance(server, str),
    )


def _web_actions(item: WebSearchThreadItem) -> list[Action]:
    """The actions of a completed `webSearch` item (07 §4): a search is
    `web_search` (one action per entry of `queries` when `query` is unset),
    `openPage` and `findInPage` are `fetch_url`; no action, an unknown one,
    or a missing query or url sets `missing_critical_field` (decision 13)."""
    root = item.action.root if item.action is not None else None
    native_args = root.model_dump(mode="json", by_alias=True) if root is not None else {}
    # What the stop message names when the query or url is missing.
    unmapped = root.type if root is not None else "webSearch"

    def one(kind: ActionKind, field: str, value: str | None) -> Action:
        return Action(
            kind=kind,
            fields={field: value},
            backend="codex",
            native_name="webSearch",
            native_args=native_args,
            summary=value or unmapped,
            missing_critical_field=value is None,
        )

    if isinstance(root, SearchWebSearchAction):
        if root.query is not None:
            return [one("web_search", "query", root.query)]
        return [one("web_search", "query", q) for q in root.queries or []] or [
            one("web_search", "query", None)
        ]
    if isinstance(root, OpenPageWebSearchAction | FindInPageWebSearchAction):
        return [one("fetch_url", "url", root.url)]
    return [one("web_search", "query", None)]


def observed_actions(item: ThreadItem, *, workspace: str | None) -> list[Action]:
    """The actions of a completed command, file-change or web item, for
    `observe` (05 §7, 07 §4); `[]` for every other item kind."""
    root = item.root
    if isinstance(root, CommandExecutionThreadItem):
        return [shell_action({"command": root.command, "cwd": root.cwd.root})]
    if isinstance(root, FileChangeThreadItem):
        return [
            file_write_action(
                root.changes,
                workspace=workspace,
                native_args={"changes": [c.model_dump(mode="json") for c in root.changes]},
            )
        ]
    if isinstance(root, WebSearchThreadItem):
        return _web_actions(root)
    return []


def report_work(work: WorkSupervisor, payload: object) -> None:
    """Map one notification payload onto work signals (05 §7, Codex row).
    Signals only -- nothing here becomes an event or a mirror item.

    - `item/started` / `item/completed` -> `started(id, "tool")` / `ended`,
      except agent-message and reasoning items (model output: `model_active`)
      and the user's own message (nothing);
    - command output, terminal input, MCP progress and auto-review notices ->
      `active` (an auto review names its target item, else its review id);
    - agent-message and reasoning deltas -> `model_active`;
    - `turn/completed` -> `engine_done`.
    """
    if isinstance(payload, ItemStartedNotification | ItemCompletedNotification):
        root = payload.item.root
        if isinstance(root, AgentMessageThreadItem | ReasoningThreadItem):
            work.model_active()
        elif isinstance(root, UserMessageThreadItem):
            return
        elif isinstance(payload, ItemStartedNotification):
            work.started(root.id, "tool", root.type)
        else:
            work.ended(root.id)
    elif isinstance(
        payload,
        CommandExecutionOutputDeltaNotification
        | TerminalInteractionNotification
        | McpToolCallProgressNotification,
    ):
        work.active(payload.item_id)
    elif isinstance(
        payload,
        ItemGuardianApprovalReviewStartedNotification
        | ItemGuardianApprovalReviewCompletedNotification,
    ):
        work.active(payload.target_item_id or payload.review_id)
    elif isinstance(
        payload,
        AgentMessageDeltaNotification
        | ReasoningTextDeltaNotification
        | ReasoningSummaryTextDeltaNotification,
    ):
        work.model_active()
    elif isinstance(payload, TurnCompletedNotification):
        work.engine_done()


class ApprovalLedger:
    """What the approval handler (on `CodexClient`'s reader thread) and the
    turn's stream (worker thread, then the event loop) share about items.

    - Which items had an approval request: `_consume` observes only a
      completed command or file change that had none (05 §2.1).
    - The changes of each file-change item, from its `item/started`:
      app-server 0.147.0's `FileChangeRequestApprovalParams` carries only
      ids, a reason and `grant_root` (app-server-protocol
      `v2/item.rs:1521-1535`), while core emits the item's start, with its
      changes, before it asks (core `tools/handlers/apply_patch.rs:553`, then
      the orchestrator). The request is read after that start notification,
      but the worker thread may not have pulled the start yet, so
      `started_changes` waits for it up to `wait_s`.

    Thread-safe: every method takes the one lock.
    """

    def __init__(self, wait_s: float = 2.0) -> None:
        self._wait_s = wait_s
        self._lock = threading.Condition()
        self._requested: set[str] = set()
        self._changes: dict[str, list[FileUpdateChange]] = {}

    def record_started(self, item: ThreadItem) -> None:
        root = item.root
        if isinstance(root, FileChangeThreadItem):
            with self._lock:
                self._changes[root.id] = list(root.changes)
                self._lock.notify_all()

    def mark_requested(self, item_id: object) -> None:
        if isinstance(item_id, str):
            with self._lock:
                self._requested.add(item_id)

    def was_requested(self, item_id: str) -> bool:
        with self._lock:
            return item_id in self._requested

    def started_changes(self, item_id: object) -> list[FileUpdateChange] | None:
        """The item's changes, waiting up to `wait_s` for its start; `None`
        when it does not arrive in time."""
        if not isinstance(item_id, str):
            return None
        with self._lock:
            self._lock.wait_for(lambda: item_id in self._changes, timeout=self._wait_s)
            return self._changes.get(item_id)


def _gate_sync(
    policy: PolicyGate,
    loop: asyncio.AbstractEventLoop,
    action: Action,
    broker_name: str,
    broker_args: dict[str, object],
    *,
    pass_through: bool = False,
) -> GateDecision:
    """Bridge the sync `approval_handler` callback (running on `CodexClient`'s
    own reader thread) into `policy.gate()` (an `async def`, which may ask the
    broker) via `run_coroutine_threadsafe` against the loop captured before
    the client started (spike §1) -- blocks this (non-loop) thread until the
    gate decides, exactly mirroring `PermissionBroker.decide`'s own "ask"
    semantics contract."""
    future = asyncio.run_coroutine_threadsafe(
        policy.gate(
            action, broker_name=broker_name, broker_args=broker_args, pass_through=pass_through
        ),
        loop,
    )
    return future.result()


def _queue_refusal(
    decision: GateDecision,
    out_queue: queue.Queue[object],
    tool_name: str,
    tool_input: dict[str, object],
) -> None:
    """A deny or stop: `PermissionRequested` (FR-4.1, also on stop, as the
    Claude adapter does), then the stop or the FR-4.4 terminate request."""
    out_queue.put(
        PermissionRequested(
            tool_name=tool_name,
            tool_input=cast("dict[str, Any]", tool_input),
            verdict="deny",
            # FR-4.4: codex's approval protocol has NO reason channel
            # (supports_deny_reason=False) -- recorded here and in the
            # mirror; the model sees only the engine's own denial.
            reason=decision.reason,
        )
    )
    if decision.outcome == "stop":
        out_queue.put(
            _StopRequested(reason=decision.reason or f"turn stopped by policy at {tool_name!r}")
        )
    elif decision.terminate:
        out_queue.put(_TerminateRequested())


def _answer_exec_or_patch(
    decision: GateDecision,
    out_queue: queue.Queue[object],
    tool_name: str,
    tool_input: dict[str, object],
) -> JsonObject:
    """05 §2.6: allow -> the one-time `accept`; deny and stop -> `reject`
    (verified live to prevent the action, spike §3), a stop then interrupts
    the turn. Never `acceptWithExecpolicyAmendment` (it persists)."""
    if decision.outcome == "allow":
        return {"decision": "accept"}
    _queue_refusal(decision, out_queue, tool_name, tool_input)
    return {"decision": "reject"}


def _decide_mcp_elicitation(
    policy: PolicyGate,
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
    bare_name = _extract_tool_name(params)
    if server_name == _SHIM_SERVER_NAME:
        # Every tradewind-tool call Codex makes goes through this branch: a
        # structural pass-through (05 §2.3). `ToolHost.call()` gates it once
        # as a `caller_tool` (`turn_runner.py` hands this backend's
        # `ToolHost` the gate as its broker) -- gating here too would ask an
        # "ask"-semantics broker the same question twice.
        name = bare_name or _UNKNOWN_TOOL_NAME
        _gate_sync(
            policy,
            loop,
            caller_tool_action(name, tool_input, backend="codex"),
            name,
            tool_input,
            pass_through=True,
        )
        return {"action": "accept", "content": {}}
    # Fail-closed (controller ruling, kept): an unreadable tool name is a
    # missing critical field, which the floor denies without asking.
    broker_name = bare_name if bare_name is not None else _UNKNOWN_TOOL_NAME
    decision = _gate_sync(
        policy, loop, mcp_action(server_name, bare_name, tool_input), broker_name, tool_input
    )
    if decision.outcome == "allow":
        return {"action": "accept", "content": {}}
    _queue_refusal(decision, out_queue, broker_name, tool_input)
    return {"action": "decline", "content": {}}


def make_approval_handler(
    policy: PolicyGate,
    loop: asyncio.AbstractEventLoop,
    out_queue: queue.Queue[object],
    ledger: ApprovalLedger,
    workspace: str | None = None,
) -> ApprovalHandler:
    """Build the sync `CodexClient(approval_handler=...)` callback (module
    docstring's approval-routing section): every request goes through
    `policy.gate` (05 §2.1); the broker, when the rules say `ask`, gets
    today's `"shell"` / `"apply_patch"` names and arguments (05 §1.1)."""

    def handler(method: str, params: JsonObject | None) -> JsonObject:
        params_dict: JsonObject = params or {}
        # 05 §2.6: every request's full option list is logged.
        _logger.debug(
            "codex approval request",
            extra={"method": method, "available_decisions": params_dict.get("availableDecisions")},
        )
        if method == "item/commandExecution/requestApproval":
            ledger.mark_requested(params_dict.get("itemId"))
            tool_input: dict[str, object] = {"command": params_dict.get("command")}
            decision = _gate_sync(policy, loop, shell_action(params_dict), "shell", tool_input)
            return _answer_exec_or_patch(decision, out_queue, "shell", tool_input)
        if method == "item/fileChange/requestApproval":
            item_id = params_dict.get("itemId")
            ledger.mark_requested(item_id)
            tool_input = {"changes": params_dict.get("changes")}
            action = file_write_action(
                ledger.started_changes(item_id), workspace=workspace, native_args=dict(params_dict)
            )
            decision = _gate_sync(policy, loop, action, "apply_patch", tool_input)
            return _answer_exec_or_patch(decision, out_queue, "apply_patch", tool_input)
        if method == "mcpServer/elicitation/request":
            return _decide_mcp_elicitation(policy, loop, out_queue, params_dict)
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


def _thread_settings(
    backend_options: dict[str, Any], *, allow_all_broker: bool, engine_auto: bool
) -> tuple[SandboxMode, ApprovalsReviewer]:
    """The thread's sandbox and approvals reviewer (05 §2.5).

    `engine_auto`: Codex's own auto-reviewer -- `ApprovalsReviewer.
    auto_review`, the value the SDK's own `ApprovalMode.auto_review` maps to
    (`openai_codex/_approval_mode.py`, `_approval_mode_settings`) -- with the
    workspace-write sandbox, also when no broker is set; requests that still
    reach this client go through the gate. Otherwise `ApprovalsReviewer.user`
    (every request reaches this adapter) and the sandbox defaults of
    `_sandbox_mode_from_options`, unchanged. An explicit
    `backend_options["sandbox"]` always wins."""
    if engine_auto:
        return (
            SandboxMode(backend_options.get("sandbox", _DEFAULT_SANDBOX.value)),
            ApprovalsReviewer.auto_review,
        )
    return (
        _sandbox_mode_from_options(backend_options, allow_all_broker=allow_all_broker),
        ApprovalsReviewer.user,
    )


def _approval_policy_value_from_options(backend_options: dict[str, Any]) -> AskForApprovalValue:
    value = backend_options.get("approval_policy", _DEFAULT_APPROVAL_POLICY_VALUE.value)
    return AskForApprovalValue(value)


# Conservative native-loss classification for `thread_resume` failures
# (FR-6.1, fail-closed: anything unmatched stays TurnFailed).
_NATIVE_LOST_MARKERS = (
    "not found",
    "no such thread",
    "does not exist",
    "expired",
    "unknown thread",
)


def _is_native_lost_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _NATIVE_LOST_MARKERS)


@dataclass
class _ReplayPromptRequest:
    """Queue payload from the worker: `thread_resume` failed with a
    classified native-loss error (FR-6.1). `_consume` (on the event loop)
    lazily builds the rendered replay prompt, emits the `resume_degraded`
    item, and answers on `reply`; the worker then starts a FRESH thread
    with it -- one worker, no relaunch, zero mirror reads on healthy
    turns."""

    reason: str
    reply: queue.Queue[str]


@dataclass
class _TerminateRequested:
    """Queue payload: a broker `Denial(terminate=True)` was delivered on
    this backend (FR-4.4). Codex has no in-protocol terminate, so `_consume`
    approximates by interrupting the turn handle; the interrupted turn is
    then reclassified `broker_terminated`."""


@dataclass
class _StopRequested:
    """Queue payload: the policy gate's `stop` for a request (05 §2.6). The
    action was rejected; `_consume` interrupts the turn and ends it with
    `TurnFailed(reason)` -- never reported as a caller interrupt."""

    reason: str


@dataclass
class _ConnectRetryNotice:
    """Queue payload from `_drive_turn`'s worker thread: one scheduled
    pre-turn connect retry (FR-6.6), mapped by `_consume` to the mirrored
    `retry_notice` event item."""

    attempt: int
    max_attempts: int
    delay_s: float
    error: str


def _drive_turn(
    make_client: Callable[[], CodexClient],
    session: SessionRow,
    prompt: str,
    model: str,
    effort: ReasoningEffort | None,
    output_schema: JsonObject | None,
    system_prompt: str | None,
    sandbox: SandboxMode,
    approval_policy_value: AskForApprovalValue,
    approvals_reviewer: ApprovalsReviewer,
    out_queue: queue.Queue[object],
    record_native_id: Callable[[str], None],
    record_handle: Callable[[TurnHandle], None],
    retry_max: int,
    retry_delay_s: float,
    ledger: ApprovalLedger,
) -> None:
    """Runs entirely on its own `threading.Thread` (module docstring): starts
    `client`, opens/resumes the thread, starts the turn, and pumps every
    notification from `TurnHandle.stream()` onto `out_queue` until the
    stream ends (clean finish or exception) -- `_consume` (on the event
    loop) is the other half of this bridge."""
    client = make_client()
    try:
        # FR-6.6 pre-turn retry: start/initialize failures are provably
        # side-effect free (the engine subprocess never took a turn). A
        # fresh client is built per attempt -- a failed start leaves the
        # old one unusable.
        connect_retries = 0
        while True:
            try:
                client.start()
                client.initialize()
                break
            except Exception as exc:
                if connect_retries >= retry_max:
                    raise
                connect_retries += 1
                delay = retry_delay_s * 2 ** (connect_retries - 1)
                out_queue.put(
                    _ConnectRetryNotice(
                        attempt=connect_retries,
                        max_attempts=retry_max,
                        delay_s=delay,
                        error=str(exc),
                    )
                )
                with contextlib.suppress(Exception):
                    # a client that failed to start may have nothing to close
                    client.close()
                time.sleep(delay)
                client = make_client()
        approval_policy = AskForApproval(root=approval_policy_value)
        replay_prompt: str | None = None
        if session.native_session_id is not None:
            try:
                resumed = client.thread_resume(
                    session.native_session_id,
                    ThreadResumeParams(
                        thread_id=session.native_session_id,
                        cwd=session.cwd,
                        model=model,
                        sandbox=sandbox,
                        approval_policy=approval_policy,
                        approvals_reviewer=approvals_reviewer,
                        base_instructions=system_prompt,
                    ),
                )
                thread_id = resumed.thread.id
            except Exception as exc:
                if not _is_native_lost_error(exc):
                    raise  # ambiguous: fail closed (FR-6.1 honesty rule)
                reply: queue.Queue[str] = queue.Queue()
                out_queue.put(_ReplayPromptRequest(reason=str(exc), reply=reply))
                replay_prompt = reply.get(timeout=60)
                started = client.thread_start(
                    ThreadStartParams(
                        cwd=session.cwd,
                        model=model,
                        sandbox=sandbox,
                        approval_policy=approval_policy,
                        approvals_reviewer=approvals_reviewer,
                        base_instructions=system_prompt,
                    )
                )
                thread_id = started.thread.id
        else:
            started = client.thread_start(
                ThreadStartParams(
                    cwd=session.cwd,
                    model=model,
                    sandbox=sandbox,
                    approval_policy=approval_policy,
                    approvals_reviewer=approvals_reviewer,
                    base_instructions=system_prompt,
                )
            )
            thread_id = started.thread.id
        record_native_id(thread_id)

        effective_prompt = replay_prompt if replay_prompt is not None else prompt
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
            effective_prompt,
            params=TurnStartParams(
                thread_id=thread_id,
                # Required by `TurnStartParams` (no default) but never
                # actually sent -- see the comment above. Built from the
                # same `prompt` purely to satisfy pydantic validation, not
                # because its value matters.
                input=[UserInput(TextUserInput(type="text", text=effective_prompt))],
                model=model,
                effort=effort,
                output_schema=output_schema,
            ),
        )
        handle = TurnHandle(client, thread_id, turn_started.turn.id)
        record_handle(handle)
        for notification in handle.stream():
            if isinstance(notification.payload, ItemStartedNotification):
                # Before the queue: the approval handler may be waiting for
                # this item's changes (`ApprovalLedger`).
                ledger.record_started(notification.payload.item)
            out_queue.put(notification)
    except BaseException as exc:  # forwarded across the thread boundary, never swallowed
        out_queue.put(exc)
    finally:
        client.close()
        out_queue.put(_DONE)


def _engine_pid(client: CodexClient) -> int | None:
    """The app-server process's PID, for the process report (05 §3.4). The
    SDK keeps it private (`CodexClient._proc`, pinned by a unit test); when
    it is missing, one WARNING and no report."""
    pid = getattr(getattr(client, "_proc", None), "pid", None)
    if not isinstance(pid, int):
        _logger.warning("process report unavailable: codex SDK has no _proc.pid")
        return None
    return pid


class _EngineReportingClient(CodexClient):
    """A turn's `CodexClient` that reports its app-server process to the
    runner (05 §3.4): the PID once started, and a snapshot request just
    before it is closed. Both run on `_drive_turn`'s worker thread, where
    blocking on `ps` is fine."""

    def __init__(
        self,
        config: CodexConfig | None = None,
        approval_handler: ApprovalHandler | None = None,
        *,
        engine_started: Callable[[int], None],
        engine_closing: Callable[[], None],
    ) -> None:
        super().__init__(config=config, approval_handler=approval_handler)
        self._engine_started = engine_started
        self._engine_closing = engine_closing

    def start(self) -> None:
        super().start()
        pid = _engine_pid(self)
        if pid is not None:
            self._engine_started(pid)

    def close(self) -> None:
        self._engine_closing()
        super().close()


async def _interrupt_for_stop(
    request_interrupt: Callable[[], Awaitable[None]] | None, turn_id: str
) -> None:
    """Interrupt the turn for a policy stop (05 §2.6). A failed interrupt is
    logged, not raised, so the stop reason still ends the turn as
    `TurnFailed(reason)` (the engine may already have finished the turn)."""
    if request_interrupt is None:
        return
    try:
        await request_interrupt()
    except Exception:
        _logger.warning(
            "interrupt for a policy stop failed", exc_info=True, extra={"turn_id": turn_id}
        )


def _direct_policy(ctx: TurnContext, out_queue: queue.Queue[object]) -> PolicyGate:
    """The gate for a `TurnContext` built without the runner (`ctx.policy`
    unset -- direct construction, as in unit tests): broker mode with no
    caller or default rules, so every request asks `ctx.broker`, as before
    turn governance (as `ClaudeBackend`'s `_direct_policy`). Its record
    notices go through the turn's queue. The runner always sets
    `ctx.policy`."""
    return PolicyGate(
        broker=ctx.broker,
        backend="codex",
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
        emit=out_queue.put,
    )


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

    def customization_patterns(self) -> list[CustomizationPattern]:
        # 06 §1, Step 0 (static, codex 0.147.0 binary): `strings` shows
        # "Ignored unsupported project-local config keys in ... manually set
        # them in your user-level config.toml" and "Trusting the directory
        # allows project-local config, hooks, and exec policies to load".
        # The list of honored keys is not recoverable from the binary, so a
        # dangerous key (sandbox_mode, approval_policy, notify, hooks) cannot
        # be ruled out. Controller ruling (fix round 1): fail closed, flag it.
        # Exec policies: the same trust line names them; they are `.rules`
        # files (e.g. `default.rules`) in a `rules` folder of each config
        # layer. An allow rule can let an escalated command run with no
        # approval request. Controller ruling (final review): flag the
        # folder, fail closed.
        return [
            CustomizationPattern(".codex/config.toml", "codex-config", False),
            CustomizationPattern(".codex/rules", "codex-exec-policy", True),
        ]

    def auth_env_vars(self) -> frozenset[str]:
        # 06 §4. Explicit names only.
        return frozenset({"OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"})

    def engine_version(self) -> str | None:
        # 06 §5, plan ruling: the bundled app-server package's version (the
        # same value as `serverInfo.version`), known before spawn so the
        # runner can log it at turn start. Read per call, no cache.
        return importlib.metadata.version("openai-codex-cli-bin")

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
            # Same reasoning as claude (FR-6.6): engine-owned turn, no
            # honest mid-turn retry; pre-turn spawn retry only.
            supports_turn_retry=False,
            # FR-4.4: verified -- the approval protocol has no reason channel.
            supports_deny_reason=False,
            # 05 §3/§7: item, delta and turn signals reach `ctx.work`, so the
            # inactivity limits apply and there is no default hard cap.
            supports_work_supervisor=True,
            # 05 §2.5: Codex's own auto-reviewer; approval requests that
            # still reach the client, and the shim, go through the gate.
            supports_engine_auto=True,
            supports_action_policy=True,
            web_search="observed",
            fetch_url="gated",
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

    def _engine_config(self, config_overrides: tuple[str, ...] = ()) -> CodexConfig:
        """The `codex app-server` launch config, for turns and transcript
        reads alike: the `CODEX_HOME` isolation (DR-3), plus "" for each
        auth variable a subscription profile must not pass on (06 §4; the
        SDK cannot remove one)."""
        scrub = engine_env(self.profile.auth, self.auth_env_vars(), os.environ).overrides
        env = {**(_codex_env(self.native_config) or {}), **scrub}
        return CodexConfig(config_overrides=config_overrides, env=env or None)

    def _read_thread_sync(self, native_session_id: str) -> ThreadReadResponse:
        client = CodexClient(config=self._engine_config())
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
        policy = ctx.policy if ctx.policy is not None else _direct_policy(ctx, out_queue)
        ledger = ApprovalLedger()
        workspace = ctx.session.cwd
        approval_handler = make_approval_handler(policy, loop, out_queue, ledger, workspace)
        try:
            shim_def = await ctx.tools.shim_server_def()
            config = self._engine_config(
                mcp_server_config_overrides(shim_def) + web_search_overrides(ctx.web)
            )

            def make_client() -> CodexClient:
                return _EngineReportingClient(
                    config=config,
                    approval_handler=approval_handler,
                    engine_started=ctx.engine_started,
                    engine_closing=ctx.engine_closing,
                )

            backend_options = self.profile.backend_options
            # Duck-typed marker check (module docstring, item 3a) -- see
            # `turn_runner._AllowAllBroker.is_default_allow_all`'s own
            # comment for why this is `getattr`, not an `isinstance` check
            # against a class this adapter never imports.
            allow_all_broker = cast(bool, getattr(ctx.broker, "is_default_allow_all", False))
            sandbox, approvals_reviewer = _thread_settings(
                backend_options,
                allow_all_broker=allow_all_broker,
                engine_auto=ctx.session.options_snapshot.get("permission_mode") == "engine_auto",
            )
            approval_policy_value = _approval_policy_value_from_options(backend_options)
            effort = _reasoning_effort(ctx.model_spec.effort)

            handle_ref: list[TurnHandle] = []

            def record_handle(handle: TurnHandle) -> None:
                handle_ref.append(handle)
                self._turn_handles[session_id] = handle

            def record_native_id(native_thread_id: str) -> None:
                self._native_ids[session_id] = native_thread_id

            worker_prompt = ctx.prompt
            if ctx.force_replay and ctx.load_replay_history is not None:
                # FR-6.1 planned REPLAY (cross-backend continuation): the
                # runner suppressed the stale id; open a fresh thread with
                # the rendered mirror preamble.
                replay_rows = await ctx.load_replay_history()
                budget = ctx.compaction.keep_recent_tokens if ctx.compaction is not None else 20000
                worker_prompt = compose_replay_prompt(replay_rows, budget, ctx.prompt)
                yield ItemCompleted(
                    message=resume_degraded_notice(
                        reason="cross-backend continuation onto codex; rendered mirror "
                        "transcript injected into a fresh thread"
                    )
                )
            worker = threading.Thread(
                target=_drive_turn,
                args=(
                    make_client,
                    ctx.session,
                    worker_prompt,
                    ctx.model_spec.model,
                    effort,
                    ctx.output_schema,
                    ctx.system_prompt,
                    sandbox,
                    approval_policy_value,
                    approvals_reviewer,
                    out_queue,
                    record_native_id,
                    record_handle,
                    ctx.retry.max_attempts if ctx.retry is not None else 0,
                    ctx.retry.base_delay_s if ctx.retry is not None else 0.0,
                    ledger,
                ),
                daemon=True,
            )
            worker.start()
            terminal_event_observed = False
            try:

                async def _request_interrupt() -> None:
                    if handle_ref:
                        await anyio.to_thread.run_sync(handle_ref[0].interrupt)

                async def _build_replay_prompt() -> str:
                    rows = (
                        await ctx.load_replay_history()
                        if ctx.load_replay_history is not None
                        else []
                    )
                    budget = (
                        ctx.compaction.keep_recent_tokens if ctx.compaction is not None else 20000
                    )
                    return compose_replay_prompt(rows, budget, ctx.prompt)

                async for event in self._consume(
                    ctx.turn_id,
                    out_queue,
                    ctx.model_spec,
                    _request_interrupt,
                    _build_replay_prompt,
                    has_output_schema=ctx.output_schema is not None,
                    work=ctx.work,
                    policy=policy,
                    ledger=ledger,
                    workspace=workspace,
                ):
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

    @staticmethod
    def _observe(
        item: ThreadItem,
        policy: PolicyGate | None,
        ledger: ApprovalLedger | None,
        workspace: str | None,
    ) -> str | None:
        """Observe a completed command, file change or web item that had no
        approval request (05 §2.1, §7, 07 §4): `item/started` comes before a possible
        request, so only the completion is observed, and a requested item is
        decided at the gate only. Returns the stop reason, or `None`."""
        if policy is None or ledger is None:
            return None
        actions = observed_actions(item, workspace=workspace)
        if not actions or ledger.was_requested(item.root.id):
            return None
        for action in actions:
            observation = policy.observe(action)
            if observation.outcome != "allow":
                stopped = f"turn stopped by policy after {action.kind} {action.summary!r}"
                return f"{stopped}: {observation.reason}" if observation.reason else stopped
        return None

    async def _consume(
        self,
        turn_id: str,
        out_queue: queue.Queue[object],
        model_spec: ModelSpec,
        request_interrupt: Callable[[], Awaitable[None]] | None = None,
        build_replay_prompt: Callable[[], Awaitable[str]] | None = None,
        *,
        has_output_schema: bool = False,
        work: WorkSupervisor | None = None,
        policy: PolicyGate | None = None,
        ledger: ApprovalLedger | None = None,
        workspace: str | None = None,
    ) -> AsyncIterator[Event]:
        """Drain `out_queue` into events. With `work`, every notification also
        feeds the work supervisor (`report_work`, never an event); with
        `policy` and `ledger`, a completed command or file change that had no
        approval request is observed (05 §2.1), and an observed `stop` -- or
        a gate `stop` (`_StopRequested`) -- interrupts the turn and ends it
        with `TurnFailed(reason)`. No settle wait (05 §3.3): every Codex item
        is `tool` work that ends inside the engine's turn, so no background
        or subagent work outlives `turn/completed`."""
        agent_messages: list[AgentMessageThreadItem] = []
        usage: ThreadTokenUsage | None = None
        broker_terminated = False
        stop_reason: str | None = None
        while True:
            item = await anyio.to_thread.run_sync(out_queue.get)
            if item is _DONE:
                return
            if isinstance(item, PermissionRequested | ItemCompleted):
                # `ItemCompleted` here: a record notice of a gate built
                # without the runner (`_direct_policy`).
                yield item
                continue
            if isinstance(item, _StopRequested):
                if stop_reason is None:
                    stop_reason = item.reason
                    await _interrupt_for_stop(request_interrupt, turn_id)
                continue
            if isinstance(item, _ReplayPromptRequest):
                # FR-6.1 reactive degrade: the worker classified the
                # resume failure as native loss and is blocked on `reply`.
                if build_replay_prompt is None:
                    item.reply.put("")  # unreachable in practice; unblock the worker
                    continue
                yield ItemCompleted(
                    message=resume_degraded_notice(
                        reason=f"codex thread unavailable ({item.reason}); rendered mirror "
                        "transcript injected into a fresh thread"
                    )
                )
                item.reply.put(await build_replay_prompt())
                continue
            if isinstance(item, _TerminateRequested):
                # FR-4.4 approximate terminate: reject was already sent;
                # interrupting the handle ends the engine's turn.
                broker_terminated = True
                if request_interrupt is not None:
                    await request_interrupt()
                continue
            if isinstance(item, _ConnectRetryNotice):
                yield ItemCompleted(
                    message=retry_notice(
                        phase="connect",
                        attempt=item.attempt,
                        max_attempts=item.max_attempts,
                        delay_s=item.delay_s,
                        error=item.error,
                    )
                )
                continue
            if isinstance(item, BaseException):
                yield TurnFailed(turn_id=turn_id, error=str(item))
                return
            notification = cast(Notification, item)
            payload = notification.payload
            if work is not None:
                report_work(work, payload)
            if isinstance(payload, ItemCompletedNotification):
                agent = _as_agent_message(payload.item)
                if agent is not None:
                    agent_messages.append(agent)
                permission_event = _denied_shim_permission_event(payload.item)
                if permission_event is not None:
                    yield permission_event
                for message in thread_item_to_messages(payload.item):
                    yield ItemCompleted(message=message)
                observed_stop = self._observe(payload.item, policy, ledger, workspace)
                if observed_stop is not None and stop_reason is None:
                    stop_reason = observed_stop
                    await _interrupt_for_stop(request_interrupt, turn_id)
            elif isinstance(payload, ContextCompactedNotification):
                # The engine compacted its OWN context (FR-5.8
                # observability). Codex encrypts the summary
                # (`CompactionResponseItem.encrypted_content`), so the
                # record carries no text -- only the native turn id, on the
                # field that exists for exactly that, so the compaction can
                # be cross-referenced against the native transcript.
                yield ItemCompleted(message=engine_compaction_record(native_id=payload.turn_id))
            elif isinstance(payload, ThreadTokenUsageUpdatedNotification):
                usage = payload.token_usage
            elif isinstance(payload, TurnCompletedNotification):
                turn = payload.turn
                if stop_reason is not None:
                    # 05 §2.6: OUR policy stop -- a failed turn naming the
                    # reason, never a caller interrupt (even when the engine
                    # finished before the interrupt reached it).
                    yield TurnFailed(turn_id=turn_id, error=stop_reason)
                    return
                if turn.status == TurnStatus.interrupted:
                    if broker_terminated:
                        # FR-4.4: this interrupt was OUR terminate
                        # approximation -- honest broker_terminated
                        # completion, batch results already delivered.
                        yield TurnCompleted(
                            result=TurnResult(
                                turn_id=turn_id,
                                status="completed",
                                end_reason="broker_terminated",
                                final_text=_final_text_from_items(agent_messages),
                                usage=_usage_dict(usage) if usage is not None else {},
                                cost_usd=_computed_cost(
                                    model_spec,
                                    _usage_dict(usage) if usage is not None else {},
                                ),
                            )
                        )
                        return
                    # `Backend.run`'s contract (ports.py): an interrupted
                    # turn ends with neither `TurnCompleted` nor
                    # `TurnFailed` -- the turn runner assigns `interrupted`
                    # itself once this generator ends with no terminal
                    # event (same as `ClaudeBackend._drive_client`'s
                    # `is_aborted_result` handling).
                    return
                if turn.status == TurnStatus.completed:
                    final_text = _final_text_from_items(agent_messages)
                    structured_output: dict[str, object] | None = None
                    if has_output_schema:
                        # FR-1.3: `final_text` stays the engine's final
                        # answer as before; the parsed result is ADDED. No
                        # valid result leaves it None -- still a completion.
                        structured_output = _structured_output_from_text(final_text)
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
                            final_text=final_text,
                            usage=_usage_dict(usage) if usage is not None else {},
                            # Codex reports no per-turn dollar figure, so
                            # this is COMPUTED from the tier's cost table
                            # when one exists (FR-10.5; API-equivalent
                            # price on subscription auth), else honest None.
                            cost_usd=_computed_cost(
                                model_spec, _usage_dict(usage) if usage is not None else {}
                            ),
                            structured_output=structured_output,
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
