"""Tests for tradewind.adapters.langchain_backend.LangchainBackend: the
broker-gated tool loop, mirror rebuild (thinking omitted, tool blocks
reconstructed), and interrupt (task-8 brief, three required scenarios).

Drives the adapter through fake `BaseChatModel`s (never the real Anthropic
API) so these stay fast, hermetic unit tests; the model boundary is the
only thing faked.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import anyio
import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatResult
from langchain_core.runnables import Runnable
from pydantic import ConfigDict, Field

from tradewind.adapters.langchain_backend import LangchainBackend
from tradewind.application.config import NativeStoreConfig
from tradewind.application.ports import TurnContext
from tradewind.application.tool_host import ToolHost
from tradewind.domain.errors import Unsupported
from tradewind.domain.events import (
    ItemCompleted,
    PermissionRequested,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from tradewind.domain.models import (
    ApiKeyAuth,
    ModelSpec,
    Profile,
    SessionRow,
    StoredMessage,
    Tool,
    Verdict,
)

# --- shared fixtures/helpers ---


def _profile() -> Profile:
    return Profile(
        backend="langchain",
        auth=ApiKeyAuth(api_key=cast(Any, "sk-test")),
        models={"standard": ModelSpec(model="claude-sonnet-4-5")},
    )


def _session_row() -> SessionRow:
    return SessionRow(
        session_id="11111111-1111-1111-1111-111111111111",
        backend="langchain",
        profile="default",
        options_snapshot={},
    )


class _NoBroker:
    """Satisfies `PermissionBroker` but must never be called."""

    async def decide(self, tool_name: str, _tool_input: dict[str, object]) -> Verdict:
        raise AssertionError(f"broker.decide should not be called (got {tool_name!r})")


def _make_ctx(
    *,
    prompt: str = "hello",
    system_prompt: str | None = None,
    tools: ToolHost | None = None,
    broker: object = _NoBroker(),
    history: list[StoredMessage] | None = None,
) -> TurnContext:
    return TurnContext(
        session=_session_row(),
        turn_id="turn-1",
        prompt=prompt,
        model_spec=ModelSpec(model="claude-sonnet-4-5"),
        system_prompt=system_prompt,
        output_schema=None,
        tools=tools if tools is not None else ToolHost([], [], lambda ref: ref),
        broker=cast(Any, broker),
        load_history=(lambda: history if history is not None else []),
    )


class _ScriptedChatModel(FakeMessagesListChatModel):
    """`FakeMessagesListChatModel` plus a no-op `bind_tools` (the base
    class's default raises `NotImplementedError`) and a recording of every
    `messages` list it was invoked with, for asserting on rebuild output."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    calls: list[list[BaseMessage]] = Field(default_factory=list)

    def bind_tools(
        self, _tools: Sequence[object], *, _tool_choice: object = None, **_kwargs: object
    ) -> Runnable[Any, AIMessage]:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: object,
    ) -> ChatResult:
        self.calls.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _backend(model: BaseChatModel) -> LangchainBackend:
    return LangchainBackend(_profile(), NativeStoreConfig(), chat_model_factory=lambda _spec: model)


# --- (a) two-tool turn: allow one, deny one (Step 1a) ---


async def test_tool_loop_allows_one_tool_and_denies_another() -> None:
    async def allowed_handler(**kwargs: object) -> str:
        return f"ok:{kwargs}"

    tool_host = ToolHost(
        [
            Tool(
                name="allowed_tool",
                description="d",
                input_schema={"type": "object"},
                handler=allowed_handler,
            )
        ],
        [],
        lambda ref: ref,
    )

    first_response = AIMessage(
        content="",
        tool_calls=[
            {"name": "allowed_tool", "args": {"x": 1}, "id": "call_a", "type": "tool_call"},
            {"name": "denied_tool", "args": {"y": 2}, "id": "call_b", "type": "tool_call"},
        ],
    )
    final_response = AIMessage(content="Done!")
    model = _ScriptedChatModel(responses=[first_response, final_response])

    class _Broker:
        async def decide(self, tool_name: str, _tool_input: dict[str, object]) -> Verdict:
            return "deny" if tool_name == "denied_tool" else "allow"

    ctx = _make_ctx(tools=tool_host, broker=_Broker())
    events = [event async for event in _backend(model).run(ctx)]

    assert events[0] == TurnStarted(turn_id="turn-1")
    assert isinstance(events[-1], TurnCompleted)
    result = cast(TurnCompleted, events[-1]).result
    assert result.status == "completed"
    assert result.final_text == "Done!"

    permission_events = [e for e in events if isinstance(e, PermissionRequested)]
    assert permission_events == [
        PermissionRequested(tool_name="denied_tool", tool_input={"y": 2}, verdict="deny")
    ]

    tool_results = {
        cast(str, e.message.content["tool_use_id"]): e.message.content
        for e in events
        if isinstance(e, ItemCompleted) and e.message.kind == "tool_result"
    }
    assert tool_results["call_a"]["is_error"] is False
    assert "ok:{'x': 1}" in tool_results["call_a"]["content"]
    assert tool_results["call_b"] == {
        "tool_use_id": "call_b",
        "content": "permission denied",
        "is_error": True,
    }

    tool_use_items = [
        e.message for e in events if isinstance(e, ItemCompleted) and e.message.kind == "tool_use"
    ]
    assert {item.content["name"] for item in tool_use_items} == {"allowed_tool", "denied_tool"}

    # Full relative order (fix round 1, minor 2): both tool_use items land
    # before any tool_result/PermissionRequested for either call, and
    # denied_tool's PermissionRequested precedes its own synthesized
    # tool_result -- not just "these events all occurred somewhere".
    def _kind(event: object) -> str:
        if isinstance(event, ItemCompleted):
            return f"item:{event.message.kind}:{event.message.content.get('name') or event.message.content.get('tool_use_id')}"
        return type(event).__name__

    assert [_kind(e) for e in events] == [
        "TurnStarted",
        "item:tool_use:allowed_tool",
        "item:tool_use:denied_tool",
        "item:tool_result:call_a",
        "PermissionRequested",
        "item:tool_result:call_b",
        "TextDelta",
        "item:text:None",
        "TurnCompleted",
    ]


# --- (b) rebuild: thinking omitted, tool_use/tool_result reconstructed (Step 1b) ---


async def test_rebuild_excludes_thinking_and_reconstructs_tool_blocks() -> None:
    history = [
        StoredMessage(role="user", kind="text", content={"text": "what's the weather?"}),
        StoredMessage(role="assistant", kind="thinking", content={"text": "let me check"}),
        StoredMessage(
            role="assistant",
            kind="tool_use",
            content={"id": "call_x", "name": "get_weather", "input": {"city": "sf"}},
        ),
        StoredMessage(
            role="tool",
            kind="tool_result",
            content={"tool_use_id": "call_x", "content": "sunny", "is_error": False},
        ),
        StoredMessage(role="assistant", kind="text", content={"text": "It's sunny."}),
    ]
    model = _ScriptedChatModel(responses=[AIMessage(content="anything else?")])
    ctx = _make_ctx(prompt="thanks", system_prompt="be terse", history=history)

    events = [event async for event in _backend(model).run(ctx)]
    assert isinstance(events[-1], TurnCompleted)

    sent = model.calls[0]
    assert sent[0] == SystemMessage(content="be terse")
    assert sent[1] == HumanMessage(content="what's the weather?")

    # thinking is dropped entirely -- no ThinkingMessage-shaped entry anywhere.
    assert not any(isinstance(m, AIMessage) and "let me check" in str(m.content) for m in sent)

    tool_use_msg = sent[2]
    assert isinstance(tool_use_msg, AIMessage)
    assert tool_use_msg.tool_calls == [
        {"name": "get_weather", "args": {"city": "sf"}, "id": "call_x", "type": "tool_call"}
    ]

    tool_result_msg = sent[3]
    assert isinstance(tool_result_msg, ToolMessage)
    assert tool_result_msg.content == "sunny"
    assert tool_result_msg.tool_call_id == "call_x"
    assert tool_result_msg.status == "success"

    assert sent[4] == AIMessage(content="It's sunny.")
    assert sent[5] == HumanMessage(content="thanks")


# --- (c) interrupt mid-loop ends the iterator cleanly, no TurnFailed (Step 1c) ---


class _BlockingChatModel(BaseChatModel):
    """Blocks forever on the first request so a concurrent `interrupt()`
    can be delivered deterministically (no sleep-based synchronization,
    GUIDELINES §10): `started` is set right before the block, so the test
    awaits it before cancelling."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    started: anyio.Event

    def bind_tools(
        self, _tools: Sequence[object], *, _tool_choice: object = None, **_kwargs: object
    ) -> Runnable[Any, AIMessage]:
        return self

    def _generate(self, messages: list[BaseMessage], **kwargs: object) -> ChatResult:
        raise NotImplementedError("only the async path is exercised in this test")

    async def _agenerate(self, _messages: list[BaseMessage], **_kwargs: object) -> ChatResult:
        self.started.set()
        await anyio.sleep_forever()
        raise AssertionError("unreachable: cancellation should unwind before this returns")

    @property
    def _llm_type(self) -> str:
        return "blocking-fake"


async def test_interrupt_mid_loop_ends_iterator_cleanly_without_turnfailed() -> None:
    started = anyio.Event()
    model = _BlockingChatModel(started=started)
    backend = _backend(model)
    ctx = _make_ctx()

    events: list[object] = []

    async def consume() -> None:
        async for event in backend.run(ctx):
            events.append(event)

    async with anyio.create_task_group() as tg:
        tg.start_soon(consume)
        await started.wait()
        await backend.interrupt(ctx.session.session_id)

    assert events == [TurnStarted(turn_id="turn-1")]
    assert not any(isinstance(e, TurnFailed) for e in events)
    assert not any(isinstance(e, TurnCompleted) for e in events)


async def test_interrupt_of_unknown_session_is_a_no_op() -> None:
    await _backend(_ScriptedChatModel(responses=[AIMessage(content="x")])).interrupt(
        "no-such-session"
    )


# --- capabilities / probe_native / read_native_transcript (spec table) ---


def test_capabilities_match_spec_table() -> None:
    caps = _backend(_ScriptedChatModel(responses=[AIMessage(content="x")])).capabilities()
    assert caps.supports_system_prompt is True
    # Structured output is deferred (fix round 1, Important): tool-choice
    # forcing is not implemented, so the flag must say so honestly rather
    # than advertise unimplemented behavior.
    assert caps.supports_structured_output is False
    assert caps.supports_interactive_permissions is True
    assert caps.supports_in_process_tools is True
    assert caps.supports_native_resume is False
    assert caps.supports_fork is False
    assert caps.supports_transcript_read is False


async def test_probe_native_always_false() -> None:
    backend = _backend(_ScriptedChatModel(responses=[AIMessage(content="x")]))
    assert await backend.probe_native(_session_row()) is False


async def test_read_native_transcript_raises_unsupported() -> None:
    backend = _backend(_ScriptedChatModel(responses=[AIMessage(content="x")]))
    with pytest.raises(Unsupported):
        await backend.read_native_transcript(_session_row(), None)


# --- output_schema is rejected up front, matching supports_structured_output=False ---


async def test_run_with_output_schema_raises_unsupported_before_turn_started() -> None:
    model = _ScriptedChatModel(responses=[AIMessage(content="x")])
    ctx = _make_ctx()
    ctx.output_schema = {"type": "object", "properties": {}}

    events: list[object] = []
    with pytest.raises(Unsupported):
        async for event in _backend(model).run(ctx):
            events.append(event)

    # No TurnStarted (or anything else) was yielded before the raise.
    assert events == []


# --- max-iteration guard -> TurnFailed, not TurnCompleted ---


async def test_max_iterations_exceeded_yields_turnfailed_not_turncompleted() -> None:
    looping_response = AIMessage(
        content="",
        tool_calls=[{"name": "loop_tool", "args": {}, "id": "call_loop", "type": "tool_call"}],
    )

    async def loop_handler(**_: object) -> str:
        return "again"

    tool_host = ToolHost(
        [
            Tool(
                name="loop_tool",
                description="d",
                input_schema={"type": "object"},
                handler=loop_handler,
            )
        ],
        [],
        lambda ref: ref,
    )
    model = _ScriptedChatModel(responses=[looping_response])  # cycles forever
    ctx = _make_ctx(tools=tool_host, broker=_AllowAllBroker())

    events = [event async for event in _backend(model).run(ctx)]

    assert not any(isinstance(e, TurnCompleted) for e in events)
    failed = [e for e in events if isinstance(e, TurnFailed)]
    assert len(failed) == 1
    assert "max iterations" in failed[0].error


class _AllowAllBroker:
    async def decide(self, _tool_name: str, _tool_input: dict[str, object]) -> Verdict:
        return "allow"


# --- end_reason (FR-6.5): status alone must not conflate a clean finish,
# provider truncation, and the caller's tool-round cap ---


async def test_clean_finish_completes_with_end_reason_end_turn() -> None:
    model = _ScriptedChatModel(responses=[AIMessage(content="Done!")])

    events = [event async for event in _backend(model).run(_make_ctx())]

    result = cast(TurnCompleted, events[-1]).result
    assert result.status == "completed"
    assert result.end_reason == "end_turn"


async def test_max_tokens_stop_reason_completes_as_max_tokens_and_executes_no_tools() -> None:
    """A response the provider truncated at max_tokens must not be reported
    as a clean `end_turn` (the lie FR-6.5 exists to prevent), and any tool
    calls on it are potentially truncated themselves -- none may execute."""

    async def must_not_run(**_: object) -> str:
        raise AssertionError("a truncated response's tool call was executed")

    tool_host = ToolHost(
        [Tool(name="t", description="d", input_schema={"type": "object"}, handler=must_not_run)],
        [],
        lambda ref: ref,
    )
    truncated = AIMessage(
        content="partial tex",
        tool_calls=[{"name": "t", "args": {}, "id": "call_1", "type": "tool_call"}],
        response_metadata={"stop_reason": "max_tokens"},
    )
    ctx = _make_ctx(tools=tool_host, broker=_AllowAllBroker())

    events = [event async for event in _backend(_ScriptedChatModel(responses=[truncated])).run(ctx)]

    result = cast(TurnCompleted, events[-1]).result
    assert result.status == "completed"
    assert result.end_reason == "max_tokens"
    assert result.final_text == "partial tex"


async def test_max_tool_rounds_cap_completes_honestly_after_the_capped_rounds() -> None:
    """`max_tool_rounds=1` on a model that would loop forever: exactly one
    round executes, then the turn completes as an honest partial
    (`end_reason="max_tool_rounds"`) -- never `TurnFailed`, never a fake
    clean finish."""
    calls: list[object] = []

    async def counting_handler(**kwargs: object) -> str:
        calls.append(kwargs)
        return "again"

    tool_host = ToolHost(
        [
            Tool(
                name="t", description="d", input_schema={"type": "object"}, handler=counting_handler
            )
        ],
        [],
        lambda ref: ref,
    )
    looping = AIMessage(
        content="",
        tool_calls=[{"name": "t", "args": {}, "id": "call_loop", "type": "tool_call"}],
    )
    ctx = _make_ctx(tools=tool_host, broker=_AllowAllBroker())
    ctx.max_tool_rounds = 1

    events = [event async for event in _backend(_ScriptedChatModel(responses=[looping])).run(ctx)]

    assert not any(isinstance(e, TurnFailed) for e in events)
    result = cast(TurnCompleted, events[-1]).result
    assert result.status == "completed"
    assert result.end_reason == "max_tool_rounds"
    assert len(calls) == 1


async def test_max_tool_rounds_zero_permits_one_model_response_and_no_tool_execution() -> None:
    async def must_not_run(**_: object) -> str:
        raise AssertionError("max_tool_rounds=0 must not execute any tool")

    tool_host = ToolHost(
        [Tool(name="t", description="d", input_schema={"type": "object"}, handler=must_not_run)],
        [],
        lambda ref: ref,
    )
    wants_tools = AIMessage(
        content="",
        tool_calls=[{"name": "t", "args": {}, "id": "call_1", "type": "tool_call"}],
    )
    ctx = _make_ctx(tools=tool_host, broker=_AllowAllBroker())
    ctx.max_tool_rounds = 0

    events = [
        event async for event in _backend(_ScriptedChatModel(responses=[wants_tools])).run(ctx)
    ]

    result = cast(TurnCompleted, events[-1]).result
    assert result.end_reason == "max_tool_rounds"


async def test_uncapped_turn_still_hits_the_max_iterations_backstop() -> None:
    """No `max_tool_rounds` keeps today's behavior byte-for-byte: the
    `_MAX_ITERATIONS` runaway guard fails the turn rather than quietly
    completing it -- an uncapped caller never asked for a partial."""
    looping = AIMessage(
        content="",
        tool_calls=[{"name": "t", "args": {}, "id": "call_loop", "type": "tool_call"}],
    )

    async def loop_handler(**_: object) -> str:
        return "again"

    tool_host = ToolHost(
        [Tool(name="t", description="d", input_schema={"type": "object"}, handler=loop_handler)],
        [],
        lambda ref: ref,
    )
    ctx = _make_ctx(tools=tool_host, broker=_AllowAllBroker())

    events = [event async for event in _backend(_ScriptedChatModel(responses=[looping])).run(ctx)]

    assert not any(isinstance(e, TurnCompleted) for e in events)
    assert any(isinstance(e, TurnFailed) for e in events)


def test_capabilities_declare_tool_round_cap_support() -> None:
    caps = _backend(_ScriptedChatModel(responses=[AIMessage(content="x")])).capabilities()
    assert caps.supports_tool_round_cap is True


# --- cancel-scope safety across tasks (sextant integration bug,
# 2026-09-02): `run()` must never hold a CancelScope open across a
# `yield`. When a consumer abandons the stream, asyncio's async-generator
# finalizer delivers GeneratorExit FROM ITS OWN TASK -- the old shape then
# died with "Attempted to exit cancel scope in a different task than it
# was entered in" ---


async def test_run_survives_being_closed_from_a_different_task() -> None:
    import asyncio

    model = _ScriptedChatModel(responses=[AIMessage(content="never finished")])
    generator = _backend(model).run(_make_ctx())

    first = await generator.__anext__()
    assert first == TurnStarted(turn_id="turn-1")

    # Close the generator from a DIFFERENT task -- exactly what asyncio's
    # async-generator shutdown finalizer does to an abandoned stream.
    await asyncio.get_running_loop().create_task(generator.aclose())
    # Old code: RuntimeError from anyio's cross-task CancelScope check.
    # Reaching here at all is the regression assertion.


async def test_abandoning_the_stream_mid_turn_closes_cleanly() -> None:
    # The consumer-side shape that triggered the bug end to end:
    # `Session.run` raising on a `TurnFailed` abandons the stream between
    # events; the backend generator must finalize without error and
    # without leaking its pump task.
    model = _ScriptedChatModel(responses=[AIMessage(content="hi")])
    generator = _backend(model).run(_make_ctx())

    assert await generator.__anext__() == TurnStarted(turn_id="turn-1")
    await generator.aclose()

    remaining = [event async for event in generator]
    assert remaining == []


async def test_bind_tools_not_implemented_fails_loudly_with_a_named_message() -> None:
    """`BaseChatModel.bind_tools` raises a bare NotImplementedError whose
    str() is EMPTY -- uncaught it produced `TurnFailed("")`. Tools were
    registered and the model cannot take them: the failure must name the
    operation (FR-1.2, GUIDELINES §9), never proceed unbound."""

    async def handler(**_: object) -> str:
        return "unreachable"

    tool_host = ToolHost(
        [Tool(name="t", description="d", input_schema={"type": "object"}, handler=handler)],
        [],
        lambda ref: ref,
    )
    # Plain FakeMessagesListChatModel: no bind_tools override, so the base
    # class raises -- the shape sextant hit with GenericFakeChatModel.
    model = FakeMessagesListChatModel(responses=[AIMessage(content="x")])
    ctx = _make_ctx(tools=tool_host, broker=_AllowAllBroker())

    events = [
        event
        async for event in LangchainBackend(
            _profile(), NativeStoreConfig(), chat_model_factory=lambda _spec: model
        ).run(ctx)
    ]

    failed = [e for e in events if isinstance(e, TurnFailed)]
    assert len(failed) == 1
    assert "bind_tools" in failed[0].error
    assert "FakeMessagesListChatModel" in failed[0].error


async def test_bind_tools_not_implemented_is_fine_when_no_tools_registered() -> None:
    # No tools registered -> nothing to bind -> a bind_tools-less model
    # works untouched.
    model = FakeMessagesListChatModel(responses=[AIMessage(content="plain")])
    ctx = _make_ctx()

    events = [
        event
        async for event in LangchainBackend(
            _profile(), NativeStoreConfig(), chat_model_factory=lambda _spec: model
        ).run(ctx)
    ]

    result = cast(TurnCompleted, events[-1]).result
    assert result.final_text == "plain"
