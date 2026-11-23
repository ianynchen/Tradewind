"""Tests for `CodexBackend` instance behaviour that isn't a pure mapping
function: approval_handler routing (task-14 brief/controller ruling --
shim auto-accept scoping, fail-closed unknown extraction), the sync-reader-
thread-to-async-broker bridge against a REAL event loop, `interrupt()`,
`take_native_session_id()`, and `_codex_env`'s isolation-mode gate. Pure
event mapping lives in `tests/unit/test_codex_mapping.py`; a real live
session is `tests/conformance/test_codex.py` (`@pytest.mark.integration`).
"""

from __future__ import annotations

import asyncio
import queue
import threading
from contextlib import aclosing
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from openai_codex.generated.v2_all import ContextCompactedNotification, SandboxMode

import tradewind.adapters.codex_backend as codex_backend
from tradewind.adapters.codex_backend import (
    ApprovalLedger,
    CodexBackend,
    _codex_env,
    _sandbox_mode_from_options,
    make_approval_handler,
)
from tradewind.application.config import NativeStoreConfig
from tradewind.application.ports import TurnContext
from tradewind.application.tool_host import ToolHost
from tradewind.domain.errors import ConfigError, Unsupported
from tradewind.domain.events import ItemCompleted, PermissionRequested, TurnStarted
from tradewind.domain.models import (
    ModelSpec,
    Profile,
    SessionRow,
    StoredMessage,
    SubscriptionAuth,
    Verdict,
)


def _profile() -> Profile:
    return Profile(
        backend="codex",
        auth=SubscriptionAuth(),
        models={"standard": ModelSpec(model="gpt-5.4-mini")},
    )


class _ScriptedBroker:
    """A `PermissionBroker` that returns a fixed verdict and records every
    call it received, so tests can assert both the outcome and whether the
    broker was consulted at all."""

    def __init__(self, verdict: Verdict) -> None:
        self.verdict = verdict
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # The normalized action the gate adds under `tradewind` (05 §1.1),
        # kept apart so `calls` shows today's name and arguments exactly.
        self.actions: list[dict[str, Any]] = []

    async def decide(self, tool_name: str, tool_input: dict[str, Any]) -> Verdict:
        args = dict(tool_input)
        self.actions.append(args.pop("tradewind"))
        self.calls.append((tool_name, args))
        return self.verdict


class _RaisingBroker:
    """A `PermissionBroker` that fails the test if consulted at all -- used
    to prove the fail-closed extraction-failure path never asks it."""

    async def decide(self, tool_name: str, tool_input: dict[str, Any]) -> Verdict:
        raise AssertionError(
            f"broker.decide() must not be called, got ({tool_name!r}, {tool_input!r})"
        )


def _gate(
    broker: Any,
    *,
    caller: list[Any] | None = None,
    mode: str = "broker",
    emitted: list[object] | None = None,
) -> Any:
    """A `PolicyGate` the way `turn_runner` builds one for codex."""
    from tradewind.application.policy_gate import PolicyGate
    from tradewind.domain.policy import TurnVars, floor_tradewind

    return PolicyGate(
        broker=broker,
        backend="codex",
        floor=floor_tradewind(),
        caller=caller or [],
        defaults=[],
        turn=TurnVars(
            mode=cast(Any, mode),
            engine_mode=None,
            shim_server=None,
            workspace="/w",
            result_tool=None,
        ),
        has_broker=not getattr(broker, "is_default_allow_all", False),
        emit=(emitted if emitted is not None else []).append,
    )


def _rule(kind: str, outcome: str, reason: str = "", **where: Any) -> Any:
    from tradewind.domain.policy import Matcher, Rule

    return Rule(
        id=f"caller.{kind}.{outcome}",
        kind=cast(Any, kind),
        where={name: Matcher("equals", value) for name, value in where.items()},
        outcome=cast(Any, outcome),
        reason=reason,
    )


def _run_handler_on_a_real_loop(
    broker: Any,
    build_and_call: Any,
    *,
    policy: Any = None,
    ledger: Any = None,
) -> tuple[dict[str, object], list[object]]:
    """Spins up a real asyncio event loop on a background thread (mirroring
    exactly how `CodexBackend._run_turn` captures `asyncio.get_running_loop()`
    before `CodexClient.start()`), builds the approval_handler against it,
    then invokes `build_and_call(handler, out_queue)` from a THIRD thread --
    standing in for `CodexClient`'s own reader thread, distinct from both the
    event loop thread and the calling test thread -- and blocks for the
    result. This is the "thread-bridge unit test with a real event loop"
    (task-14 brief): proves `asyncio.run_coroutine_threadsafe(...).result()`
    actually blocks a non-loop thread until `broker.decide()` (running on the
    loop) resolves, rather than merely asserting on mocked pieces.
    """
    loop_ready = threading.Event()
    loop_box: list[asyncio.AbstractEventLoop] = []
    result_box: dict[str, object] = {}
    queue_box: list[queue.Queue[object]] = []

    def loop_thread() -> None:
        loop = asyncio.new_event_loop()
        loop_box.append(loop)
        asyncio.set_event_loop(loop)
        loop_ready.set()
        loop.run_forever()
        loop.close()

    thread = threading.Thread(target=loop_thread, daemon=True)
    thread.start()
    loop_ready.wait(timeout=5)
    loop = loop_box[0]

    out_queue: queue.Queue[object] = queue.Queue()
    queue_box.append(out_queue)
    handler = make_approval_handler(
        policy if policy is not None else _gate(broker),
        loop,
        out_queue,
        ledger if ledger is not None else ApprovalLedger(wait_s=0.0),
        "/w",
    )

    caller_done = threading.Event()

    def caller_thread() -> None:
        try:
            result_box["value"] = build_and_call(handler)
        except BaseException as exc:  # surfaced to the test thread below
            result_box["error"] = exc
        finally:
            caller_done.set()

    caller = threading.Thread(target=caller_thread, daemon=True)
    caller.start()
    assert caller_done.wait(timeout=5), "approval_handler call did not return -- bridge deadlocked"

    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)

    if "error" in result_box:
        raise result_box["error"]  # type: ignore[misc]

    drained: list[object] = []
    while not out_queue.empty():
        drained.append(out_queue.get_nowait())
    return result_box, drained


# --- thread-bridge: real event loop, real cross-thread blocking ---------


def test_exec_approval_allow_bridges_to_broker_decide_on_a_real_loop() -> None:
    broker = _ScriptedBroker("allow")

    result, events = _run_handler_on_a_real_loop(
        broker,
        lambda handler: handler("item/commandExecution/requestApproval", {"command": "echo hi"}),
    )

    assert result["value"] == {"decision": "accept"}
    assert broker.calls == [("shell", {"command": "echo hi"})]
    assert events == []


def test_exec_approval_deny_bridges_and_emits_permission_requested() -> None:
    broker = _ScriptedBroker("deny")

    result, events = _run_handler_on_a_real_loop(
        broker,
        lambda handler: handler("item/commandExecution/requestApproval", {"command": "rm -rf /"}),
    )

    assert result["value"] == {"decision": "reject"}
    assert len(events) == 1
    assert isinstance(events[0], PermissionRequested)
    assert events[0].tool_name == "shell"
    assert events[0].tool_input == {"command": "rm -rf /"}
    assert events[0].verdict == "deny"


def test_file_change_approval_maps_changes_into_tool_input() -> None:
    # The broker keeps today's `{"changes": params["changes"]}`; the gate's
    # action takes the changes from the item's `item/started` (the request
    # itself carries none on app-server 0.147.0 -- `ApprovalLedger`).
    broker = _ScriptedBroker("allow")
    ledger = ApprovalLedger(wait_s=0.0)
    ledger.record_started(_file_item("patch-1"))

    result, _events = _run_handler_on_a_real_loop(
        broker,
        lambda handler: handler(
            "item/fileChange/requestApproval",
            {"itemId": "patch-1", "changes": [{"path": "a.py"}]},
        ),
        ledger=ledger,
    )

    assert result["value"] == {"decision": "accept"}
    assert broker.calls == [("apply_patch", {"changes": [{"path": "a.py"}]})]


# --- MCP elicitation routing ---------------------------------------------


def test_shim_server_mcp_tool_call_auto_accepts_without_consulting_broker() -> None:
    # ToolHost.call() already gates authoritatively for tradewind's own
    # shim server (module docstring) -- the broker must NOT be asked here.
    broker = _RaisingBroker()

    result, events = _run_handler_on_a_real_loop(
        broker,
        lambda handler: handler(
            "mcpServer/elicitation/request",
            {
                "serverName": "toolproxy",
                "message": 'Allow the toolproxy MCP server to run tool "my_tool"?',
                "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_params": {"x": 1}},
            },
        ),
    )

    assert result["value"] == {"action": "accept", "content": {}}
    assert events == []


def test_non_shim_server_mcp_tool_call_extracts_name_and_consults_broker() -> None:
    broker = _ScriptedBroker("allow")

    result, events = _run_handler_on_a_real_loop(
        broker,
        lambda handler: handler(
            "mcpServer/elicitation/request",
            {
                "serverName": "echo",
                "message": 'Allow the echo MCP server to run tool "echo"?',
                "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_params": {"text": "hi"}},
            },
        ),
    )

    assert result["value"] == {"action": "accept", "content": {}}
    assert broker.calls == [("echo", {"text": "hi"})]
    assert events == []


def test_non_shim_server_mcp_tool_call_deny_emits_permission_requested() -> None:
    broker = _ScriptedBroker("deny")

    result, events = _run_handler_on_a_real_loop(
        broker,
        lambda handler: handler(
            "mcpServer/elicitation/request",
            {
                "serverName": "echo",
                "message": 'Allow the echo MCP server to run tool "echo"?',
                "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_params": {}},
            },
        ),
    )

    assert result["value"] == {"action": "decline", "content": {}}
    assert len(events) == 1
    assert events[0].tool_name == "echo"
    assert events[0].verdict == "deny"


def test_extraction_failure_denies_fail_closed_without_consulting_broker() -> None:
    # The `message` field doesn't match the expected phrasing -- fail-closed
    # per controller ruling: deny without asking the broker anything (there
    # is no tool name to ask about).
    broker = _RaisingBroker()

    result, events = _run_handler_on_a_real_loop(
        broker,
        lambda handler: handler(
            "mcpServer/elicitation/request",
            {
                "serverName": "echo",
                "message": "some unrelated wording that never named a tool",
                "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_params": {}},
            },
        ),
    )

    assert result["value"] == {"action": "decline", "content": {}}
    assert len(events) == 1
    assert events[0].verdict == "deny"


def test_non_mcp_tool_call_elicitation_kind_is_ignored() -> None:
    broker = _RaisingBroker()

    result, events = _run_handler_on_a_real_loop(
        broker,
        lambda handler: handler(
            "mcpServer/elicitation/request",
            {"serverName": "echo", "_meta": {"codex_approval_kind": "something_else"}},
        ),
    )

    assert result["value"] == {}
    assert events == []


def test_unrelated_method_returns_empty_dict() -> None:
    broker = _RaisingBroker()

    result, events = _run_handler_on_a_real_loop(broker, lambda handler: handler("some/other", {}))

    assert result["value"] == {}
    assert events == []


# --- take_native_session_id: per-session pop (mirrors ClaudeBackend) ----


def test_take_native_session_id_pops_and_second_call_returns_none() -> None:
    backend = CodexBackend(_profile(), NativeStoreConfig())
    backend._native_ids["sess-1"] = "thread-abc"

    first = backend.take_native_session_id("sess-1")
    second = backend.take_native_session_id("sess-1")

    assert first == "thread-abc"
    assert second is None


def test_take_native_session_id_is_scoped_per_session() -> None:
    backend = CodexBackend(_profile(), NativeStoreConfig())
    backend._native_ids["sess-a"] = "thread-a"

    assert backend.take_native_session_id("sess-b") is None
    assert backend.take_native_session_id("sess-a") == "thread-a"


# --- interrupt: no-op when nothing registered, delegates otherwise ------


async def test_interrupt_is_a_noop_when_no_handle_is_registered() -> None:
    backend = CodexBackend(_profile(), NativeStoreConfig())

    await backend.interrupt("no-such-session")  # must not raise


async def test_interrupt_calls_the_registered_handles_interrupt() -> None:
    backend = CodexBackend(_profile(), NativeStoreConfig())
    calls: list[bool] = []

    class _FakeHandle:
        def interrupt(self) -> None:
            calls.append(True)

    backend._turn_handles["sess-1"] = _FakeHandle()  # type: ignore[assignment]

    await backend.interrupt("sess-1")

    assert calls == [True]


async def test_interrupt_swallows_a_failure_from_an_already_finished_turn() -> None:
    backend = CodexBackend(_profile(), NativeStoreConfig())

    class _RaisingHandle:
        def interrupt(self) -> None:
            raise RuntimeError("turn already completed")

    backend._turn_handles["sess-1"] = _RaisingHandle()  # type: ignore[assignment]

    await backend.interrupt("sess-1")  # must not raise


# --- capabilities ----------------------------------------------------


def test_capabilities_match_the_brief() -> None:
    caps = CodexBackend(_profile(), NativeStoreConfig()).capabilities()

    assert caps.supports_system_prompt is True
    assert caps.supports_structured_output is True
    assert caps.supports_interactive_permissions is True
    assert caps.supports_in_process_tools is False
    assert caps.supports_native_resume is True
    assert caps.supports_fork is True
    assert caps.supports_transcript_read is True
    # The agentic loop runs inside the codex engine, which exposes no
    # round/turn cap (`TurnStartParams` has none) -- the flag must say so
    # honestly rather than advertise a cap this adapter could only fake.
    assert caps.supports_tool_round_cap is False
    # 05 §3/§7: item, delta and turn signals reach `ctx.work`.
    assert caps.supports_work_supervisor is True
    # 05 §2.5: Codex's own auto-reviewer; approval requests reach the gate.
    assert caps.supports_engine_auto is True
    assert caps.supports_action_policy is True


# --- probe_native: cheap id-truthiness check ---------------------------


async def test_probe_native_is_true_iff_native_session_id_is_set() -> None:
    from tradewind.domain.models import SessionRow

    backend = CodexBackend(_profile(), NativeStoreConfig())
    with_id = SessionRow(
        session_id="s1",
        backend="codex",
        profile="default",
        options_snapshot={},
        native_session_id="t1",
    )
    without_id = SessionRow(
        session_id="s2", backend="codex", profile="default", options_snapshot={}
    )

    assert await backend.probe_native(with_id) is True
    assert await backend.probe_native(without_id) is False


# --- _codex_env: DR-3 isolation-mode gate -------------------------------


def test_codex_env_is_none_when_isolation_mode_is_off() -> None:
    assert _codex_env(NativeStoreConfig(isolation_mode=False)) is None


def test_codex_env_sets_codex_home_when_isolation_mode_is_on() -> None:
    env = _codex_env(NativeStoreConfig(isolation_mode=True, codex_home=Path("/tmp/codex-home")))

    assert env == {"CODEX_HOME": "/tmp/codex-home"}


def test_codex_env_raises_when_isolation_mode_is_on_without_a_codex_home() -> None:
    with pytest.raises(ConfigError, match="codex_home"):
        _codex_env(NativeStoreConfig(isolation_mode=True, codex_home=None))


# --- item-4 fix: abandoning `run()` before a terminal event must interrupt
# the still-in-flight worker, not hang `worker.join()` forever ------------


def _history_loader(messages: list[StoredMessage]):
    """`TurnContext.load_history` is async since FR-9.3 (lazy, awaited by
    mirror-rebuilding backends); tests hand it a pre-baked list."""

    async def load() -> list[StoredMessage]:
        return messages

    return load


def _make_turn_context(*, broker: Any, tools: ToolHost) -> TurnContext:
    return TurnContext(
        session=SessionRow(
            session_id="s1", backend="codex", profile="default", options_snapshot={}
        ),
        turn_id="turn-1",
        prompt="hi",
        model_spec=ModelSpec(model="gpt-5.4-mini"),
        system_prompt=None,
        output_schema=None,
        tools=tools,
        broker=broker,
        load_history=_history_loader([]),
    )


async def test_run_interrupts_the_handle_when_abandoned_before_a_terminal_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`fake_drive_turn` stands in for `_drive_turn`'s real worker thread: it
    registers a fake `TurnHandle` and then BLOCKS -- exactly like a real,
    still in-flight `TurnHandle.stream()` would -- until `interrupt()` is
    actually called on that handle. Abandoning `backend.run(ctx)` (via
    `aclosing`, same pattern as `TurnRunner.execute`'s own abandonment
    handling) after the first worker-produced event must therefore call
    `handle.interrupt()` from `_run_turn`'s `finally` for this test to ever
    complete -- before the item-4 fix, `worker.join()` would wait on this
    thread forever and the test would hang until its own timeout instead.
    """
    interrupt_calls: list[bool] = []
    released = threading.Event()

    class _FakeTurnHandle:
        def interrupt(self) -> None:
            interrupt_calls.append(True)
            released.set()

    def fake_drive_turn(
        client: Any,  # noqa: ARG001 -- matches _drive_turn's real signature
        session: Any,  # noqa: ARG001 -- matches _drive_turn's real signature
        prompt: Any,  # noqa: ARG001 -- matches _drive_turn's real signature
        model: Any,  # noqa: ARG001 -- matches _drive_turn's real signature
        effort: Any,  # noqa: ARG001 -- matches _drive_turn's real signature
        output_schema: Any,  # noqa: ARG001 -- matches _drive_turn's real signature
        system_prompt: Any,  # noqa: ARG001 -- matches _drive_turn's real signature
        sandbox: Any,  # noqa: ARG001 -- matches _drive_turn's real signature
        approval_policy_value: Any,  # noqa: ARG001 -- matches _drive_turn's real signature
        approvals_reviewer: Any,  # noqa: ARG001 -- matches _drive_turn's real signature
        out_queue: queue.Queue[object],
        record_native_id: Any,
        record_handle: Any,
        retry_max: Any,  # noqa: ARG001 -- matches _drive_turn's real signature
        retry_delay_s: Any,  # noqa: ARG001 -- matches _drive_turn's real signature
        ledger: Any,  # noqa: ARG001 -- matches _drive_turn's real signature
    ) -> None:
        record_native_id("thread-1")
        record_handle(_FakeTurnHandle())
        out_queue.put(PermissionRequested(tool_name="probe", tool_input={}, verdict="allow"))
        assert released.wait(timeout=5), "handle.interrupt() was never called -- would hang"
        out_queue.put(codex_backend._DONE)

    monkeypatch.setattr(codex_backend, "_drive_turn", fake_drive_turn)

    backend = CodexBackend(_profile(), NativeStoreConfig())
    tool_host = ToolHost([], [], lambda ref: ref)
    ctx = _make_turn_context(broker=_ScriptedBroker("allow"), tools=tool_host)

    events: list[object] = []
    try:
        stream = backend.run(ctx)
        async with aclosing(stream):
            async for event in stream:
                events.append(event)
                if isinstance(event, PermissionRequested):
                    break  # abandon the stream right after the worker's first event
    finally:
        await tool_host.stop_socket()

    assert isinstance(events[0], TurnStarted)
    assert interrupt_calls == [True]


# --- _sandbox_mode_from_options: safe default when no broker is configured
# (final review wave, item 3a) ------------------------------------------


def test_sandbox_defaults_to_read_only_when_no_broker_is_configured_anywhere() -> None:
    assert _sandbox_mode_from_options({}, allow_all_broker=True) == SandboxMode.read_only


def test_sandbox_keeps_workspace_write_default_when_a_broker_is_configured() -> None:
    assert _sandbox_mode_from_options({}, allow_all_broker=False) == SandboxMode.workspace_write


def test_sandbox_explicit_backend_option_wins_regardless_of_broker() -> None:
    explicit = {"sandbox": "danger-full-access"}
    assert (
        _sandbox_mode_from_options(explicit, allow_all_broker=True)
        == SandboxMode.danger_full_access
    )
    assert (
        _sandbox_mode_from_options(explicit, allow_all_broker=False)
        == SandboxMode.danger_full_access
    )


# --- max_tool_rounds is rejected up front, matching
# supports_tool_round_cap=False (same shape as the cursor/langchain
# output_schema guard tests) ---


async def test_run_with_max_tool_rounds_raises_unsupported_before_turn_started() -> None:
    backend = CodexBackend(_profile(), NativeStoreConfig())
    ctx = _make_turn_context(
        broker=_ScriptedBroker("allow"), tools=ToolHost([], [], lambda ref: ref)
    )
    ctx.max_tool_rounds = 1

    events: list[object] = []
    with pytest.raises(Unsupported):
        async for event in backend.run(ctx):
            events.append(event)

    # No TurnStarted (and in particular no `codex app-server` subprocess
    # attempt) happened before the raise.
    assert events == []


# --- FR-6.6: the worker thread's connect-retry notice reaches the event
# stream as a mirrored retry item ---


async def test_connect_retry_notice_maps_to_event_item() -> None:
    backend = CodexBackend(_profile(), NativeStoreConfig())
    out_queue: queue.Queue[object] = queue.Queue()
    out_queue.put(
        codex_backend._ConnectRetryNotice(
            attempt=1, max_attempts=3, delay_s=2.0, error="engine spawn failed"
        )
    )
    out_queue.put(codex_backend._DONE)

    events = [
        event
        async for event in backend._consume("turn-1", out_queue, ModelSpec(model="gpt-5.4-mini"))
    ]

    from tradewind.domain.events import ItemCompleted

    assert len(events) == 1
    item = events[0]
    assert isinstance(item, ItemCompleted)
    assert item.message.kind == "event"
    assert item.message.content["type"] == "retry_scheduled"
    assert item.message.content["phase"] == "connect"
    assert item.message.content["error"] == "engine spawn failed"


# --- broker verdict enrichment (FR-4.4): reason recorded (no codex
# channel), terminate approximated via handle interrupt ---


def test_denial_reason_is_recorded_and_terminate_queued() -> None:
    from tradewind.domain.models import Denial

    class _Broker:
        async def decide(self, _tool_name: str, _tool_input: dict[str, object]) -> object:
            return Denial(reason="no shell for you", terminate=True)

    result, drained = _run_handler_on_a_real_loop(
        _Broker(),
        lambda handler: handler("item/commandExecution/requestApproval", {"command": "rm -rf /"}),
    )

    assert result["value"] == {"decision": "reject"}
    permission = next(e for e in drained if isinstance(e, PermissionRequested))
    assert permission.reason == "no shell for you"  # recorded; model never sees it
    assert any(isinstance(e, codex_backend._TerminateRequested) for e in drained)


async def test_terminate_request_interrupts_and_reclassifies() -> None:
    from openai_codex.generated.v2_all import Turn, TurnCompletedNotification, TurnStatus

    backend = CodexBackend(_profile(), NativeStoreConfig())
    interrupts: list[bool] = []

    async def request_interrupt() -> None:
        interrupts.append(True)

    out_queue: queue.Queue[object] = queue.Queue()
    out_queue.put(codex_backend._TerminateRequested())

    class _FakeNotification:
        def __init__(self, payload: object) -> None:
            self.payload = payload

    out_queue.put(
        _FakeNotification(
            TurnCompletedNotification(
                thread_id="t-1",
                turn=Turn(id="turn-native-1", items=[], status=TurnStatus.interrupted),
            )
        )
    )
    out_queue.put(codex_backend._DONE)

    events = [
        event
        async for event in backend._consume(
            "turn-1", out_queue, ModelSpec(model="gpt-5.4-mini"), request_interrupt
        )
    ]

    assert interrupts == [True]
    from tradewind.domain.events import TurnCompleted

    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert len(completed) == 1
    assert completed[0].result.status == "completed"
    assert completed[0].result.end_reason == "broker_terminated"


# --- REPLAY (FR-6.1, Phase 4): reactive degrade over the worker queue ---


def test_native_lost_classification_is_conservative() -> None:
    assert codex_backend._is_native_lost_error(Exception("Thread not found: t-1")) is True
    assert codex_backend._is_native_lost_error(Exception("thread t-1 does not exist")) is True
    assert codex_backend._is_native_lost_error(Exception("rate limited")) is False
    assert codex_backend._is_native_lost_error(Exception("internal server error")) is False


async def test_replay_prompt_request_is_answered_and_visible() -> None:
    backend = CodexBackend(_profile(), NativeStoreConfig())
    reply: queue.Queue[str] = queue.Queue()
    out_queue: queue.Queue[object] = queue.Queue()
    out_queue.put(codex_backend._ReplayPromptRequest(reason="thread not found", reply=reply))
    out_queue.put(codex_backend._DONE)

    async def build_replay_prompt() -> str:
        return "RENDERED-PREAMBLE\n\nthe ask"

    events = [
        event
        async for event in backend._consume(
            "turn-1",
            out_queue,
            ModelSpec(model="gpt-5.4-mini"),
            None,
            build_replay_prompt,
        )
    ]

    assert reply.get_nowait() == "RENDERED-PREAMBLE\n\nthe ask"  # worker unblocked
    from tradewind.domain.events import ItemCompleted

    degraded = [
        e
        for e in events
        if isinstance(e, ItemCompleted) and e.message.content.get("type") == "resume_degraded"
    ]
    assert len(degraded) == 1
    assert "thread not found" in str(degraded[0].message.content["reason"])


# --- engine compaction observability (FR-5.8) ---


async def test_codex_context_compacted_notification_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex encrypts its summary (`CompactionResponseItem.
    encrypted_content`), so the record carries no text -- only the native
    turn id, so the compaction can be cross-referenced against the native
    transcript."""

    def fake_drive_turn(
        client: Any,  # noqa: ARG001 -- matches _drive_turn's real signature
        session: Any,  # noqa: ARG001
        prompt: Any,  # noqa: ARG001
        model: Any,  # noqa: ARG001
        effort: Any,  # noqa: ARG001
        output_schema: Any,  # noqa: ARG001
        system_prompt: Any,  # noqa: ARG001
        sandbox: Any,  # noqa: ARG001
        approval_policy_value: Any,  # noqa: ARG001
        approvals_reviewer: Any,  # noqa: ARG001
        out_queue: queue.Queue[object],
        record_native_id: Any,
        record_handle: Any,  # noqa: ARG001
        retry_max: Any,  # noqa: ARG001
        retry_delay_s: Any,  # noqa: ARG001
        ledger: Any,  # noqa: ARG001
    ) -> None:
        record_native_id("thread-1")
        out_queue.put(
            SimpleNamespace(
                payload=ContextCompactedNotification(threadId="thread-1", turnId="native-turn-9")
            )
        )
        out_queue.put(codex_backend._DONE)

    monkeypatch.setattr(codex_backend, "_drive_turn", fake_drive_turn)

    backend = CodexBackend(_profile(), NativeStoreConfig())
    tool_host = ToolHost([], [], lambda ref: ref)
    ctx = _make_turn_context(broker=_ScriptedBroker("allow"), tools=tool_host)
    try:
        events = [event async for event in backend.run(ctx)]
    finally:
        await tool_host.stop_socket()

    compactions = [
        e for e in events if isinstance(e, ItemCompleted) and e.message.kind == "compaction"
    ]
    assert len(compactions) == 1
    assert compactions[0].message.native_id == "native-turn-9"
    # No summary text is invented for an engine that does not expose one.
    assert compactions[0].message.content == {}


# --- FR-1.3: structured output. Codex's final answer IS the JSON text, so
# `final_text` stays exactly as before and the parsed result is ADDED as
# `TurnResult.structured_output`. ---


def _codex_turn_queue(final_answer: str) -> queue.Queue[object]:
    from openai_codex.generated.v2_all import (
        AgentMessageThreadItem,
        ItemCompletedNotification,
        MessagePhase,
        ThreadItem,
        Turn,
        TurnCompletedNotification,
        TurnStatus,
    )

    class _FakeNotification:
        def __init__(self, payload: object) -> None:
            self.payload = payload

    out_queue: queue.Queue[object] = queue.Queue()
    out_queue.put(
        _FakeNotification(
            ItemCompletedNotification(
                thread_id="t-1",
                turn_id="turn-native-1",
                completed_at_ms=0,
                item=ThreadItem(
                    root=AgentMessageThreadItem(
                        id="m-1",
                        type="agentMessage",
                        text=final_answer,
                        phase=MessagePhase.final_answer,
                    )
                ),
            )
        )
    )
    out_queue.put(
        _FakeNotification(
            TurnCompletedNotification(
                thread_id="t-1",
                turn=Turn(id="turn-native-1", items=[], status=TurnStatus.completed),
            )
        )
    )
    out_queue.put(codex_backend._DONE)
    return out_queue


async def test_schema_turn_adds_structured_output_and_keeps_final_text() -> None:
    from tradewind.domain.events import TurnCompleted

    backend = CodexBackend(_profile(), NativeStoreConfig())

    events = [
        event
        async for event in backend._consume(
            "turn-1",
            _codex_turn_queue('{"answer": 42}'),
            ModelSpec(model="gpt-5.4-mini"),
            has_output_schema=True,
        )
    ]

    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert len(completed) == 1
    assert completed[0].result.final_text == '{"answer": 42}'  # unchanged
    assert completed[0].result.structured_output == {"answer": 42}
    texts = [e.message.content["text"] for e in events if isinstance(e, ItemCompleted)]
    assert texts == ['{"answer": 42}']  # the mirror item is unchanged too


async def test_plain_turn_has_no_structured_output_even_when_text_looks_like_json() -> None:
    # No schema = today's behaviour exactly: nothing is parsed or added.
    from tradewind.domain.events import TurnCompleted

    backend = CodexBackend(_profile(), NativeStoreConfig())

    events = [
        event
        async for event in backend._consume(
            "turn-1", _codex_turn_queue('{"answer": 42}'), ModelSpec(model="gpt-5.4-mini")
        )
    ]

    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert completed[0].result.final_text == '{"answer": 42}'
    assert completed[0].result.structured_output is None


async def test_schema_turn_whose_final_answer_is_not_a_json_object_completes_without_a_result() -> (
    None
):
    """Owner rule (FR-1.3): a schema turn that ends normally without a
    valid result still COMPLETES -- the text is kept, `structured_output`
    is None."""
    from tradewind.domain.events import TurnCompleted, TurnFailed

    backend = CodexBackend(_profile(), NativeStoreConfig())

    events = [
        event
        async for event in backend._consume(
            "turn-1",
            _codex_turn_queue("sorry, no JSON today"),
            ModelSpec(model="gpt-5.4-mini"),
            has_output_schema=True,
        )
    ]

    assert not any(isinstance(e, TurnFailed) for e in events)
    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert len(completed) == 1
    assert completed[0].result.end_reason == "end_turn"
    assert completed[0].result.final_text == "sorry, no JSON today"
    assert completed[0].result.structured_output is None


# --- turn governance (05-turn-governance §2.1, §2.3, §2.5, §2.6, §5, §7) --


def _note(payload: object) -> SimpleNamespace:
    """A stand-in `Notification`: `_consume` reads only `.payload`."""
    return SimpleNamespace(payload=payload)


def _command_item(item_id: str, command: str = "ls") -> Any:
    from openai_codex.generated.v2_all import (
        CommandExecutionStatus,
        CommandExecutionThreadItem,
        LegacyAppPathString,
        ThreadItem,
    )

    return ThreadItem(
        root=CommandExecutionThreadItem(
            id=item_id,
            command=command,
            command_actions=[],
            cwd=LegacyAppPathString("/w"),
            status=CommandExecutionStatus.completed,
            type="commandExecution",
        )
    )


def _file_item(item_id: str, path: str = "a.py") -> Any:
    from openai_codex.generated.v2_all import (
        AddPatchChangeKind,
        FileChangeThreadItem,
        FileUpdateChange,
        PatchApplyStatus,
        PatchChangeKind,
        ThreadItem,
    )

    return ThreadItem(
        root=FileChangeThreadItem(
            id=item_id,
            changes=[
                FileUpdateChange(
                    path=path, kind=PatchChangeKind(AddPatchChangeKind(type="add")), diff="+x"
                )
            ],
            status=PatchApplyStatus.completed,
            type="fileChange",
        )
    )


def _agent_item(item_id: str, text: str) -> Any:
    from openai_codex.generated.v2_all import AgentMessageThreadItem, MessagePhase, ThreadItem

    return ThreadItem(
        root=AgentMessageThreadItem(
            id=item_id, type="agentMessage", text=text, phase=MessagePhase.final_answer
        )
    )


def _started(item: Any) -> SimpleNamespace:
    from openai_codex.generated.v2_all import ItemStartedNotification

    return _note(
        ItemStartedNotification(thread_id="t-1", turn_id="n-1", started_at_ms=0, item=item)
    )


def _completed(item: Any) -> SimpleNamespace:
    from openai_codex.generated.v2_all import ItemCompletedNotification

    return _note(
        ItemCompletedNotification(thread_id="t-1", turn_id="n-1", completed_at_ms=0, item=item)
    )


def _turn_done(status: str = "completed") -> SimpleNamespace:
    from openai_codex.generated.v2_all import Turn, TurnCompletedNotification, TurnStatus

    return _note(
        TurnCompletedNotification(
            thread_id="t-1", turn=Turn(id="n-1", items=[], status=TurnStatus(status))
        )
    )


def _queue_of(*items: object) -> queue.Queue[object]:
    out: queue.Queue[object] = queue.Queue()
    for item in items:
        out.put(item)
    out.put(codex_backend._DONE)
    return out


class _RecordingWork:
    """Records every work signal `report_work` sends (05 §3.1)."""

    def __init__(self) -> None:
        self.signals: list[tuple[str, ...]] = []

    def started(self, id: str, kind: str, label: str, has_end_signal: bool = True) -> None:  # noqa: A002, ARG002
        self.signals.append(("started", id, kind))

    def active(self, id: str) -> None:  # noqa: A002
        self.signals.append(("active", id))

    def ended(self, id: str) -> None:  # noqa: A002
        self.signals.append(("ended", id))

    def model_active(self) -> None:
        self.signals.append(("model_active",))

    def engine_done(self) -> None:
        self.signals.append(("engine_done",))


async def _consume_all(
    out_queue: queue.Queue[object], **kwargs: Any
) -> tuple[list[object], list[bool]]:
    backend = CodexBackend(_profile(), NativeStoreConfig())
    interrupts: list[bool] = []

    async def request_interrupt() -> None:
        interrupts.append(True)

    events = [
        event
        async for event in backend._consume(
            "turn-1",
            out_queue,
            ModelSpec(model="gpt-5.4-mini"),
            request_interrupt,
            workspace="/w",
            **kwargs,
        )
    ]
    return events, interrupts


def test_broker_gets_todays_names_and_args_plus_the_action() -> None:
    """05 §1.1: brokers keep `"shell"` / `"apply_patch"` and today's
    arguments; the shared vocabulary rides along under `tradewind`."""
    broker = _ScriptedBroker("allow")
    ledger = ApprovalLedger(wait_s=0.0)
    ledger.record_started(_file_item("patch-1", path="src/a.py"))

    _run_handler_on_a_real_loop(
        broker,
        lambda handler: (
            handler(
                "item/commandExecution/requestApproval",
                {"itemId": "exec-1", "command": "git push", "cwd": "/w"},
            ),
            handler("item/fileChange/requestApproval", {"itemId": "patch-1"}),
        ),
        ledger=ledger,
    )

    assert broker.calls == [
        ("shell", {"command": "git push"}),
        ("apply_patch", {"changes": None}),
    ]
    shell, write = broker.actions
    assert shell["kind"] == "shell"
    assert shell["fields"]["command"] == "git push"
    # The request itself carries no changes (app-server 0.147.0
    # `FileChangeRequestApprovalParams`); they come from the item's start.
    assert write["kind"] == "file_write"
    assert write["fields"]["changes"] == [
        {"path": "src/a.py", "change": "add", "in_workspace": True}
    ]


def test_file_change_request_with_no_known_changes_is_denied_by_the_floor() -> None:
    """No `item/started` for the item within the wait: the critical field
    is missing, so the floor denies without asking (owner M8)."""
    emitted: list[object] = []

    result, events = _run_handler_on_a_real_loop(
        None,
        lambda handler: handler("item/fileChange/requestApproval", {"itemId": "patch-9"}),
        policy=_gate(_RaisingBroker(), emitted=emitted),
    )

    assert result["value"] == {"decision": "reject"}
    assert [type(e).__name__ for e in events] == ["PermissionRequested"]
    assert len(emitted) == 1  # the floor rule's record notice


def test_caller_deny_rule_rejects_without_asking_the_broker() -> None:
    result, events = _run_handler_on_a_real_loop(
        None,
        lambda handler: handler(
            "item/commandExecution/requestApproval", {"itemId": "e-1", "command": "rm -rf x"}
        ),
        policy=_gate(
            _RaisingBroker(), caller=[_rule("shell", "deny", "no rm", command="rm -rf x")]
        ),
    )

    assert result["value"] == {"decision": "reject"}
    assert len(events) == 1
    permission = events[0]
    assert isinstance(permission, PermissionRequested)
    assert permission.reason == "no rm"


def test_gate_stop_rejects_reports_the_denial_and_queues_a_stop() -> None:
    """05 §2.6: `stop` cancels and ends the turn; FR-4.1 reports it as a
    denial (as the Claude adapter does)."""
    result, events = _run_handler_on_a_real_loop(
        None,
        lambda handler: handler(
            "item/commandExecution/requestApproval", {"itemId": "e-1", "command": "curl x"}
        ),
        policy=_gate(_RaisingBroker(), caller=[_rule("shell", "stop", "no network")]),
    )

    assert result["value"] == {"decision": "reject"}
    permission, stop = events
    assert isinstance(permission, PermissionRequested)
    assert permission.verdict == "deny"
    assert isinstance(stop, codex_backend._StopRequested)
    assert stop.reason == "no network"


async def test_stop_request_interrupts_and_fails_the_turn_with_the_reason() -> None:
    from tradewind.domain.events import TurnCompleted, TurnFailed

    events, interrupts = await _consume_all(
        _queue_of(codex_backend._StopRequested(reason="no network"), _turn_done("interrupted"))
    )

    assert interrupts == [True]
    assert not any(isinstance(e, TurnCompleted) for e in events)
    failed = [e for e in events if isinstance(e, TurnFailed)]
    assert len(failed) == 1
    assert failed[0].error == "no network"


def test_shim_elicitation_is_a_pass_through_asked_once_inside_tool_host() -> None:
    """05 §2.3: the shim call is gated once, inside `ToolHost.call` -- even
    a caller rule that denies every caller tool does not act here."""
    result, events = _run_handler_on_a_real_loop(
        None,
        lambda handler: handler(
            "mcpServer/elicitation/request",
            {
                "serverName": "toolproxy",
                "message": 'Allow the toolproxy MCP server to run tool "merge"?',
                "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_params": {}},
            },
        ),
        policy=_gate(_RaisingBroker(), caller=[_rule("caller_tool", "deny")]),
    )

    assert result["value"] == {"action": "accept", "content": {}}
    assert events == []


def test_non_shim_mcp_call_is_gated_as_an_mcp_action() -> None:
    broker = _ScriptedBroker("allow")

    _run_handler_on_a_real_loop(
        broker,
        lambda handler: handler(
            "mcpServer/elicitation/request",
            {
                "serverName": "echo",
                "message": 'Allow the echo MCP server to run tool "say"?',
                "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_params": {"t": 1}},
            },
        ),
    )

    assert broker.calls == [("say", {"t": 1})]
    assert broker.actions[0]["kind"] == "mcp"
    assert broker.actions[0]["fields"] == {"server": "echo", "tool": "say", "args": {"t": 1}}


def test_non_shim_mcp_call_with_an_unreadable_name_is_denied_by_the_floor() -> None:
    """Today's fail-closed rule, now as `missing_critical_field` (05 §7)."""
    emitted: list[object] = []

    result, events = _run_handler_on_a_real_loop(
        None,
        lambda handler: handler(
            "mcpServer/elicitation/request",
            {
                "serverName": "echo",
                "message": "no tool named here",
                "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_params": {}},
            },
        ),
        policy=_gate(_RaisingBroker(), caller=[_rule("mcp", "allow")], emitted=emitted),
    )

    assert result["value"] == {"action": "decline", "content": {}}
    assert len(events) == 1
    assert events[0].tool_name == "<unknown>"
    assert len(emitted) == 1  # the floor rule's record notice


async def test_ask_rule_on_requested_command_reaches_broker() -> None:
    """05 Acceptance: a rule with `ask` on a command that Codex asks about
    reaches the broker, and the command's later `item/completed` is never
    observed -- so the turn is not stopped after the fact."""
    from tradewind.domain.events import TurnCompleted, TurnFailed

    broker = _ScriptedBroker("allow")
    ledger = ApprovalLedger(wait_s=0.0)
    policy = _gate(broker, caller=[_rule("shell", "ask")])

    result, _ = _run_handler_on_a_real_loop(
        broker,
        lambda handler: handler(
            "item/commandExecution/requestApproval", {"itemId": "exec-1", "command": "make"}
        ),
        policy=policy,
        ledger=ledger,
    )
    events, interrupts = await _consume_all(
        _queue_of(
            _started(_command_item("exec-1", "make")),
            _completed(_command_item("exec-1", "make")),
            _turn_done(),
        ),
        policy=policy,
        ledger=ledger,
    )

    assert result["value"] == {"decision": "accept"}
    assert broker.calls == [("shell", {"command": "make"})]
    assert interrupts == []
    assert not any(isinstance(e, TurnFailed) for e in events)
    assert any(isinstance(e, TurnCompleted) for e in events)


async def test_observe_only_completed_without_request() -> None:
    """05 §2.1/§7: only a command that never asked is observed, at its
    `item/completed`; an observed `deny` stops the turn after the fact."""
    from tradewind.domain.events import TurnFailed

    policy = _gate(_RaisingBroker(), caller=[_rule("shell", "deny", "no ls", command="ls")])
    ledger = ApprovalLedger(wait_s=0.0)

    events, interrupts = await _consume_all(
        _queue_of(
            _started(_command_item("exec-2")),
            _completed(_command_item("exec-2")),
            _turn_done("interrupted"),
        ),
        policy=policy,
        ledger=ledger,
    )

    assert interrupts == [True]
    # The command already ran: it is still mirrored.
    kinds = [e.message.kind for e in events if isinstance(e, ItemCompleted)]
    assert kinds == ["command_execution"]
    failed = [e for e in events if isinstance(e, TurnFailed)]
    assert len(failed) == 1
    assert "ls" in failed[0].error
    # The matching rule's reason reaches the stop (05 §2.1).
    assert "no ls" in failed[0].error


async def test_tool_host_broker_labels_caller_tools_by_name() -> None:
    """05 §1: a caller tool's `source` is `local` or `mcp:<server>`, also
    for the broker `turn_runner` gives Codex's `ToolHost`."""
    broker = _ScriptedBroker("allow")
    tool_host_broker = _gate(broker, caller=[_rule("caller_tool", "ask")]).as_broker()

    assert await tool_host_broker.decide("merge", {"pr": 7}) == "allow"
    assert await tool_host_broker.decide("mcp__gh__merge", {}) == "allow"

    assert [a["fields"]["source"] for a in broker.actions] == ["local", "mcp:gh"]
    assert broker.actions[0]["backend"] == "codex"
    assert broker.calls[0] == ("merge", {"pr": 7})


async def test_item_started_is_never_observed() -> None:
    """A request may follow `item/started`, so it is never observed."""
    from tradewind.domain.events import TurnCompleted

    policy = _gate(_RaisingBroker(), caller=[_rule("shell", "deny"), _rule("file_write", "deny")])

    events, interrupts = await _consume_all(
        _queue_of(_started(_command_item("exec-3")), _started(_file_item("p-3")), _turn_done()),
        policy=policy,
        ledger=ApprovalLedger(wait_s=0.0),
    )

    assert interrupts == []
    assert any(isinstance(e, TurnCompleted) for e in events)


async def test_observed_file_change_without_request_can_stop_the_turn() -> None:
    from tradewind.domain.events import TurnFailed

    policy = _gate(_RaisingBroker(), caller=[_rule("file_write", "deny")])

    events, interrupts = await _consume_all(
        _queue_of(_completed(_file_item("p-4")), _turn_done("completed")),
        policy=policy,
        ledger=ApprovalLedger(wait_s=0.0),
    )

    assert interrupts == [True]
    assert any(isinstance(e, TurnFailed) for e in events)


async def test_observed_web_item_stops_at_the_first_stopped_action() -> None:
    """07 §4: each query of a search is its own action, in order; the first
    stop ends the turn."""
    from openai_codex.generated.v2_all import ThreadItem

    from tradewind.domain.events import TurnFailed

    item = ThreadItem.model_validate(
        {
            "id": "ws-1",
            "type": "webSearch",
            "query": "a",
            "action": {"type": "search", "queries": ["a", "b"]},
        }
    )
    policy = _gate(
        _RaisingBroker(),
        caller=[
            _rule("web_search", "allow", query="a"),
            _rule("web_search", "deny", "no web"),
        ],
    )

    events, interrupts = await _consume_all(
        _queue_of(_completed(item), _turn_done("completed")),
        policy=policy,
        ledger=ApprovalLedger(wait_s=0.0),
    )

    assert interrupts == [True]
    (failed,) = [e for e in events if isinstance(e, TurnFailed)]
    assert "web_search 'b'" in failed.error
    assert "no web" in failed.error


def _web_completed(action: dict[str, Any]) -> Any:
    from openai_codex.generated.v2_all import ThreadItem

    return _completed(
        ThreadItem.model_validate(
            {"id": "ws-2", "type": "webSearch", "query": "q", "action": action}
        )
    )


@pytest.mark.parametrize(
    ("action", "named"),
    [
        ({"type": "openPage"}, "fetch_url 'openPage'"),
        ({"type": "other"}, "web_search 'other'"),
    ],
)
async def test_observed_web_item_missing_its_field_is_stopped_by_the_floor(
    action: dict[str, Any], named: str
) -> None:
    from tradewind.domain.events import TurnFailed

    events, interrupts = await _consume_all(
        _queue_of(_web_completed(action), _turn_done("completed")),
        policy=_gate(_RaisingBroker()),
        ledger=ApprovalLedger(wait_s=0.0),
    )

    assert interrupts == [True]
    (failed,) = [e for e in events if isinstance(e, TurnFailed)]
    assert named in failed.error


async def test_observed_web_item_never_evaluates_queries_after_the_stop() -> None:
    from tradewind.domain.events import TurnFailed

    policy = _gate(
        _RaisingBroker(),
        caller=[_rule("web_search", "allow", query="a"), _rule("web_search", "deny")],
    )
    seen: list[str] = []
    observe = policy.observe

    def recording(action: Any) -> Any:
        seen.append(action.summary)
        return observe(action)

    policy.observe = recording

    events, _ = await _consume_all(
        _queue_of(
            _web_completed({"type": "search", "queries": ["a", "b", "c"]}),
            _turn_done("completed"),
        ),
        policy=policy,
        ledger=ApprovalLedger(wait_s=0.0),
    )

    assert seen == ["a", "b"]
    assert any(isinstance(e, TurnFailed) for e in events)


def test_report_work_maps_items_deltas_and_turn_end() -> None:
    """05 §7 Codex row: items open and close work; output, progress,
    auto-review and terminal input are activity; message and reasoning
    deltas are model activity; `turn/completed` is the engine's done."""
    from openai_codex.generated.v2_all import (
        AgentMessageDeltaNotification,
        CommandExecutionOutputDeltaNotification,
        ItemGuardianApprovalReviewCompletedNotification,
        ItemGuardianApprovalReviewStartedNotification,
        McpToolCallProgressNotification,
        ReasoningSummaryTextDeltaNotification,
        ReasoningTextDeltaNotification,
        TerminalInteractionNotification,
    )

    from tradewind.adapters.codex_backend import report_work

    ids = {"thread_id": "t-1", "turn_id": "n-1"}
    work = _RecordingWork()
    for note in [
        _started(_command_item("exec-1")),
        _note(CommandExecutionOutputDeltaNotification(item_id="exec-1", delta="x", **ids)),
        _note(TerminalInteractionNotification(item_id="exec-1", process_id="p", stdin="y", **ids)),
        _note(McpToolCallProgressNotification(item_id="call-1", message="50%", **ids)),
        _note(
            ItemGuardianApprovalReviewStartedNotification.model_construct(
                target_item_id="exec-1", review_id="r-1"
            )
        ),
        _note(
            ItemGuardianApprovalReviewCompletedNotification.model_construct(
                target_item_id=None, review_id="r-1"
            )
        ),
        _completed(_command_item("exec-1")),
        _started(_agent_item("m-1", "")),
        _note(AgentMessageDeltaNotification(item_id="m-1", delta="h", **ids)),
        _note(ReasoningTextDeltaNotification(item_id="r", content_index=0, delta="t", **ids)),
        _note(
            ReasoningSummaryTextDeltaNotification(item_id="r", summary_index=0, delta="s", **ids)
        ),
        _completed(_agent_item("m-1", "hi")),
        _turn_done(),
    ]:
        report_work(cast(Any, work), note.payload)

    assert work.signals == [
        ("started", "exec-1", "tool"),
        ("active", "exec-1"),
        ("active", "exec-1"),
        ("active", "call-1"),
        ("active", "exec-1"),
        ("active", "r-1"),
        ("ended", "exec-1"),
        ("model_active",),
        ("model_active",),
        ("model_active",),
        ("model_active",),
        ("model_active",),
        ("engine_done",),
    ]


async def test_plain_turn_events_are_identical_with_activity_notifications() -> None:
    """05 Acceptance: work signals never become events or mirror items --
    the same turn with and without the activity notifications yields
    exactly the same events, so exactly the same mirror rows (the runner
    mirrors each `ItemCompleted.message`)."""
    from openai_codex.generated.v2_all import (
        AgentMessageDeltaNotification,
        CommandExecutionOutputDeltaNotification,
        ItemGuardianApprovalReviewCompletedNotification,
        ItemGuardianApprovalReviewStartedNotification,
        McpToolCallProgressNotification,
        ReasoningSummaryTextDeltaNotification,
        ReasoningTextDeltaNotification,
        TerminalInteractionNotification,
    )

    ids = {"thread_id": "t-1", "turn_id": "n-1"}
    plain = _queue_of(
        _completed(_command_item("exec-1")),
        _completed(_agent_item("m-1", "hello")),
        _turn_done(),
    )
    busy = _queue_of(
        _started(_command_item("exec-1")),
        _note(
            ItemGuardianApprovalReviewStartedNotification.model_construct(
                target_item_id="exec-1", review_id="r-1"
            )
        ),
        _note(
            ItemGuardianApprovalReviewCompletedNotification.model_construct(
                target_item_id="exec-1", review_id="r-1"
            )
        ),
        _note(CommandExecutionOutputDeltaNotification(item_id="exec-1", delta="x", **ids)),
        _note(TerminalInteractionNotification(item_id="exec-1", process_id="p", stdin="y", **ids)),
        _note(McpToolCallProgressNotification(item_id="call-1", message="50%", **ids)),
        _completed(_command_item("exec-1")),
        _started(_agent_item("m-1", "")),
        _note(ReasoningTextDeltaNotification(item_id="r", content_index=0, delta="t", **ids)),
        _note(
            ReasoningSummaryTextDeltaNotification(item_id="r", summary_index=0, delta="s", **ids)
        ),
        _note(AgentMessageDeltaNotification(item_id="m-1", delta="hel", **ids)),
        _completed(_agent_item("m-1", "hello")),
        _turn_done(),
    )

    plain_events, _ = await _consume_all(
        plain,
        work=_RecordingWork(),
        policy=_gate(_ScriptedBroker("allow")),
        ledger=ApprovalLedger(),
    )
    busy_work = _RecordingWork()
    busy_events, _ = await _consume_all(
        busy, work=busy_work, policy=_gate(_ScriptedBroker("allow")), ledger=ApprovalLedger()
    )

    assert busy_events == plain_events
    assert [type(e).__name__ for e in plain_events] == [
        "ItemCompleted",
        "ItemCompleted",
        "TurnCompleted",
    ]

    def mirror_rows(events: list[object]) -> list[tuple[str, str, dict[str, object], str | None]]:
        return [
            (e.message.role, e.message.kind, e.message.content, e.message.native_id)
            for e in events
            if isinstance(e, ItemCompleted)
        ]

    assert mirror_rows(busy_events) == mirror_rows(plain_events)
    assert [kind for _role, kind, _content, _id in mirror_rows(plain_events)] == [
        "command_execution",
        "text",
    ]
    # The activity notifications did reach the supervisor.
    assert ("active", "call-1") in busy_work.signals


async def test_a_failed_interrupt_keeps_the_policy_stop_reason() -> None:
    """The engine may have ended the turn already; the stop reason must
    still end it as `TurnFailed(reason)`."""
    from tradewind.domain.events import TurnFailed

    backend = CodexBackend(_profile(), NativeStoreConfig())

    async def failing_interrupt() -> None:
        raise RuntimeError("turn already finished")

    policy = _gate(_RaisingBroker(), caller=[_rule("shell", "deny")])
    for script in (
        _queue_of(codex_backend._StopRequested(reason="no network"), _turn_done("completed")),
        _queue_of(_completed(_command_item("exec-5")), _turn_done("completed")),
    ):
        events = [
            event
            async for event in backend._consume(
                "turn-1",
                script,
                ModelSpec(model="gpt-5.4-mini"),
                failing_interrupt,
                policy=policy,
                ledger=ApprovalLedger(wait_s=0.0),
                workspace="/w",
            )
        ]
        failed = [e for e in events if isinstance(e, TurnFailed)]
        assert len(failed) == 1
        assert "turn already finished" not in failed[0].error


def test_thread_settings_engine_auto_uses_the_auto_reviewer_with_workspace_write() -> None:
    """05 §2.5: engine_auto = Codex's own auto-reviewer
    (`ApprovalsReviewer.auto_review`, the value `ApprovalMode.auto_review`
    maps to in `openai_codex/_approval_mode.py`) with workspace-write, also
    when no broker is set."""
    from openai_codex.generated.v2_all import ApprovalsReviewer

    from tradewind.adapters.codex_backend import _thread_settings

    for allow_all in (True, False):
        assert _thread_settings({}, allow_all_broker=allow_all, engine_auto=True) == (
            SandboxMode.workspace_write,
            ApprovalsReviewer.auto_review,
        )


def test_thread_settings_without_engine_auto_are_unchanged() -> None:
    from openai_codex.generated.v2_all import ApprovalsReviewer

    from tradewind.adapters.codex_backend import _thread_settings

    assert _thread_settings({}, allow_all_broker=True, engine_auto=False) == (
        SandboxMode.read_only,
        ApprovalsReviewer.user,
    )
    assert _thread_settings({}, allow_all_broker=False, engine_auto=False) == (
        SandboxMode.workspace_write,
        ApprovalsReviewer.user,
    )
    explicit = {"sandbox": "read-only"}
    assert _thread_settings(explicit, allow_all_broker=False, engine_auto=True)[0] == (
        SandboxMode.read_only
    )


async def test_run_passes_engine_auto_settings_to_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openai_codex.generated.v2_all import ApprovalsReviewer

    captured: list[tuple[object, ...]] = []

    def fake_drive_turn(*args: Any) -> None:
        captured.append(args)
        args[10].put(codex_backend._DONE)

    monkeypatch.setattr(codex_backend, "_drive_turn", fake_drive_turn)
    backend = CodexBackend(_profile(), NativeStoreConfig())
    tool_host = ToolHost([], [], lambda ref: ref)
    ctx = _make_turn_context(broker=_ScriptedBroker("allow"), tools=tool_host)
    ctx.session.options_snapshot["permission_mode"] = "engine_auto"
    try:
        [event async for event in backend.run(ctx)]
    finally:
        await tool_host.stop_socket()

    (args,) = captured
    assert args[7] == SandboxMode.workspace_write
    assert args[9] == ApprovalsReviewer.auto_review


class _FakeClient:
    """Records what `_drive_turn` sends; the stream ends at once."""

    def __init__(self) -> None:
        self.prompts: list[object] = []
        self.thread_params: list[object] = []

    def start(self) -> None: ...

    def initialize(self) -> None: ...

    def close(self) -> None: ...

    def thread_start(self, params: object) -> Any:
        self.thread_params.append(params)
        return SimpleNamespace(thread=SimpleNamespace(id="t-1"))

    def turn_start(self, thread_id: str, prompt: object, params: object) -> Any:  # noqa: ARG002
        self.prompts.append(prompt)
        return SimpleNamespace(turn=SimpleNamespace(id="n-1"))


def test_slash_prompt_reaches_the_model_as_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """05 §5: `codex app-server` 0.147.0 does not expand `/` prompts (its
    `turn/start` maps text input straight to core input: app-server
    `request_processors/turn_processor.rs:512-516`, app-server-protocol
    `v2/turn.rs:328-336`), so the prompt is sent unchanged."""
    from openai_codex.generated.v2_all import ApprovalsReviewer, AskForApprovalValue

    class _EmptyHandle:
        def __init__(self, *_args: object) -> None: ...

        def stream(self) -> list[object]:
            return []

    monkeypatch.setattr(codex_backend, "TurnHandle", _EmptyHandle)
    client = _FakeClient()
    out_queue: queue.Queue[object] = queue.Queue()

    codex_backend._drive_turn(
        lambda: cast(Any, client),
        SessionRow(session_id="s1", backend="codex", profile="default", options_snapshot={}),
        "/clear",
        "gpt-5.4-mini",
        None,
        None,
        None,
        SandboxMode.workspace_write,
        AskForApprovalValue.on_request,
        ApprovalsReviewer.user,
        out_queue,
        lambda _id: None,
        lambda _handle: None,
        0,
        0.0,
        ApprovalLedger(),
    )

    assert client.prompts == ["/clear"]
    assert cast(Any, client.thread_params[0]).approvals_reviewer == ApprovalsReviewer.user


def test_ledger_hands_the_changes_to_a_request_that_waits_for_the_start() -> None:
    """The request is read after its item's start, but the worker thread may
    record that start a little later: the handler side waits for it."""
    ledger = ApprovalLedger(wait_s=5.0)
    got: list[object] = []
    waiter = threading.Thread(target=lambda: got.append(ledger.started_changes("patch-1")))
    waiter.start()
    ledger.record_started(_file_item("patch-1", path="b.py"))
    waiter.join(timeout=5)

    (changes,) = got
    assert [c.path for c in cast(Any, changes)] == ["b.py"]
    assert ApprovalLedger(wait_s=0.0).started_changes("missing") is None


# --- process report seam (05 §3.4) ---


def test_turn_client_reports_the_engine_pid_and_snapshots_before_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run()` hands `_drive_turn` a client factory whose clients report
    the app-server's PID once started and ask for a snapshot before they
    close it (no process is spawned: `CodexClient.start`/`close` are
    replaced at the SDK boundary)."""
    from openai_codex.client import CodexClient

    order: list[str] = []

    def fake_start(self: Any) -> None:
        self._proc = SimpleNamespace(pid=5151)
        order.append("start")

    def fake_close(self: Any) -> None:
        self._proc = None
        order.append("close")

    monkeypatch.setattr(CodexClient, "start", fake_start)
    monkeypatch.setattr(CodexClient, "close", fake_close)

    def fake_drive_turn(*args: Any) -> None:
        client = args[0]()
        client.start()
        client.close()
        args[10].put(codex_backend._DONE)

    monkeypatch.setattr(codex_backend, "_drive_turn", fake_drive_turn)
    tool_host = ToolHost([], [], lambda ref: ref)
    ctx = _make_turn_context(broker=_ScriptedBroker("allow"), tools=tool_host)
    ctx.engine_started = lambda pid: order.append(f"started {pid}")
    ctx.engine_closing = lambda: order.append("closing")

    async def run() -> None:
        try:
            [event async for event in CodexBackend(_profile(), NativeStoreConfig()).run(ctx)]
        finally:
            await tool_host.stop_socket()

    asyncio.run(run())

    assert order == ["start", "started 5151", "closing", "close"]


def test_turn_client_without_the_sdk_attribute_warns_and_reports_no_pid(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from openai_codex.client import CodexClient

    started: list[int] = []
    monkeypatch.setattr(CodexClient, "start", lambda self: delattr(self, "_proc"))
    client = codex_backend._EngineReportingClient(
        engine_started=started.append, engine_closing=lambda: None
    )

    with caplog.at_level("WARNING"):
        client.start()

    assert started == []
    assert any(
        "process report unavailable: codex SDK has no _proc" in r.getMessage()
        for r in caplog.records
    )


def test_installed_sdk_keeps_the_app_server_process_where_the_adapter_reads_it() -> None:
    """Pins the private attribute `_engine_pid` reads: an SDK upgrade that
    moves it fails here, not silently in production."""
    import inspect

    from openai_codex.client import CodexClient

    assert hasattr(CodexClient(), "_proc")
    assert "self._proc = subprocess.Popen(" in inspect.getsource(CodexClient.start)
