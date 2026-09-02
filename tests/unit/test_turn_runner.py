"""Tests for tradewind.application.turn_runner.TurnRunner behaviours not
already covered end to end by tests/conformance/ or the client-level
error-path tests in test_client_sessions.py (task-9 fix round 1):

1. the REAL backend-factory registry path (`_resolve_backend` ->
   `_backend_factories` -> factory call), not a pre-seeded `Tradewind.
   _backends` cache -- both the unregistered-backend error and building
   `LangchainBackend` through the actual registered factory.
2. the `on_event` tap's exceptions are swallowed without breaking the turn.
3. the default `_AllowAllBroker` (no broker anywhere) actually lets a
   registered tool execute.
4. abandoning `session.stream()` early (no `stop()`) closes the backend's
   own generator deterministically (`contextlib.aclosing`) and still
   finalizes the turn -- no dangling `in_progress` row, no warnings.
"""

from __future__ import annotations

import warnings
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from pathlib import Path
from typing import Any, ClassVar, cast

import anyio
import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from pydantic import ConfigDict, SecretStr

from tradewind.adapters.langchain_backend import LangchainBackend
from tradewind.application import client as _client
from tradewind.application.client import Tradewind
from tradewind.application.config import NativeStoreConfig, StoreConfig, TradewindConfig
from tradewind.application.ports import Backend, TurnContext
from tradewind.domain.errors import ConfigError, Unsupported
from tradewind.domain.events import (
    Event,
    ItemCompleted,
    TurnCompleted,
    TurnStarted,
)
from tradewind.domain.models import (
    ApiKeyAuth,
    BackendName,
    Capabilities,
    ModelSpec,
    NormalizedMessage,
    Profile,
    SessionOptions,
    SessionRow,
    Tool,
    TurnResult,
)

_VALID_ID = "33333333-3333-3333-3333-333333333333"


def _profile(*, backend: BackendName = "langchain") -> Profile:
    return Profile(
        backend=backend,
        auth=ApiKeyAuth(api_key=SecretStr("sk-test")),
        models={"standard": ModelSpec(model="model-standard")},
    )


def _config(tmp_path: Path, profile: Profile, **kwargs: object) -> TradewindConfig:
    return TradewindConfig(
        profiles={"default": profile},
        default_profile="default",
        store=StoreConfig(sqlite_path=tmp_path / "sessions.db"),
        **kwargs,  # type: ignore[arg-type]
    )


# --- (1a) unregistered backend -> ConfigError via the REAL registry path ---


async def test_unregistered_backend_raises_config_error_via_real_registry(tmp_path: Path) -> None:
    # "codex" is a valid BackendName but tradewind/__init__.py registers
    # only "langchain" -- no monkeypatching, no seeded `_backends`: this
    # goes through `Tradewind._resolve_backend` -> the real module-level
    # `_backend_factories` dict exactly as production code populates it.
    profile = _profile(backend="codex")
    tw = Tradewind(_config(tmp_path, profile))
    session = await tw.create(_VALID_ID, SessionOptions())

    with pytest.raises(ConfigError, match="no backend factory registered"):
        await session.run("hello")

    # The turn must not be left dangling `in_progress` (fix round 1 bug fix:
    # backend resolution moved inside try/finally) -- `sweep_stale_turns`
    # returning 0 proves nothing in_progress remains for this session.
    swept = await anyio.to_thread.run_sync(tw._store.sweep_stale_turns, _VALID_ID)
    assert swept == 0


# --- (1b) LangchainBackend built through the real registered factory ---


class _ScriptedChatModel(FakeMessagesListChatModel):
    """`FakeMessagesListChatModel` plus a no-op `bind_tools` (the base
    class's default raises `NotImplementedError`) -- same pattern as
    tests/unit/test_langchain_adapter.py and tests/conformance/test_langchain.py."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def bind_tools(
        self, _tools: Sequence[object], *, _tool_choice: object = None, **_kwargs: object
    ) -> Runnable[Any, AIMessage]:
        return self


async def test_run_builds_langchain_backend_via_the_registered_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile()
    model = _ScriptedChatModel(responses=[AIMessage(content="pong")])

    def fake_langchain_factory(p: Profile) -> Backend:
        return LangchainBackend(p, NativeStoreConfig(), chat_model_factory=lambda _spec: model)

    # Patches the module-level registry `tradewind/__init__.py` populates at
    # import time -- exercises `Tradewind._resolve_backend`'s real lookup
    # and factory call, not `Tradewind._backends` seeded directly.
    monkeypatch.setattr(_client, "_backend_factories", {"langchain": fake_langchain_factory})

    tw = Tradewind(_config(tmp_path, profile))
    session = await tw.create(_VALID_ID, SessionOptions())

    result = await session.run("ping")

    assert result.status == "completed"
    assert result.final_text == "pong"


# --- shared fake Backend for the tap/broker/cleanup tests below ---


class _ScriptedBackend(Backend):
    """Replays a fixed event list -- enough for the `on_event` tap test,
    which only cares that every event reaches the tap and the caller."""

    name: ClassVar[BackendName] = "langchain"

    def __init__(
        self, profile: Profile, native_config: NativeStoreConfig, events: list[Event]
    ) -> None:
        super().__init__(profile, native_config)
        self._events = events

    def capabilities(self) -> Capabilities:
        return Capabilities(
            supports_system_prompt=True,
            supports_structured_output=False,
            supports_interactive_permissions=True,
            supports_in_process_tools=True,
            supports_native_resume=False,
            supports_fork=False,
            supports_transcript_read=False,
        )

    async def run(self, ctx: TurnContext) -> AsyncIterator[Event]:  # noqa: ARG002 -- Backend interface
        for event in self._events:
            yield event

    async def probe_native(self, session: SessionRow) -> bool:  # noqa: ARG002
        return False

    async def read_native_transcript(
        self,
        session: SessionRow,  # noqa: ARG002
        after_native_id: str | None,  # noqa: ARG002
    ) -> list[NormalizedMessage]:
        raise Unsupported("fake backend has no native transcript")

    async def interrupt(self, session_id: str) -> None:
        pass


def _completed(text: str = "ok") -> TurnCompleted:
    return TurnCompleted(
        result=TurnResult(
            turn_id="fake-turn", status="completed", final_text=text, usage={}, cost_usd=None
        )
    )


# --- (2) on_event tap exceptions are swallowed, never break the turn ---


async def test_on_event_tap_exception_is_swallowed_and_events_still_stream(tmp_path: Path) -> None:
    tap_calls: list[Event] = []

    def raising_hook(event: Event) -> None:
        tap_calls.append(event)
        raise RuntimeError("tap boom")

    profile = _profile()
    tw = Tradewind(_config(tmp_path, profile, on_event=raising_hook))
    tw._backends["default"] = _ScriptedBackend(
        profile, NativeStoreConfig(), [TurnStarted(turn_id="t"), _completed("hi there")]
    )
    session = await tw.create(_VALID_ID, SessionOptions())

    events = [event async for event in session.stream("hello")]

    assert isinstance(events[-1], TurnCompleted)
    assert events[-1].result.status == "completed"
    # Not vacuous: the always-raising hook really ran once per event that
    # reached the caller (proves the tap fired despite raising every time).
    assert len(tap_calls) == len(events)
    assert tap_calls == events


# --- (3) default allow-all broker: a registered tool actually executes ---


class _ToolCallingBackend(Backend):
    """Simulates the broker-gate-then-execute shape every real backend's
    tool loop follows, in isolation from any provider SDK: consults
    `ctx.broker.decide()` then, on "allow", calls `ctx.tools.call()` for
    real -- enough to prove which broker `TurnRunner` actually wired in."""

    name: ClassVar[BackendName] = "langchain"

    def __init__(
        self,
        profile: Profile,
        native_config: NativeStoreConfig,
        tool_name: str,
        tool_args: dict[str, object],
    ) -> None:
        super().__init__(profile, native_config)
        self._tool_name = tool_name
        self._tool_args = tool_args

    def capabilities(self) -> Capabilities:
        return Capabilities(
            supports_system_prompt=True,
            supports_structured_output=False,
            supports_interactive_permissions=True,
            supports_in_process_tools=True,
            supports_native_resume=False,
            supports_fork=False,
            supports_transcript_read=False,
        )

    async def run(self, ctx: TurnContext) -> AsyncIterator[Event]:
        yield TurnStarted(turn_id=ctx.turn_id)
        verdict = await ctx.broker.decide(self._tool_name, self._tool_args)
        if verdict == "allow":
            outcome = await ctx.tools.call(self._tool_name, self._tool_args)
            yield ItemCompleted(
                message=NormalizedMessage(
                    role="tool",
                    kind="tool_result",
                    content={
                        "tool_use_id": "call-1",
                        "content": outcome.content,
                        "is_error": outcome.is_error,
                    },
                )
            )
            final_text = outcome.content
        else:
            final_text = "denied"
        yield TurnCompleted(
            result=TurnResult(
                turn_id=ctx.turn_id,
                status="completed",
                final_text=final_text,
                usage={},
                cost_usd=None,
            )
        )

    async def probe_native(self, session: SessionRow) -> bool:  # noqa: ARG002
        return False

    async def read_native_transcript(
        self,
        session: SessionRow,  # noqa: ARG002
        after_native_id: str | None,  # noqa: ARG002
    ) -> list[NormalizedMessage]:
        raise Unsupported("fake backend has no native transcript")

    async def interrupt(self, session_id: str) -> None:
        pass


async def test_tool_executes_when_no_broker_is_configured_anywhere(tmp_path: Path) -> None:
    handler_calls: list[dict[str, object]] = []

    async def handler(**kwargs: object) -> str:
        handler_calls.append(kwargs)
        return "handled"

    tool = Tool(name="my_tool", description="d", input_schema={"type": "object"}, handler=handler)
    profile = _profile()
    # No `permission_broker` in SessionOptions AND none in TradewindConfig.
    config = _config(tmp_path, profile)
    assert config.permission_broker is None
    tw = Tradewind(config)
    tw._backends["default"] = _ToolCallingBackend(profile, NativeStoreConfig(), "my_tool", {"x": 1})

    session = await tw.create(_VALID_ID, SessionOptions(tools=[tool]))
    result = await session.run("go")

    assert result.status == "completed"
    assert handler_calls == [{"x": 1}]
    assert result.final_text == "handled"


# --- (4) abandoning the stream without stop() closes the backend generator
# deterministically and still finalizes the turn ---


class _HangingBackend(Backend):
    """Blocks forever after its first event, recording whether it was
    ever properly `aclose()`d (its `finally` ran) so the test can prove
    `TurnRunner`'s `contextlib.aclosing` wrapper actually reaches it."""

    name: ClassVar[BackendName] = "langchain"

    def __init__(self, profile: Profile, native_config: NativeStoreConfig) -> None:
        super().__init__(profile, native_config)
        self.cleaned_up = False

    def capabilities(self) -> Capabilities:
        return Capabilities(
            supports_system_prompt=True,
            supports_structured_output=False,
            supports_interactive_permissions=True,
            supports_in_process_tools=True,
            supports_native_resume=False,
            supports_fork=False,
            supports_transcript_read=False,
        )

    async def run(self, ctx: TurnContext) -> AsyncIterator[Event]:
        try:
            yield TurnStarted(turn_id=ctx.turn_id)
            await anyio.sleep_forever()
            yield _completed()  # unreachable
        finally:
            self.cleaned_up = True

    async def probe_native(self, session: SessionRow) -> bool:  # noqa: ARG002
        return False

    async def read_native_transcript(
        self,
        session: SessionRow,  # noqa: ARG002
        after_native_id: str | None,  # noqa: ARG002
    ) -> list[NormalizedMessage]:
        raise Unsupported("fake backend has no native transcript")

    async def interrupt(self, session_id: str) -> None:
        pass


async def test_stream_abandoned_without_stop_closes_backend_and_finalizes_turn(
    tmp_path: Path,
) -> None:
    profile = _profile()
    tw = Tradewind(_config(tmp_path, profile))
    fake = _HangingBackend(profile, NativeStoreConfig())
    tw._backends["default"] = fake
    session = await tw.create(_VALID_ID, SessionOptions())

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        stream = session.stream("hi")
        async with aclosing(stream):
            async for event in stream:
                assert isinstance(event, TurnStarted)
                break

    # No "coroutine/generator was never awaited" or "async generator ignored
    # GeneratorExit"-class warnings -- clean, deterministic shutdown.
    assert caught == []
    # The backend's own generator was actually closed (its `finally` ran),
    # not just abandoned to the garbage collector.
    assert fake.cleaned_up is True
    # The turn was finalized (not left `in_progress`): sweep finds nothing.
    swept = await anyio.to_thread.run_sync(tw._store.sweep_stale_turns, _VALID_ID)
    assert swept == 0
    # And specifically finalized as `failed` (no `stop()` was ever called,
    # so this wasn't an interrupt) with the "no terminal event" reason.
    assert await anyio.to_thread.run_sync(_turn_statuses, tw._store, _VALID_ID) == ["failed"]


def _turn_statuses(store: object, session_id: str) -> list[str]:
    """Reads the `turns` table's `status` column directly for `session_id`
    -- `SessionStorePort` has no read verb for a single turn's terminal
    status, only `sweep_stale_turns` (a write) and `finalize_turn`'s own
    caller-supplied value, neither of which independently proves what
    actually landed in the store."""
    conn = cast(Any, store)._conn  # SqliteSessionStore's one sqlite3.Connection
    cur = conn.execute("SELECT status FROM turns WHERE session_id = ? ORDER BY seq", (session_id,))
    return [cast(str, row[0]) for row in cur.fetchall()]
