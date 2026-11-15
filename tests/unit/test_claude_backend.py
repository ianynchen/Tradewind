"""Tests for `ClaudeBackend` instance behaviour that isn't a pure mapping
function -- `interrupt()`'s handling of a client registered before
`ClaudeSDKClient.connect()` finishes (task-10 fix round 1),
`take_native_session_id()`'s per-session pop semantics (task-11 review, fix
round 1), and structured output (`ctx.output_schema` wired to the SDK's
`output_format`, result added as `TurnResult.structured_output` -- FR-1.3). Pure event mapping lives in `tests/unit/test_claude_mapping.py`;
a real live session is `tests/conformance/test_claude.py` /
`tests/integration/test_claude_live.py` (`@pytest.mark.integration`).
"""

from __future__ import annotations

from typing import Any, cast

import anyio
import pytest
from claude_agent_sdk import CLIConnectionError, ResultMessage

from tradewind.adapters import claude_backend as claude_backend_module
from tradewind.adapters.claude_backend import ClaudeBackend
from tradewind.application.config import NativeStoreConfig
from tradewind.application.ports import TurnContext
from tradewind.application.tool_host import ToolHost
from tradewind.domain.events import ItemCompleted, PermissionRequested, TurnCompleted, TurnFailed
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
    """Satisfies `PermissionBroker` but must never be called -- no scripted
    turn here makes a tool call."""

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
    can_use_tool = _make_can_use_tool(_policy(_Broker()), cast(Any, pending), flag, [None])

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


# --- structured output (FR-1.3): `ctx.output_schema` rides the SDK's native
# `output_format`; the result is ADDED as `TurnResult.structured_output`
# beside the unchanged prose `final_text` (same field on every backend). ---

_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"answer": {"type": "integer"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def test_capabilities_declare_structured_output_support() -> None:
    assert (
        ClaudeBackend(_profile(), NativeStoreConfig()).capabilities().supports_structured_output
        is True
    )


async def test_output_schema_is_passed_to_the_sdk_as_a_json_schema_output_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_fake_sdk_client(
        monkeypatch, [_success_result(structured_output={"answer": 42})]
    )
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    [event async for event in backend.run(_make_ctx(output_schema=_SCHEMA))]

    assert cast(Any, captured[0]).output_format == {"type": "json_schema", "schema": _SCHEMA}


async def test_no_output_schema_sends_no_output_format(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without a schema the CLI must not be told to force structured output.
    captured = _install_fake_sdk_client(monkeypatch, [_success_result()])
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = [event async for event in backend.run(_make_ctx(output_schema=None))]

    assert cast(Any, captured[0]).output_format is None
    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert completed[0].result.final_text == "done"  # plain text turn unchanged
    assert completed[0].result.structured_output is None


async def test_structured_output_is_added_beside_the_prose_final_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-1.3, additive: the model's own text still streams and mirrors as
    in a plain turn, `final_text` stays its prose, and the schema result is
    ADDED as `structured_output`."""
    from claude_agent_sdk import (
        AssistantMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    # What the CLI streams for a schema turn: the model's text, then its
    # call to the CLI's own `StructuredOutput` tool and that tool's result.
    _install_fake_sdk_client(
        monkeypatch,
        [
            AssistantMessage(
                content=[
                    TextBlock(text="Here you go."),
                    ToolUseBlock(id="toolu_so", name="StructuredOutput", input={"answer": 42}),
                ],
                model="claude-sonnet-4-5",
            ),
            UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="toolu_so",
                        content="Structured output provided successfully",
                        is_error=None,
                    )
                ]
            ),
            _success_result(result="Here you go.", structured_output={"answer": 42}),
        ],
    )
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = [event async for event in backend.run(_make_ctx(output_schema=_SCHEMA))]

    # Exactly a plain turn's items: the CLI's StructuredOutput call and its
    # result are engine plumbing, never caller-visible tool events.
    assert [
        (e.message.kind, e.message.content.get("text"))
        for e in events
        if isinstance(e, ItemCompleted)
    ] == [("text", "Here you go.")]
    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert len(completed) == 1
    assert completed[0].result.end_reason == "end_turn"
    assert completed[0].result.final_text == "Here you go."
    assert completed[0].result.structured_output == {"answer": 42}


async def test_clean_finish_without_structured_output_completes_without_a_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner rule (FR-1.3): a schema turn that ends normally without a
    valid result still COMPLETES -- the text is kept, `structured_output`
    is None. (The CLI silently disables structured output when it rejects
    a schema at init, and then ends as an ordinary success.)"""
    _install_fake_sdk_client(
        monkeypatch, [_success_result(result="prose only", structured_output=None)]
    )
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = [event async for event in backend.run(_make_ctx(output_schema=_SCHEMA))]

    assert not any(isinstance(e, TurnFailed) for e in events)
    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert len(completed) == 1
    assert completed[0].result.end_reason == "end_turn"
    assert completed[0].result.final_text == "prose only"
    assert completed[0].result.structured_output is None


async def test_structured_output_retry_exhaustion_completes_without_a_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI's own `error_max_structured_output_retries` error result (no
    attempt produced schema-valid output) is the same situation as above:
    the turn ended without a valid result, so it completes with None and
    keeps any text -- not a TurnFailed."""
    _install_fake_sdk_client(
        monkeypatch,
        [
            _success_result(
                subtype="error_max_structured_output_retries",
                is_error=True,
                result="partial prose",
                terminal_reason="structured_output_retry_exhausted",
                errors=["Output does not match required schema"],
            )
        ],
    )
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = [event async for event in backend.run(_make_ctx(output_schema=_SCHEMA))]

    assert not any(isinstance(e, TurnFailed) for e in events)
    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert len(completed) == 1
    assert completed[0].result.end_reason == "end_turn"
    assert completed[0].result.final_text == "partial prose"
    assert completed[0].result.structured_output is None


async def test_structured_output_tool_is_auto_allowed_without_consulting_the_broker() -> None:
    """The CLI's own `StructuredOutput` tool is plumbing, not a caller tool:
    the broker must never see it and no PermissionRequested may fire."""
    from claude_agent_sdk import PermissionResultAllow

    from tradewind.adapters.claude_backend import _make_can_use_tool

    pending: list[object] = []
    flag = [False]
    can_use_tool = _make_can_use_tool(_policy(_NoBroker()), cast(Any, pending), flag, [None])

    result = await can_use_tool("StructuredOutput", {"answer": 1}, cast(Any, None))

    assert isinstance(result, PermissionResultAllow)
    assert pending == []
    assert flag == [False]


async def test_schema_turn_cap_leaves_room_for_the_structured_output_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`max_tool_rounds=N` must allow N caller-tool rounds on a schema turn
    exactly as on a plain one. The CLI counts its StructuredOutput call as a
    turn, and when the model first ends with plain text it adds a reminder
    and asks once more -- so a schema turn needs `N + 2`, not `N + 1`."""
    captured = _install_fake_sdk_client(
        monkeypatch, [_success_result(structured_output={"answer": 1})]
    )
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    [event async for event in backend.run(_make_ctx(output_schema=_SCHEMA, max_tool_rounds=2))]
    [event async for event in backend.run(_make_ctx(output_schema=_SCHEMA))]

    assert cast(Any, captured[0]).max_turns == 4
    assert cast(Any, captured[1]).max_turns is None  # uncapped stays uncapped


def test_native_transcript_drops_the_structured_output_pair() -> None:
    """Reconcile must not backfill what the live stream hid: the CLI's
    StructuredOutput tool_use/tool_result pair never enters the mirror."""
    from claude_agent_sdk import SessionMessage

    from tradewind.adapters.claude_backend import native_transcript_items

    def entry(entry_type: str, uuid: str, content: object) -> SessionMessage:
        return SessionMessage(
            type=cast(Any, entry_type),
            uuid=uuid,
            session_id="sess-1",
            message={"role": entry_type, "content": content},
        )

    entries = [
        entry(
            "assistant",
            "a-1",
            [
                {"type": "text", "text": "Here you go."},
                {"type": "tool_use", "id": "toolu_so", "name": "StructuredOutput", "input": {}},
            ],
        ),
        entry(
            "user",
            "u-1",
            [{"type": "tool_result", "tool_use_id": "toolu_so", "content": "ok"}],
        ),
    ]

    items = native_transcript_items(entries, after_native_id=None)

    assert [(i.kind, i.content.get("text")) for i in items] == [("text", "Here you go.")]


# --- FR-6.5 on a schema turn: the cap counts CALLER tool rounds exactly.
# The CLI's `max_turns` keeps headroom for the StructuredOutput call, so the
# adapter itself denies (and interrupts) the first caller tool past the cap.
# The fake below plays the CLI: it streams each response's AssistantMessage,
# then asks `can_use_tool` for each tool in it, as the CLI's control
# requests do. ---


class _AllowBroker:
    async def decide(self, _tool_name: str, _tool_input: dict[str, object]) -> Verdict:
        return "allow"


async def _collect_bounded(stream: Any) -> list[object]:
    """Drain `stream` under a 5 s bound. The round cap's wait for its call
    has no deadline by design (it is bounded by the turn), so a regression
    that stops feeding the cap would otherwise hang the suite instead of
    failing it."""
    with anyio.fail_after(5):
        return [event async for event in stream]


def _install_cli_like_client(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[list[tuple[str, str]]],
    *,
    ask_before_streaming: bool = False,
    split_blocks: bool = False,
    texts: dict[int, str] | None = None,
) -> list[object]:
    """`responses`: one list of `(tool_use_id, tool_name)` per model
    response. A response with no tools ends the turn. Records every
    permission result. `ask_before_streaming` sends a response's permission
    request BEFORE its message reaches the stream consumer. `split_blocks`
    streams a response the way the real CLI does: one `AssistantMessage`
    per content block, all sharing the response's `message_id`, each with
    its own `uuid`. `texts` maps a response index to a text block streamed
    ahead of that response's tool calls."""
    import asyncio

    from claude_agent_sdk import (
        AssistantMessage,
        PermissionResultDeny,
        TextBlock,
        ToolPermissionContext,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    decisions: list[object] = []

    class _CliLikeClient:
        def __init__(self, options: Any) -> None:
            self._options = options

        async def connect(self, _prompt: str) -> None:
            pass

        async def receive_response(self) -> Any:
            can_use_tool = self._options.can_use_tool
            for index, tools in enumerate(responses):
                text_blocks: list[Any] = (
                    [TextBlock(text=texts[index])] if texts and index in texts else []
                )
                message = AssistantMessage(
                    content=text_blocks + [ToolUseBlock(id=i, name=n, input={}) for i, n in tools],
                    model="claude-sonnet-4-5",
                    message_id=f"msg-{index}",
                    uuid=f"uuid-{index}",
                )
                if split_blocks and tools:
                    for block_index, (i, n) in enumerate(tools):
                        yield AssistantMessage(
                            content=[ToolUseBlock(id=i, name=n, input={})],
                            model="claude-sonnet-4-5",
                            message_id=f"msg-{index}",
                            uuid=f"uuid-{index}-{block_index}",
                        )
                    results = [
                        await can_use_tool(n, {}, ToolPermissionContext(tool_use_id=i))
                        for i, n in tools
                    ]
                    decisions.extend(results)
                    if any(isinstance(r, PermissionResultDeny) and r.interrupt for r in results):
                        yield _success_result(terminal_reason="aborted_tools", result=None)
                        return
                    yield UserMessage(
                        content=[
                            ToolResultBlock(tool_use_id=i, content="ok", is_error=None)
                            for i, _ in tools
                        ]
                    )
                    continue

                async def ask(tools: list[tuple[str, str]] = tools) -> list[object]:
                    return [
                        await can_use_tool(n, {}, ToolPermissionContext(tool_use_id=i))
                        for i, n in tools
                    ]

                if ask_before_streaming and tools:
                    pending = asyncio.ensure_future(ask())
                    # Let the permission request start (and block) BEFORE
                    # the consumer sees the message holding the call.
                    await asyncio.sleep(0)
                    assert not pending.done()
                    yield message
                    results = await pending
                else:
                    yield message
                    results = await ask() if tools else []
                decisions.extend(results)
                if any(isinstance(r, PermissionResultDeny) and r.interrupt for r in results):
                    yield _success_result(terminal_reason="aborted_tools", result=None)
                    return
                if not tools:
                    break
                yield UserMessage(
                    content=[
                        ToolResultBlock(tool_use_id=i, content="ok", is_error=None)
                        for i, _ in tools
                    ]
                )
            yield _success_result(structured_output={"answer": 1})

        async def disconnect(self) -> None:
            pass

    monkeypatch.setattr(claude_backend_module, "ClaudeSDKClient", _CliLikeClient)
    return decisions


def _capped_schema_ctx(max_tool_rounds: int) -> TurnContext:
    ctx = _make_ctx(output_schema=_SCHEMA, max_tool_rounds=max_tool_rounds)
    ctx.broker = cast(Any, _AllowBroker())
    return ctx


async def test_schema_turn_with_zero_cap_executes_no_caller_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-6.5: `max_tool_rounds=0` means no tool execution, schema or not."""
    from claude_agent_sdk import PermissionResultDeny

    decisions = _install_cli_like_client(
        monkeypatch, [[("t1", "mcp__tradewind__x")], [("so", "StructuredOutput")]]
    )
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = await _collect_bounded(backend.run(_capped_schema_ctx(0)))

    assert len(decisions) == 1
    assert isinstance(decisions[0], PermissionResultDeny)
    assert decisions[0].interrupt is True
    assert not any(isinstance(e, PermissionRequested) for e in events)  # not a broker denial
    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert len(completed) == 1
    assert completed[0].result.end_reason == "max_tool_rounds"
    assert completed[0].result.structured_output is None


async def test_schema_turn_with_cap_one_runs_exactly_one_caller_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    decisions = _install_cli_like_client(
        monkeypatch,
        [
            [("t1", "mcp__tradewind__x"), ("t2", "mcp__tradewind__y")],
            [("t3", "mcp__tradewind__x")],
        ],
    )
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = await _collect_bounded(backend.run(_capped_schema_ctx(1)))

    # Round 1 (both of its calls) runs; round 2's first call is stopped.
    assert [type(d) for d in decisions] == [
        PermissionResultAllow,
        PermissionResultAllow,
        PermissionResultDeny,
    ]
    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert completed[0].result.end_reason == "max_tool_rounds"


async def test_structured_output_call_is_not_a_caller_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cap 1: the model calls StructuredOutput first, then one caller tool.
    Only the caller call is a round, so it is still inside the cap."""
    from claude_agent_sdk import PermissionResultAllow

    decisions = _install_cli_like_client(
        monkeypatch, [[("so", "StructuredOutput")], [("t1", "mcp__tradewind__x")], []]
    )
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = await _collect_bounded(backend.run(_capped_schema_ctx(1)))

    assert [type(d) for d in decisions] == [PermissionResultAllow, PermissionResultAllow]
    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert completed[0].result.end_reason == "end_turn"
    assert completed[0].result.structured_output == {"answer": 1}


async def test_cap_holds_when_the_permission_request_overtakes_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK starts a permission handler as soon as it reads the request,
    possibly before the stream consumer has seen the message that holds
    the call: the check must wait for it, not guess."""
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    decisions = _install_cli_like_client(
        monkeypatch,
        [[("t1", "mcp__tradewind__x")], [("t2", "mcp__tradewind__x")]],
        ask_before_streaming=True,
    )
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = await _collect_bounded(backend.run(_capped_schema_ctx(1)))

    # Round 1 allowed (it waited, it did not guess "over the cap"); round 2 stopped.
    assert [type(d) for d in decisions] == [PermissionResultAllow, PermissionResultDeny]
    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert completed[0].result.end_reason == "max_tool_rounds"


def test_native_transcript_cut_after_the_call_still_drops_its_result() -> None:
    """Reconcile reads from after the last mirrored id -- which can be the
    very message holding the StructuredOutput call. Its result, after the
    cut, must still be recognized and dropped (no orphan tool_result)."""
    from claude_agent_sdk import SessionMessage

    from tradewind.adapters.claude_backend import native_transcript_items

    entries = [
        SessionMessage(
            type="assistant",
            uuid="a-1",
            session_id="sess-1",
            message={
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_so", "name": "StructuredOutput", "input": {}}
                ],
            },
        ),
        SessionMessage(
            type="user",
            uuid="u-1",
            session_id="sess-1",
            message={
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_so", "content": "ok"}],
            },
        ),
    ]

    assert native_transcript_items(entries, after_native_id="a-1") == []


async def test_retry_exhaustion_without_result_text_keeps_the_streamed_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI's error results carry `errors`, often with `result=None`: the
    turn's text must still be kept, from the last assistant text streamed."""
    from claude_agent_sdk import AssistantMessage, TextBlock

    _install_fake_sdk_client(
        monkeypatch,
        [
            AssistantMessage(content=[TextBlock(text="first")], model="claude-sonnet-4-5"),
            AssistantMessage(content=[TextBlock(text="last words")], model="claude-sonnet-4-5"),
            _success_result(
                subtype="error_max_structured_output_retries",
                is_error=True,
                result=None,
                terminal_reason="structured_output_retry_exhausted",
                errors=["no valid output"],
            ),
        ],
    )
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = [event async for event in backend.run(_make_ctx(output_schema=_SCHEMA))]

    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert completed[0].result.final_text == "last words"
    assert completed[0].result.structured_output is None


async def test_structured_output_reminder_in_the_live_stream_is_not_surfaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the model first ends with plain text, the CLI yields an
    `isMeta` user message "[structured-output-enforce] You MUST call the
    StructuredOutput tool ..." -- engine plumbing, never a mirrored item."""
    from claude_agent_sdk import UserMessage

    _install_fake_sdk_client(
        monkeypatch,
        [
            UserMessage(
                content="[structured-output-enforce] You MUST call the StructuredOutput tool "
                "to complete this request. Call this tool now."
            ),
            _success_result(structured_output={"answer": 1}),
        ],
    )
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = [event async for event in backend.run(_make_ctx(output_schema=_SCHEMA))]

    assert not any(isinstance(e, ItemCompleted) for e in events)


async def test_parallel_calls_of_one_response_are_one_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI streams one response as several AssistantMessages (one per
    block) sharing `message_id`, each with its own `uuid`: the round is the
    response, so cap 1 allows both parallel calls."""
    from claude_agent_sdk import PermissionResultAllow

    decisions = _install_cli_like_client(
        monkeypatch,
        [[("t1", "mcp__tradewind__x"), ("t2", "mcp__tradewind__y")], []],
        split_blocks=True,
    )
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = await _collect_bounded(backend.run(_capped_schema_ctx(1)))

    assert [type(d) for d in decisions] == [PermissionResultAllow, PermissionResultAllow]
    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert completed[0].result.end_reason == "end_turn"


async def test_broker_still_decides_calls_inside_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap only refuses calls PAST it; a call inside it still goes to
    the caller's broker, whose denial is a broker denial (event + reason,
    no interrupt), not a cap stop."""
    from claude_agent_sdk import PermissionResultDeny

    from tradewind.domain.models import Denial

    class _DenyBroker:
        async def decide(self, _tool_name: str, _tool_input: dict[str, object]) -> object:
            return Denial(reason="not this one")

    decisions = _install_cli_like_client(monkeypatch, [[("t1", "mcp__tradewind__x")], []])
    ctx = _capped_schema_ctx(1)
    ctx.broker = cast(Any, _DenyBroker())
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = await _collect_bounded(backend.run(ctx))

    assert len(decisions) == 1
    denial = decisions[0]
    assert isinstance(denial, PermissionResultDeny)
    assert denial.message == "not this one"
    assert denial.interrupt is False
    permission = [e for e in events if isinstance(e, PermissionRequested)]
    assert len(permission) == 1
    assert permission[0].verdict == "deny"
    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert completed[0].result.end_reason == "end_turn"


async def test_slow_consumer_never_turns_a_waiting_call_into_a_cap_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`session.stream()`'s consumer paces `_drive_client`: while it is slow,
    a permission request can wait a long time for its message. The wait
    must last as long as the turn does -- no deadline may decide "over the
    cap" for a call inside it. (A lookup deadline under its old name is
    shrunk to 10 ms here, so reintroducing one fails this test fast.)"""
    import asyncio

    from claude_agent_sdk import (
        AssistantMessage,
        PermissionResultAllow,
        TextBlock,
        ToolPermissionContext,
        ToolUseBlock,
    )

    monkeypatch.setattr(claude_backend_module, "_ROUND_LOOKUP_TIMEOUT_S", 0.01, raising=False)
    decisions: list[object] = []

    class _SlowConsumerClient:
        def __init__(self, options: Any) -> None:
            self._options = options

        async def connect(self, _prompt: str) -> None:
            pass

        async def receive_response(self) -> Any:
            ask = asyncio.ensure_future(
                self._options.can_use_tool(
                    "mcp__tradewind__x", {}, ToolPermissionContext(tool_use_id="t1")
                )
            )
            await asyncio.sleep(0)  # the request is in flight before any message
            yield AssistantMessage(content=[TextBlock(text="thinking aloud")], model="m")
            yield AssistantMessage(
                content=[ToolUseBlock(id="t1", name="mcp__tradewind__x", input={})],
                model="m",
                message_id="msg-0",
            )
            decisions.append(await ask)
            yield _success_result(structured_output={"answer": 1})

        async def disconnect(self) -> None:
            pass

    monkeypatch.setattr(claude_backend_module, "ClaudeSDKClient", _SlowConsumerClient)
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events: list[object] = []
    with anyio.fail_after(5):  # a regression that never observes must fail, not hang
        async for event in backend.run(_capped_schema_ctx(1)):
            events.append(event)
            if isinstance(event, ItemCompleted):
                await anyio.sleep(0.1)  # a slow consumer: far past any lookup deadline

    assert [type(d) for d in decisions] == [PermissionResultAllow]
    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert completed[0].result.end_reason == "end_turn"


async def test_call_without_a_tool_use_id_goes_to_the_broker_uncounted() -> None:
    """The wire protocol always sends a `tool_use_id`; if one were ever
    missing, the call cannot be placed in a round. It is not treated as a
    cap stop: the broker decides it, as on a plain turn."""
    from claude_agent_sdk import PermissionResultAllow, ToolPermissionContext

    from tradewind.adapters.claude_backend import _CallerRoundCap, _make_can_use_tool

    asked: list[str] = []

    class _RecordingBroker:
        async def decide(self, tool_name: str, _tool_input: dict[str, object]) -> Verdict:
            asked.append(tool_name)
            return "allow"

    round_cap = _CallerRoundCap(0)
    can_use_tool = _make_can_use_tool(_policy(_RecordingBroker()), [], [False], [None], round_cap)

    result = await can_use_tool("mcp__tradewind__x", {}, ToolPermissionContext(tool_use_id=None))

    assert isinstance(result, PermissionResultAllow)
    assert asked == ["x"]
    assert round_cap.hit is False


# --- FR-6.5 on a PLAIN turn (no schema). The bundled CLI runs response k's
# tools BEFORE its own `max_turns` check, so `max_turns = N + 1` alone let
# `max_tool_rounds=0` still run the first response's tools. The same exact
# caller-round cap now applies whenever a cap is set; `max_turns` stays only
# as a backstop. ---


def _capped_plain_ctx(max_tool_rounds: int | None) -> TurnContext:
    ctx = _make_ctx(output_schema=None, max_tool_rounds=max_tool_rounds)
    ctx.broker = cast(Any, _AllowBroker())
    return ctx


async def test_plain_turn_with_zero_cap_executes_no_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_agent_sdk import PermissionResultDeny

    decisions = _install_cli_like_client(monkeypatch, [[("t1", "mcp__tradewind__x")], []])
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = await _collect_bounded(backend.run(_capped_plain_ctx(0)))

    assert len(decisions) == 1
    assert isinstance(decisions[0], PermissionResultDeny)
    assert decisions[0].interrupt is True
    assert not any(isinstance(e, PermissionRequested) for e in events)
    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert len(completed) == 1
    assert completed[0].result.end_reason == "max_tool_rounds"
    assert completed[0].result.structured_output is None


async def test_plain_turn_with_cap_one_refuses_the_second_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    decisions = _install_cli_like_client(
        monkeypatch, [[("t1", "mcp__tradewind__x")], [("t2", "mcp__tradewind__x")], []]
    )
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = await _collect_bounded(backend.run(_capped_plain_ctx(1)))

    assert [type(d) for d in decisions] == [PermissionResultAllow, PermissionResultDeny]
    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert completed[0].result.end_reason == "max_tool_rounds"


async def test_uncapped_plain_turn_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """No cap: every round goes to the broker and runs, as before."""
    from claude_agent_sdk import PermissionResultAllow

    decisions = _install_cli_like_client(
        monkeypatch, [[("t1", "mcp__tradewind__x")], [("t2", "mcp__tradewind__x")], []]
    )
    captured_options: list[Any] = []
    real_build_options = ClaudeBackend._build_options

    def capturing(self: ClaudeBackend, *args: Any, **kwargs: Any) -> Any:
        options = real_build_options(self, *args, **kwargs)
        captured_options.append(options)
        return options

    monkeypatch.setattr(ClaudeBackend, "_build_options", capturing)
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = await _collect_bounded(backend.run(_capped_plain_ctx(None)))

    assert [type(d) for d in decisions] == [PermissionResultAllow, PermissionResultAllow]
    assert captured_options[0].max_turns is None
    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert completed[0].result.end_reason == "end_turn"


# --- review round 4 ---


async def test_schema_turn_backstop_without_a_cap_hit_is_not_a_false_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cap 0 + schema: invalid StructuredOutput attempts (and the CLI's
    reminder) use CLI turns, so the `max_turns` backstop can fire with ZERO
    caller rounds. That is a schema turn without a valid result (FR-1.3):
    it completes as `end_turn`, `structured_output=None`, text kept -- not
    a false `max_tool_rounds`."""
    from claude_agent_sdk import (
        AssistantMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    def attempt(n: int, text: str) -> list[object]:
        return [
            AssistantMessage(
                content=[TextBlock(text=text)], model="m", message_id=f"msg-{n}", uuid=f"u-{n}a"
            ),
            AssistantMessage(
                content=[ToolUseBlock(id=f"so-{n}", name="StructuredOutput", input={"bad": 1})],
                model="m",
                message_id=f"msg-{n}",
                uuid=f"u-{n}b",
            ),
            UserMessage(
                content=[
                    ToolResultBlock(tool_use_id=f"so-{n}", content="schema mismatch", is_error=True)
                ]
            ),
        ]

    _install_fake_sdk_client(
        monkeypatch,
        [
            *attempt(1, "first try"),
            *attempt(2, "second try"),
            _success_result(
                subtype="error_max_turns",
                is_error=True,
                terminal_reason="max_turns",
                result=None,
            ),
        ],
    )
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = await _collect_bounded(backend.run(_capped_schema_ctx(0)))

    assert not any(isinstance(e, TurnFailed) for e in events)
    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert len(completed) == 1
    assert completed[0].result.end_reason == "end_turn"
    assert completed[0].result.structured_output is None
    assert completed[0].result.final_text == "second try"


async def test_capped_final_text_is_the_final_responses_text_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cap 1: response 1 says something and calls a tool; response 2 only
    calls a tool, which the cap refuses. `final_text` is the FINAL
    response's text -- none here -- not text from an earlier response
    (langchain's `_final_text` semantics)."""
    _install_cli_like_client(
        monkeypatch,
        [[("t1", "mcp__tradewind__x")], [("t2", "mcp__tradewind__x")]],
        texts={0: "let me check"},
    )
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = await _collect_bounded(backend.run(_capped_plain_ctx(1)))

    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert completed[0].result.end_reason == "max_tool_rounds"
    assert completed[0].result.final_text is None


# --- review round 5 ---


async def test_max_turns_result_after_our_cap_stop_stays_max_tool_rounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the CLI reports `max_turns` AFTER our own cap denial, the cap is
    what ended the turn: `max_tool_rounds`, not the schema-backstop
    `end_turn` reading."""
    from claude_agent_sdk import AssistantMessage, ToolPermissionContext, ToolUseBlock

    class _CapThenMaxTurnsClient:
        def __init__(self, options: Any) -> None:
            self._options = options

        async def connect(self, _prompt: str) -> None:
            pass

        async def receive_response(self) -> Any:
            yield AssistantMessage(
                content=[ToolUseBlock(id="t1", name="mcp__tradewind__x", input={})],
                model="m",
                message_id="msg-0",
            )
            await self._options.can_use_tool(
                "mcp__tradewind__x", {}, ToolPermissionContext(tool_use_id="t1")
            )
            yield _success_result(
                subtype="error_max_turns", is_error=True, terminal_reason="max_turns", result=None
            )

        async def disconnect(self) -> None:
            pass

    monkeypatch.setattr(claude_backend_module, "ClaudeSDKClient", _CapThenMaxTurnsClient)
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = await _collect_bounded(backend.run(_capped_schema_ctx(0)))

    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert completed[0].result.end_reason == "max_tool_rounds"


async def test_reminder_path_keeps_the_last_visible_responses_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Response A answers in text; the CLI's reminder follows; response B is
    only an invalid StructuredOutput call (hidden); then the backstop. The
    caller saw A's text as the final reply, so `final_text` keeps it -- a
    hidden SO-only response does not reset it."""
    from claude_agent_sdk import (
        AssistantMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    _install_fake_sdk_client(
        monkeypatch,
        [
            AssistantMessage(
                content=[TextBlock(text="the answer is 42")], model="m", message_id="msg-a"
            ),
            UserMessage(
                content="[structured-output-enforce] You MUST call the StructuredOutput tool "
                "to complete this request. Call this tool now."
            ),
            AssistantMessage(
                content=[ToolUseBlock(id="so-b", name="StructuredOutput", input={"bad": 1})],
                model="m",
                message_id="msg-b",
            ),
            UserMessage(
                content=[ToolResultBlock(tool_use_id="so-b", content="mismatch", is_error=True)]
            ),
            _success_result(
                subtype="error_max_turns", is_error=True, terminal_reason="max_turns", result=None
            ),
        ],
    )
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = await _collect_bounded(backend.run(_capped_schema_ctx(0)))

    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert completed[0].result.end_reason == "end_turn"
    assert completed[0].result.final_text == "the answer is 42"
    assert completed[0].result.structured_output is None


async def test_messages_without_ids_are_one_response_for_final_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With neither `message_id` nor `uuid`, blocks cannot be told apart by
    response: they are treated as the same response, so a later tool-only
    block does not wipe the text before it."""
    from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock

    _install_fake_sdk_client(
        monkeypatch,
        [
            AssistantMessage(content=[TextBlock(text="hello")], model="m"),
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="mcp__tradewind__x", input={})], model="m"
            ),
            _success_result(
                subtype="error_max_structured_output_retries",
                is_error=True,
                result=None,
                terminal_reason="structured_output_retry_exhausted",
                errors=["no valid output"],
            ),
        ],
    )
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = await _collect_bounded(backend.run(_make_ctx(output_schema=_SCHEMA)))

    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert completed[0].result.final_text == "hello"


async def test_block_without_ids_after_an_identified_response_keeps_its_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed ids: response `msg-a` says "x"; then a block with neither
    `message_id` nor `uuid` holds a caller tool. The id-less block cannot be
    placed in a new response, so it does not reset the text -- `final_text`
    stays "x"."""
    from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock

    _install_fake_sdk_client(
        monkeypatch,
        [
            AssistantMessage(content=[TextBlock(text="x")], model="m", message_id="msg-a"),
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="mcp__tradewind__x", input={})], model="m"
            ),
            _success_result(
                subtype="error_max_structured_output_retries",
                is_error=True,
                result=None,
                terminal_reason="structured_output_retry_exhausted",
                errors=["no valid output"],
            ),
        ],
    )
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = await _collect_bounded(backend.run(_make_ctx(output_schema=_SCHEMA)))

    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert completed[0].result.final_text == "x"


# --- turn governance (05-turn-governance §2.1, §2.3, §3.3, §5, §7) ---


def _policy(
    broker: object, *, caller: list[Any] | None = None, emitted: list[object] | None = None
) -> Any:
    """A broker-mode `PolicyGate` the way `turn_runner` builds one."""
    from tradewind.application.policy_gate import PolicyGate
    from tradewind.domain.policy import TurnVars, floor_tradewind

    return PolicyGate(
        broker=cast(Any, broker),
        backend="claude",
        floor=floor_tradewind(),
        caller=caller or [],
        defaults=[],
        turn=TurnVars(
            mode="broker", engine_mode=None, shim_server=None, workspace=None, result_tool=None
        ),
        has_broker=True,
        emit=(emitted if emitted is not None else []).append,
    )


def _caller_rule(name: str, outcome: str, reason: str = "") -> Any:
    from tradewind.domain.policy import Matcher, Rule

    return Rule(
        id=f"caller.{name}.{outcome}",
        kind="caller_tool",
        where={"name": Matcher("equals", name)},
        outcome=cast(Any, outcome),
        reason=reason,
    )


class _RecordingArgsBroker:
    def __init__(self) -> None:
        self.asked: list[tuple[str, dict[str, object]]] = []

    async def decide(self, tool_name: str, tool_input: dict[str, object]) -> Verdict:
        self.asked.append((tool_name, tool_input))
        return "allow"


async def test_can_use_tool_goes_through_policy_with_todays_name() -> None:
    """05 §1.1: existing brokers keep today's bare tool name and arguments;
    the shared vocabulary rides along under `tradewind` only. A caller MCP
    tool (`ToolHost` names it `mcp__<server>__<tool>`) is `source:
    mcp:<server>`; a local `Tool` is `source: local`."""
    from claude_agent_sdk import PermissionResultAllow, ToolPermissionContext

    from tradewind.adapters.claude_backend import _make_can_use_tool

    broker = _RecordingArgsBroker()
    can_use_tool = _make_can_use_tool(_policy(broker), [], [False], [None])
    context = ToolPermissionContext(tool_use_id="t1")

    local = await can_use_tool("mcp__tradewind__probe", {"q": 1}, context)
    remote = await can_use_tool("mcp__tradewind__mcp__srv__lookup", {}, context)

    assert isinstance(local, PermissionResultAllow)
    assert isinstance(remote, PermissionResultAllow)
    (name, args), (remote_name, remote_args) = broker.asked
    assert name == "probe"
    assert args["q"] == 1
    action = cast("dict[str, Any]", args["tradewind"])
    assert action["kind"] == "caller_tool"
    assert action["fields"] == {"name": "probe", "args": {"q": 1}, "source": "local"}
    assert remote_name == "mcp__srv__lookup"
    assert cast("dict[str, Any]", remote_args["tradewind"])["fields"]["source"] == "mcp:srv"


async def test_caller_deny_rule_denies_without_asking_the_broker() -> None:
    from claude_agent_sdk import PermissionResultDeny, ToolPermissionContext

    from tradewind.adapters.claude_backend import _make_can_use_tool

    broker = _RecordingArgsBroker()
    pending: list[object] = []
    policy = _policy(broker, caller=[_caller_rule("merge", "deny", "never merge")])
    can_use_tool = _make_can_use_tool(policy, cast(Any, pending), [False], [None])

    result = await can_use_tool(
        "mcp__tradewind__merge", {}, ToolPermissionContext(tool_use_id="t1")
    )

    assert isinstance(result, PermissionResultDeny)
    assert result.message == "never merge"
    assert result.interrupt is False
    assert broker.asked == []
    assert [type(e) for e in pending] == [PermissionRequested]


async def test_structured_output_pass_through() -> None:
    """05 §2.3: the result tool is a structural pass-through -- allowed and
    never asked, even when a caller rule names it, and still hidden."""
    from claude_agent_sdk import PermissionResultAllow, ToolPermissionContext

    from tradewind.adapters.claude_backend import _make_can_use_tool

    pending: list[object] = []
    policy = _policy(_NoBroker(), caller=[_caller_rule("StructuredOutput", "stop")])
    can_use_tool = _make_can_use_tool(policy, cast(Any, pending), [False], [None])

    result = await can_use_tool(
        "StructuredOutput", {"answer": 1}, ToolPermissionContext(tool_use_id="so")
    )

    assert isinstance(result, PermissionResultAllow)
    assert pending == []


async def test_policy_stop_interrupts_and_fails_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """05 §2.6: `stop` cancels and ends the turn with `TurnFailed(reason)`;
    an adapter-started cancel is never reported as a caller interrupt."""
    from claude_agent_sdk import PermissionResultDeny

    decisions = _install_cli_like_client(monkeypatch, [[("t1", "mcp__tradewind__drop_db")], []])
    ctx = _make_ctx(output_schema=None)
    ctx.policy = _policy(_NoBroker(), caller=[_caller_rule("drop_db", "stop", "dangerous")])
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    events = await _collect_bounded(backend.run(ctx))

    assert isinstance(decisions[0], PermissionResultDeny)
    assert decisions[0].interrupt is True
    assert isinstance(events[-1], TurnFailed)
    assert events[-1].error == "dangerous"
    assert not any(isinstance(e, TurnCompleted) for e in events)


def _plain_turn_script() -> list[object]:
    from claude_agent_sdk import (
        AssistantMessage,
        TextBlock,
        ThinkingBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    return [
        AssistantMessage(
            content=[ThinkingBlock(thinking="hmm", signature="sig")], model="m", uuid="a0"
        ),
        AssistantMessage(content=[TextBlock(text="checking")], model="m", uuid="a1"),
        AssistantMessage(
            content=[ToolUseBlock(id="t1", name="mcp__tradewind__probe", input={"q": 1})],
            model="m",
            uuid="a2",
        ),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="42")], uuid="u1"),
        AssistantMessage(content=[TextBlock(text="it is 42")], model="m", uuid="a3"),
        _success_result(result="it is 42"),
    ]


def _with_stream_events(script: list[object]) -> list[object]:
    from claude_agent_sdk import StreamEvent

    interleaved: list[object] = []
    for index, message in enumerate(script):
        interleaved.append(
            StreamEvent(uuid=f"e{index}", session_id="s", event={"type": "content_block_delta"})
        )
        interleaved.append(message)
    return interleaved


async def test_plain_turn_events_identical_with_partial_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """05 §7 / acceptance: `include_partial_messages` adds `StreamEvent`s
    that only feed model activity. The baseline is today's stream (no
    `StreamEvent`s, as before the flag); the same turn with them
    interleaved must yield exactly the same events -- and so the same
    mirror, which is the `ItemCompleted` events."""
    from tradewind.application.supervisor import WorkSupervisor
    from tradewind.domain.supervision import Limits

    backend = ClaudeBackend(_profile(), NativeStoreConfig())
    ctx = _make_ctx(output_schema=None)
    ctx.broker = cast(Any, _AllowBroker())

    _install_fake_sdk_client(monkeypatch, _plain_turn_script())
    baseline = [event async for event in backend.run(ctx)]

    captured = _install_fake_sdk_client(monkeypatch, _with_stream_events(_plain_turn_script()))
    work = WorkSupervisor(Limits(300.0, 1800.0, None))
    ctx.work = work
    with_partials = await _collect_bounded(backend.run(ctx))

    assert cast(Any, captured[0]).include_partial_messages is True
    assert with_partials == baseline
    assert [type(e).__name__ for e in baseline] == [
        "TurnStarted",
        "ItemCompleted",
        "ItemCompleted",
        "ItemCompleted",
        "ItemCompleted",
        "ItemCompleted",
        "TurnCompleted",
    ]
    assert work.open_items() == []


async def test_engine_auto_keeps_the_sdk_default_permission_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task-8 finding (RUNBOOK): the SDK's `auto` mode lets a model
    classifier approve tool calls without `can_use_tool` (types.py
    `can_use_tool` docstring; client.py `set_permission_mode`), which would
    bypass the policy gate. So engine_auto never sets an SDK mode on
    Claude: built-ins stay off, every caller tool still reaches
    `can_use_tool`, and only the gate's mode default changes."""
    captured = _install_fake_sdk_client(monkeypatch, [_success_result()])
    ctx = _make_ctx(output_schema=None)
    ctx.session.options_snapshot["permission_mode"] = "engine_auto"
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    [event async for event in backend.run(ctx)]

    options = cast(Any, captured[0])
    assert options.permission_mode is None
    assert options.tools == []
    assert options.can_use_tool is not None
    assert backend.capabilities().supports_work_supervisor is True


async def test_slash_prompt_reaches_the_engine_as_a_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """05 §5: the engine gets `Message: /clear`; the context (and so the
    runner's mirror) keeps the caller's original prompt."""
    prompts: list[str] = []

    class _PromptClient:
        def __init__(self, _options: object) -> None:
            pass

        async def connect(self, prompt: str) -> None:
            prompts.append(prompt)

        async def receive_response(self) -> Any:
            yield _success_result()

        async def disconnect(self) -> None:
            pass

    monkeypatch.setattr(claude_backend_module, "ClaudeSDKClient", _PromptClient)
    ctx = _make_ctx(output_schema=None)
    ctx.prompt = "/clear"
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    [event async for event in backend.run(ctx)]

    assert prompts == ["Message: /clear"]
    assert ctx.prompt == "/clear"


def _install_background_task_client(
    monkeypatch: pytest.MonkeyPatch, after_result: list[object]
) -> None:
    """The turn's response starts a background task and finishes; the task's
    end (and anything else) arrives only after the `ResultMessage`, through
    `receive_messages()`."""
    from claude_agent_sdk import TaskStartedMessage

    class _BackgroundClient:
        def __init__(self, _options: object) -> None:
            pass

        async def connect(self, _prompt: str) -> None:
            pass

        async def receive_response(self) -> Any:
            yield TaskStartedMessage(
                subtype="task_started",
                data={},
                task_id="bg1",
                description="long build",
                uuid="u-start",
                session_id="s",
            )
            yield _success_result(result="started the build")

        async def receive_messages(self) -> Any:
            for message in after_result:
                await anyio.sleep(0)
                yield message
            await anyio.sleep_forever()

        async def interrupt(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

    monkeypatch.setattr(claude_backend_module, "ClaudeSDKClient", _BackgroundClient)


async def test_open_background_task_holds_the_terminal_until_it_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """05 §3.3: the turn ends when the engine is done AND no work is open.
    The adapter keeps reading the engine (mirroring what arrives) and
    yields its terminal only once the supervisor settles."""
    import asyncio

    from claude_agent_sdk import AssistantMessage, TaskNotificationMessage, TextBlock

    from tradewind.application.supervisor import WorkSupervisor
    from tradewind.domain.supervision import Limits

    _install_background_task_client(
        monkeypatch,
        [
            AssistantMessage(content=[TextBlock(text="build done")], model="m", uuid="late"),
            TaskNotificationMessage(
                subtype="task_notification",
                data={},
                task_id="bg1",
                status="completed",
                output_file="/tmp/o",
                summary="ok",
                uuid="u-end",
                session_id="s",
            ),
        ],
    )
    work = WorkSupervisor(Limits(300.0, 1800.0, None))
    ctx = _make_ctx(output_schema=None)
    ctx.work = work

    async def on_fire(*_: object) -> None:
        raise AssertionError("no limit may fire")

    supervising = asyncio.ensure_future(work.run(on_fire))
    events = await _collect_bounded(ClaudeBackend(_profile(), NativeStoreConfig()).run(ctx))
    supervising.cancel()

    texts = [e.message.content.get("text") for e in events if isinstance(e, ItemCompleted)]
    assert texts == ["build done"]
    assert isinstance(events[-1], TurnCompleted)
    assert events[-1].result.final_text == "started the build"
    assert work.open_items() == []


async def test_unsettled_turn_leaves_the_terminal_to_the_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the supervisor ends without a normal end (a fired limit, or the
    runner cancelling it), `settled()` is False and the adapter yields no
    terminal: the runner's timeout path records the turn."""
    import asyncio

    from tradewind.application.supervisor import WorkSupervisor
    from tradewind.domain.supervision import Limits

    _install_background_task_client(monkeypatch, [])
    work = WorkSupervisor(Limits(300.0, 1800.0, None))
    ctx = _make_ctx(output_schema=None)
    ctx.work = work

    async def on_fire(*_: object) -> None:
        pass

    supervising = asyncio.ensure_future(work.run(on_fire))

    async def fire_soon() -> None:
        await asyncio.sleep(0.05)
        supervising.cancel()

    firing = asyncio.ensure_future(fire_soon())
    events = await _collect_bounded(ClaudeBackend(_profile(), NativeStoreConfig()).run(ctx))
    await firing

    assert not any(isinstance(e, TurnCompleted | TurnFailed) for e in events)


async def test_interrupt_while_settling_ends_without_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller stop while the adapter waits for open work ends the turn
    with no terminal (`Backend.run` contract: the runner records
    `interrupted`), even though the supervisor itself never settles."""
    import asyncio

    from tradewind.application.supervisor import WorkSupervisor
    from tradewind.domain.supervision import Limits

    _install_background_task_client(monkeypatch, [])
    work = WorkSupervisor(Limits(300.0, 1800.0, None))
    ctx = _make_ctx(output_schema=None)
    ctx.work = work
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        await backend.interrupt("s1")

    stopping = asyncio.ensure_future(stop_soon())
    events = await _collect_bounded(backend.run(ctx))
    await stopping

    assert not any(isinstance(e, TurnCompleted | TurnFailed) for e in events)


# --- review fixes: settle edge cases and PermissionRequested on stop ---


class _SettledAtOnceWork:
    """A `ctx.work` with one open background item whose `settled()` answers
    True at once -- in the same loop step as the first late message."""

    def __init__(self) -> None:
        from tradewind.domain.supervision import WorkItem

        self.signals: list[str] = []
        self._open = [WorkItem("bg1", "background", "build", True)]

    def open_items(self) -> list[Any]:
        return list(self._open)

    async def settled(self) -> bool:
        return True

    def __getattr__(self, name: str) -> Any:
        def signal(*_: object) -> None:
            self.signals.append(name)

        return signal


async def test_message_read_in_the_settling_step_is_still_mirrored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `settled()` and the next engine message finish in the same step,
    the message is still reported and mirrored before the terminal."""
    from claude_agent_sdk import AssistantMessage, TextBlock

    class _ImmediateClient:
        def __init__(self, _options: object) -> None:
            pass

        async def connect(self, _prompt: str) -> None:
            pass

        async def receive_response(self) -> Any:
            yield _success_result(result="started")

        async def receive_messages(self) -> Any:
            yield AssistantMessage(content=[TextBlock(text="late text")], model="m", uuid="late")
            await anyio.sleep_forever()

        async def disconnect(self) -> None:
            pass

    monkeypatch.setattr(claude_backend_module, "ClaudeSDKClient", _ImmediateClient)
    ctx = _make_ctx(output_schema=None)
    work = _SettledAtOnceWork()
    ctx.work = cast(Any, work)

    events = await _collect_bounded(ClaudeBackend(_profile(), NativeStoreConfig()).run(ctx))

    texts = [e.message.content.get("text") for e in events if isinstance(e, ItemCompleted)]
    assert texts == ["late text"]
    assert isinstance(events[-1], TurnCompleted)


async def test_policy_stop_reports_the_stopped_call_as_a_denial() -> None:
    """FR-4.1, as on Codex's shim path: a stopped call is reported as a
    deny `PermissionRequested` naming the reason."""
    from claude_agent_sdk import PermissionResultDeny, ToolPermissionContext

    from tradewind.adapters.claude_backend import _make_can_use_tool

    pending: list[object] = []
    stop_reason: list[str | None] = [None]
    policy = _policy(_NoBroker(), caller=[_caller_rule("drop_db", "stop", "dangerous")])
    can_use_tool = _make_can_use_tool(policy, cast(Any, pending), [False], stop_reason)

    result = await can_use_tool(
        "mcp__tradewind__drop_db", {"t": "x"}, ToolPermissionContext(tool_use_id="t1")
    )

    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is True
    assert stop_reason == ["dangerous"]
    [event] = pending
    assert isinstance(event, PermissionRequested)
    assert (event.tool_name, event.verdict, event.reason) == ("drop_db", "deny", "dangerous")


def _open_tool_then(result: ResultMessage, *, background: bool = False) -> list[object]:
    from claude_agent_sdk import AssistantMessage, TaskStartedMessage, ToolUseBlock

    script: list[object] = [
        AssistantMessage(
            content=[ToolUseBlock(id="t1", name="mcp__tradewind__x", input={})],
            model="m",
            uuid="a1",
        )
    ]
    if background:
        script.append(
            TaskStartedMessage(
                subtype="task_started",
                data={},
                task_id="bg1",
                description="build",
                uuid="u1",
                session_id="s",
            )
        )
    return [*script, result]


@pytest.mark.parametrize("background", [False, True])
async def test_error_result_fails_at_once_whatever_is_open(
    monkeypatch: pytest.MonkeyPatch, background: bool
) -> None:
    """A failed engine result is never held for open work: the error text
    reaches the caller at once instead of a work-silence timeout."""
    from tradewind.application.supervisor import WorkSupervisor
    from tradewind.domain.supervision import Limits

    failed = _success_result(subtype="error_during_execution", is_error=True, errors=["boom"])
    _install_fake_sdk_client(monkeypatch, _open_tool_then(failed, background=background))
    ctx = _make_ctx(output_schema=None)
    ctx.broker = cast(Any, _AllowBroker())
    ctx.work = WorkSupervisor(Limits(300.0, 1800.0, None))  # never run: settle would hang

    events = await _collect_bounded(ClaudeBackend(_profile(), NativeStoreConfig()).run(ctx))

    assert isinstance(events[-1], TurnFailed)
    assert events[-1].error == "boom"


async def test_open_plain_tool_item_does_not_hold_the_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only background or subagent work holds a finished turn (05 §3.3); a
    tool item the engine left open does not."""
    from tradewind.application.supervisor import WorkSupervisor
    from tradewind.domain.supervision import Limits

    _install_fake_sdk_client(monkeypatch, _open_tool_then(_success_result()))
    ctx = _make_ctx(output_schema=None)
    ctx.broker = cast(Any, _AllowBroker())
    ctx.work = WorkSupervisor(Limits(300.0, 1800.0, None))  # never run: settle would hang

    events = await _collect_bounded(ClaudeBackend(_profile(), NativeStoreConfig()).run(ctx))

    assert isinstance(events[-1], TurnCompleted)
