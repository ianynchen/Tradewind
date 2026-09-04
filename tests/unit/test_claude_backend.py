"""Tests for `ClaudeBackend` instance behaviour that isn't a pure mapping
function -- `interrupt()`'s handling of a client registered before
`ClaudeSDKClient.connect()` finishes (task-10 fix round 1),
`take_native_session_id()`'s per-session pop semantics (task-11 review, fix
round 1), and `run()`'s `output_schema` capability guard (task-15 fix
round 1: previously untested here, though the guard itself dates to
task-10). Pure event mapping lives in `tests/unit/test_claude_mapping.py`;
a real live session is `tests/conformance/test_claude.py` /
`tests/integration/test_claude_live.py` (`@pytest.mark.integration`).
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from claude_agent_sdk import CLIConnectionError, ResultMessage

from tradewind.adapters import claude_backend as claude_backend_module
from tradewind.adapters.claude_backend import ClaudeBackend
from tradewind.application.config import NativeStoreConfig
from tradewind.application.ports import TurnContext
from tradewind.application.tool_host import ToolHost
from tradewind.domain.errors import Unsupported
from tradewind.domain.events import ItemCompleted, TurnCompleted, TurnFailed
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
        backend="claude",
        auth=SubscriptionAuth(),
        models={"standard": ModelSpec(model="claude-sonnet-4-5")},
    )


class _NoBroker:
    """Satisfies `PermissionBroker` but must never be called -- the
    `output_schema` guard raises before `ctx.broker` is ever touched."""

    async def decide(self, tool_name: str, _tool_input: dict[str, object]) -> Verdict:
        raise AssertionError(f"broker.decide should not be called (got {tool_name!r})")


def _history_loader(messages: list[StoredMessage]):
    """`TurnContext.load_history` is async since FR-9.3 (lazy, awaited by
    mirror-rebuilding backends); tests hand it a pre-baked list."""

    async def load() -> list[StoredMessage]:
        return messages

    return load


def _make_ctx(
    *, output_schema: dict[str, object] | None, max_tool_rounds: int | None = None
) -> TurnContext:
    return TurnContext(
        session=SessionRow(
            session_id="s1", backend="claude", profile="default", options_snapshot={}
        ),
        turn_id="turn-1",
        prompt="hello",
        model_spec=ModelSpec(model="claude-sonnet-4-5"),
        system_prompt=None,
        output_schema=output_schema,
        tools=ToolHost([], [], lambda ref: ref),
        broker=cast(Any, _NoBroker()),
        load_history=_history_loader([]),
        max_tool_rounds=max_tool_rounds,
    )


# --- run(): output_schema capability guard (mirrors supports_structured_
# output=False; same test shape as test_langchain_adapter.py's own) ------


async def test_run_with_output_schema_raises_unsupported_before_turn_started() -> None:
    backend = ClaudeBackend(_profile(), NativeStoreConfig())
    ctx = _make_ctx(output_schema={"type": "object", "properties": {}})

    events: list[object] = []
    with pytest.raises(Unsupported):
        async for event in backend.run(ctx):
            events.append(event)

    # No TurnStarted (or anything else, and in particular no ClaudeSDKClient
    # construction attempt) happened before the raise.
    assert events == []


class _RaisesConnectionErrorClient:
    """Stands in for a `ClaudeSDKClient` registered in `_clients` before its
    `connect()` has resolved: `ClaudeSDKClient.interrupt()` raises
    `CLIConnectionError` in exactly that state (`client.py`: "Not connected.
    Call connect() first.")."""

    async def interrupt(self) -> None:
        raise CLIConnectionError("Not connected. Call connect() first.")


async def test_interrupt_is_a_noop_when_no_client_is_registered() -> None:
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    await backend.interrupt("no-such-session")  # must not raise


async def test_interrupt_swallows_cliconnectionerror_from_a_not_yet_connected_client() -> None:
    backend = ClaudeBackend(_profile(), NativeStoreConfig())
    backend._clients["sess-1"] = cast(Any, _RaisesConnectionErrorClient())

    await backend.interrupt("sess-1")  # must not raise


# --- take_native_session_id: per-session pop, not a shared attribute
# (task-11 review, fix round 1: a single shared `last_native_session_id`
# attribute let one session's native id rehome ANOTHER session sharing the
# same cached-per-profile backend instance) ---


def test_take_native_session_id_pops_and_second_call_returns_none() -> None:
    backend = ClaudeBackend(_profile(), NativeStoreConfig())
    backend._native_ids["sess-1"] = "native-abc"

    first = backend.take_native_session_id("sess-1")
    second = backend.take_native_session_id("sess-1")

    assert first == "native-abc"
    assert second is None


def test_take_native_session_id_is_scoped_per_session() -> None:
    # One `ClaudeBackend` instance is cached and reused across every
    # session on its profile (`Tradewind._resolve_backend`) -- a value
    # recorded for one session_id must never be handed back for another's.
    backend = ClaudeBackend(_profile(), NativeStoreConfig())
    backend._native_ids["sess-a"] = "native-a"

    assert backend.take_native_session_id("sess-b") is None
    assert backend.take_native_session_id("sess-a") == "native-a"


def test_take_native_session_id_returns_none_when_nothing_recorded() -> None:
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    assert backend.take_native_session_id("no-such-session") is None


# --- max_tool_rounds (FR-6.5): rounds->turns mapping into the SDK, and the
# honest completion when the CLI's cap stops the loop. Exercised through a
# fake `ClaudeSDKClient` at the real SDK boundary (options in, messages
# out), not by calling private builders directly. ---


def _success_result(**overrides: object) -> ResultMessage:
    base: dict[str, object] = {
        "subtype": "success",
        "duration_ms": 100,
        "duration_api_ms": 90,
        "is_error": False,
        "num_turns": 1,
        "session_id": "native-sess-1",
        "result": "done",
    }
    base.update(overrides)
    return ResultMessage(**base)  # type: ignore[arg-type]


def _install_fake_sdk_client(
    monkeypatch: pytest.MonkeyPatch, messages: list[object]
) -> list[object]:
    """Replace `claude_backend.ClaudeSDKClient` with a fake that records the
    options it was constructed with (appended to the returned list) and
    replays `messages` from `receive_response()`."""
    captured: list[object] = []

    class _FakeSDKClient:
        def __init__(self, options: object) -> None:
            captured.append(options)

        async def connect(self, _prompt: str) -> None:
            pass

        async def receive_response(self) -> Any:
            for message in messages:
                yield message

        async def disconnect(self) -> None:
            pass

    monkeypatch.setattr(claude_backend_module, "ClaudeSDKClient", _FakeSDKClient)
    return captured


async def test_max_tool_rounds_maps_to_sdk_max_turns_plus_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # N tool rounds take N+1 assistant responses (each round's response plus
    # the final one), and the CLI's `max_turns` counts assistant responses.
    captured = _install_fake_sdk_client(monkeypatch, [_success_result()])
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    [event async for event in backend.run(_make_ctx(output_schema=None, max_tool_rounds=2))]

    assert len(captured) == 1
    assert cast(Any, captured[0]).max_turns == 3


async def test_uncapped_turn_passes_no_max_turns_to_the_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_fake_sdk_client(monkeypatch, [_success_result()])
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    [event async for event in backend.run(_make_ctx(output_schema=None))]

    assert cast(Any, captured[0]).max_turns is None


async def test_max_turns_error_result_completes_with_end_reason_max_tool_rounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI flags its own `max_turns` stop as an error result, but that
    cap only exists because the caller set `max_tool_rounds` -- reporting it
    as `TurnFailed` would turn the caller's own requested cap into a
    failure. It completes honestly as a `max_tool_rounds` partial."""
    capped = _success_result(
        subtype="error_max_turns", is_error=True, terminal_reason="max_turns", result=None
    )
    _install_fake_sdk_client(monkeypatch, [capped])
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = [
        event async for event in backend.run(_make_ctx(output_schema=None, max_tool_rounds=1))
    ]

    assert not any(isinstance(e, TurnFailed) for e in events)
    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert len(completed) == 1
    assert completed[0].result.status == "completed"
    assert completed[0].result.end_reason == "max_tool_rounds"


async def test_ordinary_error_result_still_fails_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only the caller's own cap is reclassified as completion -- a genuine
    # failure keeps failing.
    _install_fake_sdk_client(
        monkeypatch,
        [
            _success_result(
                subtype="error_during_execution",
                is_error=True,
                result=None,
                errors=["boom"],
            )
        ],
    )
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = [event async for event in backend.run(_make_ctx(output_schema=None))]

    assert not any(isinstance(e, TurnCompleted) for e in events)
    assert any(isinstance(e, TurnFailed) for e in events)


def test_capabilities_declare_tool_round_cap_support() -> None:
    assert (
        ClaudeBackend(_profile(), NativeStoreConfig()).capabilities().supports_tool_round_cap
        is True
    )


# --- FR-6.6 pre-turn connect retry ---


def _install_flaky_connect_client(
    monkeypatch: pytest.MonkeyPatch, messages: list[object], connect_failures: int
) -> list[object]:
    captured: list[object] = []
    failures = [connect_failures]

    class _FlakyConnectClient:
        def __init__(self, options: object) -> None:
            captured.append(options)

        async def connect(self, _prompt: str) -> None:
            if failures[0] > 0:
                failures[0] -= 1
                raise ConnectionError("spawn failed")

        async def receive_response(self) -> Any:
            for message in messages:
                yield message

        async def disconnect(self) -> None:
            pass

    monkeypatch.setattr(claude_backend_module, "ClaudeSDKClient", _FlakyConnectClient)
    return captured


def _retry_ctx(**kwargs: Any) -> TurnContext:
    from tradewind.application.config import RetrySettings

    ctx = _make_ctx(output_schema=None, **kwargs)
    ctx.retry = RetrySettings(max_attempts=2, base_delay_s=0.001)
    return ctx


async def test_connect_failure_is_retried_pre_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_flaky_connect_client(monkeypatch, [_success_result()], connect_failures=1)
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = [event async for event in backend.run(_retry_ctx())]

    assert any(isinstance(e, TurnCompleted) for e in events)
    notices = [
        e.message.content
        for e in events
        if isinstance(e, ItemCompleted) and e.message.content.get("type") == "retry_scheduled"
    ]
    assert len(notices) == 1
    assert notices[0]["phase"] == "connect"


async def test_connect_retry_exhaustion_fails_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_flaky_connect_client(monkeypatch, [_success_result()], connect_failures=5)
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = [event async for event in backend.run(_retry_ctx())]

    failed = [e for e in events if isinstance(e, TurnFailed)]
    assert len(failed) == 1
    assert "spawn failed" in failed[0].error


# --- broker verdict enrichment (FR-4.4): native reason + native terminate ---


async def test_can_use_tool_maps_denial_reason_and_terminate_natively() -> None:
    from claude_agent_sdk import PermissionResultDeny

    from tradewind.adapters.claude_backend import _make_can_use_tool
    from tradewind.domain.events import PermissionRequested
    from tradewind.domain.models import Denial

    class _Broker:
        async def decide(self, _tool_name: str, _tool_input: dict[str, object]) -> object:
            return Denial(reason="not on my watch", terminate=True)

    pending: list[object] = []
    flag = [False]
    can_use_tool = _make_can_use_tool(cast(Any, _Broker()), cast(Any, pending), flag)

    result = await can_use_tool("mcp__tradewind__probe", {"q": 1}, cast(Any, None))

    assert isinstance(result, PermissionResultDeny)
    assert result.message == "not on my watch"  # native reason channel
    assert result.interrupt is True  # native terminate
    assert flag == [True]
    event = pending[0]
    assert isinstance(event, PermissionRequested)
    assert event.reason == "not on my watch"


async def test_broker_terminated_abort_completes_honestly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The aborted ResultMessage that OUR PermissionResultDeny(interrupt=True)
    # caused must become a broker_terminated completion, not the generic
    # interrupted synthesis. Driven through run() with the terminate flag
    # forced via a broker that denies-with-terminate is not reachable on the
    # fake client (no tool calls flow), so the flag half is covered above
    # and the reclassification half here via _drive_client directly.
    backend = ClaudeBackend(_profile(), NativeStoreConfig())
    aborted = _success_result(terminal_reason="aborted_streaming", result=None)
    captured = _install_fake_sdk_client(monkeypatch, [aborted])
    del captured

    class _FakeClient:
        async def connect(self, _prompt: str) -> None:
            pass

        async def receive_response(self) -> Any:
            yield aborted

    events = [
        event
        async for event in backend._drive_client(
            cast(Any, _FakeClient()), _make_ctx(output_schema=None), "hello", [], [True]
        )
    ]

    assert len(events) == 1
    completed = events[0]
    assert isinstance(completed, TurnCompleted)
    assert completed.result.status == "completed"
    assert completed.result.end_reason == "broker_terminated"


def test_capabilities_declare_native_deny_reason() -> None:
    assert (
        ClaudeBackend(_profile(), NativeStoreConfig()).capabilities().supports_deny_reason is True
    )


# --- REPLAY (FR-6.1, Phase 4): real probe + preamble injection ---


async def test_probe_native_checks_the_local_transcript_for_real(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = ClaudeBackend(_profile(), NativeStoreConfig())
    row = SessionRow(
        session_id="s1",
        backend="claude",
        profile="default",
        options_snapshot={},
        native_session_id="native-1",
    )

    monkeypatch.setattr(claude_backend_module, "get_session_messages", lambda *_: [object()])
    assert await backend.probe_native(row) is True

    monkeypatch.setattr(claude_backend_module, "get_session_messages", lambda *_: [])
    assert await backend.probe_native(row) is False  # empty == nothing to resume

    def _raises(*_: object) -> list[object]:
        raise FileNotFoundError("reaped")

    monkeypatch.setattr(claude_backend_module, "get_session_messages", _raises)
    assert await backend.probe_native(row) is False

    row_no_id = SessionRow(
        session_id="s1", backend="claude", profile="default", options_snapshot={}
    )
    assert await backend.probe_native(row_no_id) is False


async def test_forced_replay_injects_rendered_mirror_into_a_fresh_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tradewind.domain.models import StoredMessage

    connected_prompts: list[str] = []
    captured = _install_fake_sdk_client(monkeypatch, [_success_result()])

    real_client_cls = claude_backend_module.ClaudeSDKClient

    class _PromptCapturing(real_client_cls):  # type: ignore[misc,valid-type]
        async def connect(self, prompt: str) -> None:
            connected_prompts.append(prompt)

    monkeypatch.setattr(claude_backend_module, "ClaudeSDKClient", _PromptCapturing)

    async def load_replay() -> list[StoredMessage]:
        return [
            StoredMessage(
                role="user",
                kind="text",
                content={"text": "the codeword is MOSSPETAL"},
                seq=1,
                session_id="s1",
                created_at="t",
            )
        ]

    ctx = _make_ctx(output_schema=None)
    ctx.force_replay = True
    ctx.load_replay_history = load_replay
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = [event async for event in backend.run(ctx)]

    assert any(isinstance(e, TurnCompleted) for e in events)
    degraded = [
        e
        for e in events
        if isinstance(e, ItemCompleted) and e.message.content.get("type") == "resume_degraded"
    ]
    assert len(degraded) == 1
    assert len(connected_prompts) == 1
    assert "MOSSPETAL" in connected_prompts[0]  # rendered mirror preamble
    assert connected_prompts[0].endswith("hello")  # caller's prompt last
    assert cast(Any, captured[0]).resume is None  # fresh native session


# --- engine compaction observability (FR-5.8): the PreCompact hook ---


async def test_precompact_hook_records_the_engines_compaction() -> None:
    """Claude's hook fires BEFORE the engine compacts, so no summary text
    exists yet -- the record honestly carries only the trigger."""
    pending: list[object] = []
    hook = claude_backend_module._make_precompact_hook(cast(Any, pending))

    result = await hook(
        cast(
            Any, {"hook_event_name": "PreCompact", "trigger": "auto", "custom_instructions": None}
        ),
        None,
        cast(Any, {}),
    )

    assert result == {}  # observes only; never steers the CLI
    assert len(pending) == 1
    item = cast(ItemCompleted, pending[0])
    assert item.message.kind == "compaction"
    assert item.message.content == {"trigger": "auto"}
    assert item.message.role == "assistant"


async def test_precompact_hook_is_registered_on_the_sdk_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_fake_sdk_client(monkeypatch, [_success_result()])
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    [event async for event in backend.run(_make_ctx(output_schema=None))]

    hooks = cast(Any, captured[0]).hooks
    assert hooks is not None and "PreCompact" in hooks
