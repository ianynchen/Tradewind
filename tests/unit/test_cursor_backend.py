"""Tests for `CursorBackend` instance behaviour that isn't a pure mapping
function: `capabilities()`, `probe_native()`, `take_native_session_id()`'s
per-session pop, `interrupt()`'s registry/no-op/swallow behaviour, `run()`'s
`output_schema` capability guard, and the `ToolHost` -> `CustomTool` bridge
(`_build_custom_tools`) actually dispatching a call end to end with no
network access. Pure event mapping lives in `tests/unit/test_cursor_mapping.py`;
a real live session is `tests/conformance/test_cursor.py`, permanently
skipped until P-5 is resolved (see that module's own docstring) -- there is
no injectable fake "bridge subprocess" seam here the way `LangchainBackend`
has a fake chat model, so `_run_turn` (the actual bridge-driving body) is
never exercised in this file, mirroring exactly what
`test_claude_backend.py`/`test_codex_backend.py` do for their own
un-fakeable SDK client construction. `run()`'s own capability guard (below)
needs none of that -- it raises before `_run_turn` is ever reached.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from cursor_sdk import UnsupportedRunOperationError

from tradewind.adapters.cursor_backend import CursorBackend, _build_custom_tools
from tradewind.application.config import NativeStoreConfig
from tradewind.application.ports import TurnContext
from tradewind.application.tool_host import ToolHost
from tradewind.domain.errors import Unsupported
from tradewind.domain.models import (
    ModelSpec,
    Profile,
    SessionRow,
    StoredMessage,
    SubscriptionAuth,
    Tool,
    Verdict,
)


def _profile() -> Profile:
    return Profile(
        backend="cursor",
        auth=SubscriptionAuth(),
        models={"standard": ModelSpec(model="composer-2")},
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


def _make_ctx(*, output_schema: dict[str, object] | None) -> TurnContext:
    return TurnContext(
        session=SessionRow(
            session_id="s1", backend="cursor", profile="default", options_snapshot={}
        ),
        turn_id="turn-1",
        prompt="hello",
        model_spec=ModelSpec(model="composer-2"),
        system_prompt=None,
        output_schema=output_schema,
        tools=ToolHost([], [], lambda ref: ref),
        broker=cast(Any, _NoBroker()),
        load_history=_history_loader([]),
    )


# --- capabilities ------------------------------------------------------


def test_capabilities_match_the_brief() -> None:
    caps = CursorBackend(_profile(), NativeStoreConfig()).capabilities()

    assert caps.supports_system_prompt is False
    assert caps.supports_structured_output is False
    assert caps.supports_interactive_permissions is False
    assert caps.supports_in_process_tools is True
    assert caps.supports_native_resume is True
    assert caps.supports_fork is False
    assert caps.supports_transcript_read is False
    # The agentic loop runs inside Cursor's own engine, which exposes no
    # round/turn cap (`AgentOptions` has none) -- the flag must say so
    # honestly rather than advertise a cap this adapter could only fake.
    assert caps.supports_tool_round_cap is False


# --- run(): output_schema capability guard (mirrors supports_structured_
# output=False; matches ClaudeBackend's/LangchainBackend's identical guard,
# same test shape as test_langchain_adapter.py's own) --------------------


async def test_run_with_output_schema_raises_unsupported_before_turn_started() -> None:
    backend = CursorBackend(_profile(), NativeStoreConfig())
    ctx = _make_ctx(output_schema={"type": "object", "properties": {}})

    events: list[object] = []
    with pytest.raises(Unsupported):
        async for event in backend.run(ctx):
            events.append(event)

    # No TurnStarted (or anything else, and in particular no bridge
    # subprocess spawn attempt) happened before the raise.
    assert events == []


# --- run(): max_tool_rounds capability guard (mirrors
# supports_tool_round_cap=False; same shape as the output_schema guard) ---


async def test_run_with_max_tool_rounds_raises_unsupported_before_turn_started() -> None:
    backend = CursorBackend(_profile(), NativeStoreConfig())
    ctx = _make_ctx(output_schema=None)
    ctx.max_tool_rounds = 1

    events: list[object] = []
    with pytest.raises(Unsupported):
        async for event in backend.run(ctx):
            events.append(event)

    assert events == []


# --- probe_native: cheap id-truthiness check ---------------------------


async def test_probe_native_is_true_iff_native_session_id_is_set() -> None:
    backend = CursorBackend(_profile(), NativeStoreConfig())
    with_id = SessionRow(
        session_id="s1",
        backend="cursor",
        profile="default",
        options_snapshot={},
        native_session_id="agent-abc",
    )
    without_id = SessionRow(
        session_id="s2", backend="cursor", profile="default", options_snapshot={}
    )

    assert await backend.probe_native(with_id) is True
    assert await backend.probe_native(without_id) is False


# --- read_native_transcript: capability-gated Unsupported ----------------


async def test_read_native_transcript_raises_unsupported() -> None:
    backend = CursorBackend(_profile(), NativeStoreConfig())
    session = SessionRow(session_id="s1", backend="cursor", profile="default", options_snapshot={})

    with pytest.raises(Unsupported):
        await backend.read_native_transcript(session, None)


# --- take_native_session_id: per-session pop (mirrors ClaudeBackend) ----


def test_take_native_session_id_pops_and_second_call_returns_none() -> None:
    backend = CursorBackend(_profile(), NativeStoreConfig())
    backend._native_ids["sess-1"] = "agent-abc"

    first = backend.take_native_session_id("sess-1")
    second = backend.take_native_session_id("sess-1")

    assert first == "agent-abc"
    assert second is None


def test_take_native_session_id_is_scoped_per_session() -> None:
    backend = CursorBackend(_profile(), NativeStoreConfig())
    backend._native_ids["sess-a"] = "agent-a"

    assert backend.take_native_session_id("sess-b") is None
    assert backend.take_native_session_id("sess-a") == "agent-a"


# --- interrupt: no-op when nothing registered, delegates, swallows -------


async def test_interrupt_is_a_noop_when_no_run_is_registered() -> None:
    backend = CursorBackend(_profile(), NativeStoreConfig())

    await backend.interrupt("no-such-session")  # must not raise


async def test_interrupt_calls_the_registered_runs_cancel() -> None:
    backend = CursorBackend(_profile(), NativeStoreConfig())
    calls: list[bool] = []

    class _FakeRun:
        async def cancel(self) -> None:
            calls.append(True)

    backend._runs["sess-1"] = _FakeRun()  # type: ignore[assignment]

    await backend.interrupt("sess-1")

    assert calls == [True]


async def test_interrupt_swallows_unsupported_run_operation_error() -> None:
    # `AsyncRun.cancel()` raises this when the run already reached a
    # terminal status (installed SDK's own `_async_run.py`) -- the same
    # "nothing to interrupt anymore" race `Backend.interrupt`'s contract
    # requires be a no-op.
    backend = CursorBackend(_profile(), NativeStoreConfig())

    class _TerminalRun:
        async def cancel(self) -> None:
            raise UnsupportedRunOperationError("cancel", "already terminal")

    backend._runs["sess-1"] = _TerminalRun()  # type: ignore[assignment]

    await backend.interrupt("sess-1")  # must not raise


# --- _build_custom_tools: ToolHost bridge, dispatched end to end --------


async def test_build_custom_tools_dispatches_into_tool_host_call() -> None:
    calls: list[dict[str, object]] = []

    async def handler(**kwargs: object) -> str:
        calls.append(kwargs)
        return "ok"

    tool = Tool(
        name="my_tool",
        description="does a thing",
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        handler=handler,
    )
    host = ToolHost([tool], [], resolve_ref=lambda ref: ref)

    custom_tools = _build_custom_tools(host)

    assert set(custom_tools) == {"my_tool"}
    assert custom_tools["my_tool"].description == "does a thing"
    result = await custom_tools["my_tool"].execute({"x": "a"}, None)

    assert calls == [{"x": "a"}]
    assert result == {"content": [{"type": "text", "text": "ok"}], "isError": False}


async def test_build_custom_tools_reports_errors_via_is_error() -> None:
    async def handler(**_kwargs: object) -> str:
        raise ValueError("boom")

    tool = Tool(name="bad_tool", description="d", input_schema={}, handler=handler)
    host = ToolHost([tool], [], resolve_ref=lambda ref: ref)

    custom_tools = _build_custom_tools(host)
    result = await custom_tools["bad_tool"].execute({}, None)

    assert result["isError"] is True
    assert "boom" in result["content"][0]["text"]
