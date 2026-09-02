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
from typing import Any

import pytest
from openai_codex.generated.v2_all import SandboxMode

import tradewind.adapters.codex_backend as codex_backend
from tradewind.adapters.codex_backend import (
    CodexBackend,
    _codex_env,
    _sandbox_mode_from_options,
    make_approval_handler,
)
from tradewind.application.config import NativeStoreConfig
from tradewind.application.ports import TurnContext
from tradewind.application.tool_host import ToolHost
from tradewind.domain.errors import ConfigError
from tradewind.domain.events import PermissionRequested, TurnStarted
from tradewind.domain.models import ModelSpec, Profile, SessionRow, SubscriptionAuth, Verdict


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

    async def decide(self, tool_name: str, tool_input: dict[str, Any]) -> Verdict:
        self.calls.append((tool_name, tool_input))
        return self.verdict


class _RaisingBroker:
    """A `PermissionBroker` that fails the test if consulted at all -- used
    to prove the fail-closed extraction-failure path never asks it."""

    async def decide(self, tool_name: str, tool_input: dict[str, Any]) -> Verdict:
        raise AssertionError(
            f"broker.decide() must not be called, got ({tool_name!r}, {tool_input!r})"
        )


def _run_handler_on_a_real_loop(
    broker: Any, build_and_call: Any
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
    handler = make_approval_handler(broker, loop, out_queue)

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
    broker = _ScriptedBroker("allow")

    result, _events = _run_handler_on_a_real_loop(
        broker,
        lambda handler: handler("item/fileChange/requestApproval", {"changes": [{"path": "a.py"}]}),
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
        load_history=lambda: [],
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
        out_queue: queue.Queue[object],
        record_native_id: Any,
        record_handle: Any,
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
