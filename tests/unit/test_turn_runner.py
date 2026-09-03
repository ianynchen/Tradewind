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
5. R-1 system-prompt emulation (task-15 brief): a backend flagging
   `supports_system_prompt=False` gets the rules-file/prompt-folding
   emulation `_emulate_system_prompt` implements, driven purely by the
   capability flag (Cursor is the first backend this applies to, but the
   emulation itself is backend-agnostic -- see that function's own
   docstring).
"""

from __future__ import annotations

import warnings
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from pathlib import Path
from typing import Any, ClassVar, cast

import anyio
import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult
from langchain_core.runnables import Runnable
from pydantic import ConfigDict, Field, SecretStr

from tradewind.adapters.langchain_backend import LangchainBackend
from tradewind.application import client as _client
from tradewind.application.client import Tradewind
from tradewind.application.config import (
    CompactionSettings,
    NativeStoreConfig,
    TradewindConfig,
    TurnDefaults,
)
from tradewind.application.ports import Backend, TurnContext
from tradewind.application.turn_runner import (
    _emulate_system_prompt,
    _fold_system_prompt,
    _write_rules_file,
)
from tradewind.domain.errors import (
    CompactionFailed,
    ConfigError,
    SessionNotFound,
    TurnInProgress,
    Unsupported,
)
from tradewind.domain.events import (
    Event,
    ItemCompleted,
    TurnCompleted,
    TurnFailed,
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
    StoredMessage,
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
        store=tmp_path / "sessions.db",
        **kwargs,  # type: ignore[arg-type]
    )


# --- (1a) unregistered backend -> ConfigError via the REAL registry path ---


async def test_unregistered_backend_raises_config_error_via_real_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # "cursor" used to be a valid-but-unregistered `BackendName` stand-in for
    # this test; task-15 registered it too (alongside claude/codex/langchain),
    # so every `BackendName` now has a real factory in the module-level
    # registry and there is no naturally-unregistered name left to reuse.
    # Rather than reach for another fictional name that a future task might
    # also register out from under this test, delete just the "cursor" key
    # from the REAL registry `tradewind/__init__.py` populates -- this still
    # exercises the real `Tradewind._resolve_backend` -> `_backend_factories.
    # get()` -> ConfigError path (no seeded `_backends`, no fake factory
    # dict standing in for the whole registry), only synthetically missing
    # the one entry this test needs missing.
    monkeypatch.setattr(
        _client,
        "_backend_factories",
        {name: factory for name, factory in _client._backend_factories.items() if name != "cursor"},
    )
    profile = _profile(backend="cursor")
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

    def fake_langchain_factory(p: Profile, native_config: NativeStoreConfig) -> Backend:
        return LangchainBackend(p, native_config, chat_model_factory=lambda _spec: model)

    # Patches the module-level registry `tradewind/__init__.py` populates at
    # import time -- exercises `Tradewind._resolve_backend`'s real lookup
    # and factory call, not `Tradewind._backends` seeded directly.
    monkeypatch.setattr(_client, "_backend_factories", {"langchain": fake_langchain_factory})

    tw = Tradewind(_config(tmp_path, profile))
    session = await tw.create(_VALID_ID, SessionOptions())

    result = await session.run("ping")

    assert result.status == "completed"
    assert result.final_text == "pong"


# --- (1c) TradewindConfig.native_stores actually reaches the backend
# factory (final review wave, item 1 -- previously every factory built its
# backend with a fresh, empty NativeStoreConfig() regardless of what the
# caller configured) ---


async def test_run_passes_the_configured_native_store_config_to_the_backend_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received_native_configs: list[NativeStoreConfig] = []

    def fake_langchain_factory(p: Profile, native_config: NativeStoreConfig) -> Backend:
        received_native_configs.append(native_config)
        return LangchainBackend(
            p,
            native_config,
            chat_model_factory=lambda _spec: _ScriptedChatModel(
                responses=[AIMessage(content="pong")]
            ),
        )

    monkeypatch.setattr(_client, "_backend_factories", {"langchain": fake_langchain_factory})

    native_stores = NativeStoreConfig(isolation_mode=True, codex_home=tmp_path / "codex-home")
    profile = _profile()
    tw = Tradewind(_config(tmp_path, profile, native_stores=native_stores))
    session = await tw.create(_VALID_ID, SessionOptions())

    result = await session.run("ping")

    assert result.status == "completed"
    # Not just equal in value -- the exact object `TradewindConfig.native_stores`
    # holds, proving `_resolve_backend` reads `self._config.native_stores`
    # rather than constructing its own.
    assert received_native_configs == [native_stores]
    assert received_native_configs[0] is native_stores


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
            supports_tool_round_cap=False,
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
            turn_id="fake-turn",
            status="completed",
            end_reason="end_turn",
            final_text=text,
            usage={},
            cost_usd=None,
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
            supports_tool_round_cap=False,
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
                end_reason="end_turn",
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
            supports_tool_round_cap=False,
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


async def test_stream_abandoned_after_stop_finalizes_as_interrupted_not_failed(
    tmp_path: Path,
) -> None:
    # Minor #5 fix regression: `execute()`'s own `except BaseException`
    # clause used to classify EVERY abandonment (including this one, where
    # `stop()` already marked the interrupt as pending) as `failed` with a
    # junk empty-`GeneratorExit` message, because it set `terminal_status`
    # before the `finally` block's own `_interrupt_requested` check ever
    # ran. `stop()` here can't make `_HangingBackend` end its `run()` on its
    # own (its `interrupt()` is a no-op, same as the test above) -- the
    # stream still has to be abandoned to end the turn -- so this
    # reproduces "pending stop + abandoned stream" specifically, not a
    # clean interrupt.
    profile = _profile()
    tw = Tradewind(_config(tmp_path, profile))
    fake = _HangingBackend(profile, NativeStoreConfig())
    tw._backends["default"] = fake
    session = await tw.create(_VALID_ID, SessionOptions())

    stream = session.stream("hi")
    async with aclosing(stream):
        async for event in stream:
            assert isinstance(event, TurnStarted)
            break
        # `_interrupt_requested` must already carry this session_id BEFORE
        # `stream.aclose()` runs below (leaving the `async with aclosing`
        # block) -- that's what the `finally` block's classification reads.
        await session.stop()

    assert fake.cleaned_up is True
    # Classified `interrupted`, not `failed` -- the bug this test guards
    # against would have produced `["failed"]` here.
    assert await anyio.to_thread.run_sync(_turn_statuses, tw._store, _VALID_ID) == ["interrupted"]


# --- (5) reconcile: TurnRunner backfills the mirror before running a
# native-path turn (task-11 brief) ---


class _ReconcilingBackend(Backend):
    """Reports `supports_transcript_read=True` and always answers
    `read_native_transcript` with a fixed tail -- enough to prove
    `TurnRunner.execute` actually calls `ResumePlanner.reconcile` (and thus
    `store.import_native_items`) before running the turn, not that the
    reconcile logic itself is correct (that's tests/unit/test_reconcile.py's
    job)."""

    name: ClassVar[BackendName] = "langchain"

    def __init__(
        self, profile: Profile, native_config: NativeStoreConfig, items: list[NormalizedMessage]
    ) -> None:
        super().__init__(profile, native_config)
        self._items = items

    def capabilities(self) -> Capabilities:
        return Capabilities(
            supports_system_prompt=True,
            supports_structured_output=False,
            supports_interactive_permissions=True,
            supports_in_process_tools=True,
            supports_native_resume=True,
            supports_fork=False,
            supports_transcript_read=True,
            supports_tool_round_cap=False,
        )

    async def run(self, ctx: TurnContext) -> AsyncIterator[Event]:
        yield TurnStarted(turn_id=ctx.turn_id)
        yield _completed("done")

    async def probe_native(self, session: SessionRow) -> bool:
        return session.native_session_id is not None

    async def read_native_transcript(
        self,
        session: SessionRow,  # noqa: ARG002
        after_native_id: str | None,  # noqa: ARG002
    ) -> list[NormalizedMessage]:
        return self._items

    async def interrupt(self, session_id: str) -> None:
        pass


async def test_execute_reconciles_native_transcript_before_running_the_turn(
    tmp_path: Path,
) -> None:
    profile = _profile()
    tw = Tradewind(_config(tmp_path, profile))
    backfilled = NormalizedMessage(
        role="assistant", kind="text", content={"text": "backfilled"}, native_id="native-9"
    )
    tw._backends["default"] = _ReconcilingBackend(profile, NativeStoreConfig(), [backfilled])
    session = await tw.create(_VALID_ID, SessionOptions())
    # A session only reconciles once it already has a native_session_id
    # (execute()'s own guard) -- simulate a prior native turn having set one.
    await anyio.to_thread.run_sync(
        tw._store.rehome_native, _VALID_ID, "langchain", "native-session-x"
    )

    result = await session.run("hi")

    assert result.status == "completed"
    history = await tw.history(_VALID_ID)
    native_items = [
        (m.native_id, m.content.get("text")) for m in history if m.native_id is not None
    ]
    assert native_items == [("native-9", "backfilled")]
    # The backfilled item landed before this turn's own prompt (reconcile
    # runs before the prompt is appended -- execute()'s own ordering).
    # `_completed("done")` only sets `TurnResult.final_text`, no
    # `ItemCompleted` -- nothing else mirrors an assistant reply here.
    assert [m.content.get("text") for m in history] == ["backfilled", "hi"]


async def test_execute_skips_reconcile_when_session_has_no_native_session_id(
    tmp_path: Path,
) -> None:
    # No prior native turn -> no native_session_id on the row yet -> the
    # `session_row.native_session_id is not None` guard must skip reconcile
    # entirely (nothing to reconcile against, and calling read_native_
    # transcript(after=None) here would silently replay the WHOLE fake
    # transcript into the mirror on turn 1, which is not what this test's
    # backend is standing in for).
    profile = _profile()
    tw = Tradewind(_config(tmp_path, profile))
    would_backfill = NormalizedMessage(
        role="assistant", kind="text", content={"text": "should not appear"}, native_id="native-1"
    )
    tw._backends["default"] = _ReconcilingBackend(profile, NativeStoreConfig(), [would_backfill])
    session = await tw.create(_VALID_ID, SessionOptions())

    await session.run("hi")

    history = await tw.history(_VALID_ID)
    assert all(m.native_id is None for m in history)


# --- (6) native-id rehome: TurnRunner re-homes the session row when a
# backend's `take_native_session_id(session_id)` differs from what's stored
# (task-11 brief; scoped per-session, task-11 review fix round 1) ---


class _RehomingBackend(Backend):
    """Mirrors `ClaudeBackend`'s own fix (task-11 review, fix round 1): a
    per-session_id `_native_ids` dict plus a popping
    `take_native_session_id`, NOT a single shared attribute -- so a test can
    drive multiple tradewind sessions through ONE backend instance (exactly
    how `Tradewind._resolve_backend` caches a backend per *profile*, not per
    session) and prove they don't cross-contaminate each other's rehome.

    `script_result(session_id, native_session_id)` arranges for that
    session's next `run()` to record a native id, mirroring
    `ClaudeBackend._drive_client`'s `ResultMessage` handling.
    `script_failure(session_id)` arranges for that session's next `run()` to
    fail *before* ever recording one -- mirroring a turn that errors before
    its `ResultMessage` arrives (the reviewer's own failure scenario).
    """

    name: ClassVar[BackendName] = "claude"

    def __init__(self, profile: Profile, native_config: NativeStoreConfig) -> None:
        super().__init__(profile, native_config)
        self._native_ids: dict[str, str] = {}
        self._scripted_results: dict[str, str] = {}
        self._scripted_failures: set[str] = set()

    def script_result(self, session_id: str, native_session_id: str) -> None:
        self._scripted_results[session_id] = native_session_id

    def script_failure(self, session_id: str) -> None:
        self._scripted_failures.add(session_id)

    def take_native_session_id(self, session_id: str) -> str | None:
        return self._native_ids.pop(session_id, None)

    def capabilities(self) -> Capabilities:
        return Capabilities(
            supports_system_prompt=True,
            supports_structured_output=False,
            supports_interactive_permissions=True,
            supports_in_process_tools=True,
            supports_native_resume=True,
            supports_fork=True,
            supports_transcript_read=False,
            supports_tool_round_cap=False,
        )

    async def run(self, ctx: TurnContext) -> AsyncIterator[Event]:
        yield TurnStarted(turn_id=ctx.turn_id)
        session_id = ctx.session.session_id
        if session_id in self._scripted_failures:
            yield TurnFailed(turn_id=ctx.turn_id, error="boom before any ResultMessage")
            return
        native_session_id = self._scripted_results.get(session_id)
        if native_session_id is not None:
            self._native_ids[session_id] = native_session_id
        yield _completed("done")

    async def probe_native(self, session: SessionRow) -> bool:
        return session.native_session_id is not None

    async def read_native_transcript(
        self,
        session: SessionRow,  # noqa: ARG002
        after_native_id: str | None,  # noqa: ARG002
    ) -> list[NormalizedMessage]:
        raise Unsupported("fake backend has no native transcript")

    async def interrupt(self, session_id: str) -> None:
        pass


async def test_execute_rehomes_native_session_id_when_backend_reports_a_new_one(
    tmp_path: Path,
) -> None:
    profile = _profile(backend="claude")
    tw = Tradewind(_config(tmp_path, profile))
    fake = _RehomingBackend(profile, NativeStoreConfig())
    fake.script_result(_VALID_ID, "native-new")
    tw._backends["default"] = fake
    session = await tw.create(_VALID_ID, SessionOptions())

    result = await session.run("hi")

    assert result.status == "completed"
    row = await anyio.to_thread.run_sync(tw._store.get_session, _VALID_ID)
    assert row is not None
    assert row.native_session_id == "native-new"
    # The pre-turn (backend, native_session_id) pair -- here ("claude",
    # None), since this session never had a native id before -- is appended
    # to native_history, not discarded (SessionStorePort.rehome_native's
    # own append-only contract).
    assert row.native_history == [{"backend": "claude", "native_session_id": None}]


async def test_execute_does_not_rehome_when_backend_reports_the_same_native_session_id(
    tmp_path: Path,
) -> None:
    profile = _profile(backend="claude")
    tw = Tradewind(_config(tmp_path, profile))
    fake = _RehomingBackend(profile, NativeStoreConfig())
    fake.script_result(_VALID_ID, "native-same")
    tw._backends["default"] = fake
    session = await tw.create(_VALID_ID, SessionOptions())
    await anyio.to_thread.run_sync(tw._store.rehome_native, _VALID_ID, "claude", "native-same")

    await session.run("hi")

    row = await anyio.to_thread.run_sync(tw._store.get_session, _VALID_ID)
    assert row is not None
    # Still just the one rehome from setup above -- execute() must not have
    # appended a second, no-op entry when the backend's id already matched.
    assert row.native_history == [{"backend": "claude", "native_session_id": None}]


async def test_execute_scopes_rehome_per_session_on_one_shared_backend_instance(
    tmp_path: Path,
) -> None:
    # The reviewer's own failure scenario (task-11 review, fix round 1):
    # session A completes and records a native id; session B is fresh, on
    # the SAME cached backend instance (`Tradewind._resolve_backend` caches
    # one backend per *profile*, shared across every session on it), and
    # B's turn fails before it ever gets its own `ResultMessage`. B must NOT
    # be rehomed onto A's native id -- confirming the earlier bug (a single
    # shared `last_native_session_id` attribute) is gone.
    #
    # B is driven via `session.stream()` fully drained, not `session.run()`:
    # `Session.run()` raises `TurnExecutionFailed` the instant it sees the
    # `TurnFailed` event (client.py) and never resumes `execute()`'s
    # generator again, so the code after the event loop -- including the
    # rehome check this test exists to exercise -- would only run later, on
    # whatever schedule the abandoned async generator happens to get
    # garbage-collected and `aclose()`d on. Draining `stream()` directly
    # lets `execute()` run to its own natural end deterministically, so this
    # test actually exercises the rehome code path for B's failure, not just
    # its (trivially true either way) end state.
    profile = _profile(backend="claude")
    tw = Tradewind(_config(tmp_path, profile))
    fake = _RehomingBackend(profile, NativeStoreConfig())
    tw._backends["default"] = fake

    session_a_id = "44444444-4444-4444-4444-444444444444"
    session_b_id = "55555555-5555-5555-5555-555555555555"
    session_a = await tw.create(session_a_id, SessionOptions())
    session_b = await tw.create(session_b_id, SessionOptions())

    fake.script_result(session_a_id, "native-a")
    fake.script_failure(session_b_id)

    result_a = await session_a.run("hi")
    events_b = [event async for event in session_b.stream("hi")]

    assert any(isinstance(e, TurnFailed) for e in events_b)
    row_a = await anyio.to_thread.run_sync(tw._store.get_session, session_a_id)
    row_b = await anyio.to_thread.run_sync(tw._store.get_session, session_b_id)
    assert row_a is not None
    assert row_b is not None
    assert result_a.status == "completed"
    assert row_a.native_session_id == "native-a"
    # The bug this test guards against: B's row must stay unrehomed --
    # NOT "native-a" -- even though it shares a backend instance with A.
    assert row_b.native_session_id is None
    assert row_b.native_history == []


def _turn_statuses(store: object, session_id: str) -> list[str]:
    """Reads the `turns` table's `status` column directly for `session_id`
    -- `SessionStorePort` has no read verb for a single turn's terminal
    status, only `sweep_stale_turns` (a write) and `finalize_turn`'s own
    caller-supplied value, neither of which independently proves what
    actually landed in the store."""
    conn = cast(Any, store)._conn  # SqliteSessionStore's one sqlite3.Connection
    cur = conn.execute("SELECT status FROM turns WHERE session_id = ? ORDER BY seq", (session_id,))
    return [cast(str, row[0]) for row in cur.fetchall()]


# --- (5) R-1 system-prompt emulation (task-15 brief) ---------------------


class _NoSystemPromptBackend(Backend):
    """Reports `supports_system_prompt=False` -- the trigger `_emulate_
    system_prompt` gates on -- and records the exact `ctx.prompt` string it
    was handed each turn, so the end-to-end test below can prove
    `TurnRunner.execute` actually rewrites it before the backend sees it."""

    name: ClassVar[BackendName] = "langchain"

    def __init__(self, profile: Profile, native_config: NativeStoreConfig) -> None:
        super().__init__(profile, native_config)
        self.received_prompts: list[str] = []

    def capabilities(self) -> Capabilities:
        return Capabilities(
            supports_system_prompt=False,
            supports_structured_output=False,
            supports_interactive_permissions=True,
            supports_in_process_tools=True,
            supports_native_resume=False,
            supports_fork=False,
            supports_transcript_read=False,
            supports_tool_round_cap=False,
        )

    async def run(self, ctx: TurnContext) -> AsyncIterator[Event]:
        self.received_prompts.append(ctx.prompt)
        yield TurnStarted(turn_id=ctx.turn_id)
        yield _completed("ok")

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


# --- pure-function tests: _emulate_system_prompt / _write_rules_file ----


def test_emulate_system_prompt_is_a_noop_when_the_backend_supports_it(tmp_path: Path) -> None:
    class _SupportsIt:
        def capabilities(self) -> Capabilities:
            return Capabilities(
                supports_system_prompt=True,
                supports_structured_output=False,
                supports_interactive_permissions=True,
                supports_in_process_tools=True,
                supports_native_resume=False,
                supports_fork=False,
                supports_transcript_read=False,
                supports_tool_round_cap=False,
            )

    row = SessionRow(
        session_id="s1", backend="claude", profile="default", options_snapshot={}, cwd=str(tmp_path)
    )

    prompt = _emulate_system_prompt(
        cast(Backend, _SupportsIt()), row, "be nice", "hi", is_first_turn=True
    )

    assert prompt == "hi"
    assert not (tmp_path / ".cursor").exists()


def test_emulate_system_prompt_is_a_noop_when_there_is_no_system_prompt(tmp_path: Path) -> None:
    backend = _NoSystemPromptBackend(_profile(), NativeStoreConfig())
    row = SessionRow(
        session_id="s1",
        backend="langchain",
        profile="default",
        options_snapshot={},
        cwd=str(tmp_path),
    )

    prompt = _emulate_system_prompt(backend, row, None, "hi", is_first_turn=True)

    assert prompt == "hi"
    assert not (tmp_path / ".cursor").exists()


def test_emulate_system_prompt_writes_the_rules_file_when_cwd_is_writable(tmp_path: Path) -> None:
    backend = _NoSystemPromptBackend(_profile(), NativeStoreConfig())
    row = SessionRow(
        session_id="s1",
        backend="langchain",
        profile="default",
        options_snapshot={},
        cwd=str(tmp_path),
    )

    prompt = _emulate_system_prompt(backend, row, "be nice", "hi", is_first_turn=True)

    # The rules file, never AGENTS.md or any other existing file (R-1).
    assert prompt == "hi"
    rules_path = tmp_path / ".cursor" / "rules" / "tradewind-session.mdc"
    assert rules_path.is_file()
    assert "be nice" in rules_path.read_text()
    assert not (tmp_path / "AGENTS.md").exists()


def test_emulate_system_prompt_rewrites_the_rules_file_on_a_later_turn_too(
    tmp_path: Path,
) -> None:
    # Unlike the fold fallback (below), the rules-file path is NOT gated on
    # `is_first_turn` -- rewriting the live workspace file every turn is
    # idempotent and never duplicates anything (module docstring).
    backend = _NoSystemPromptBackend(_profile(), NativeStoreConfig())
    row = SessionRow(
        session_id="s1",
        backend="langchain",
        profile="default",
        options_snapshot={},
        cwd=str(tmp_path),
    )

    prompt = _emulate_system_prompt(backend, row, "be nice", "hi again", is_first_turn=False)

    assert prompt == "hi again"
    rules_path = tmp_path / ".cursor" / "rules" / "tradewind-session.mdc"
    assert "be nice" in rules_path.read_text()


def test_emulate_system_prompt_is_idempotent_across_turns(tmp_path: Path) -> None:
    backend = _NoSystemPromptBackend(_profile(), NativeStoreConfig())
    row = SessionRow(
        session_id="s1",
        backend="langchain",
        profile="default",
        options_snapshot={},
        cwd=str(tmp_path),
    )
    rules_path = tmp_path / ".cursor" / "rules" / "tradewind-session.mdc"

    _emulate_system_prompt(backend, row, "first prompt", "hi", is_first_turn=True)
    first_content = rules_path.read_text()
    _emulate_system_prompt(backend, row, "second prompt", "hi again", is_first_turn=False)
    second_content = rules_path.read_text()

    assert "first prompt" in first_content
    assert "second prompt" in second_content
    assert "first prompt" not in second_content  # overwritten, not appended


def test_emulate_system_prompt_folds_into_prompt_on_the_first_turn_when_cwd_is_unwritable(
    tmp_path: Path,
) -> None:
    # A file sitting exactly where the rules file's own directory would
    # need to be created forces `mkdir(parents=True)` to fail with a real
    # `OSError` (`NotADirectoryError`) -- no permission-bit trickery needed,
    # portable across platforms/CI users.
    blocked_cwd = tmp_path / "blocked"
    blocked_cwd.write_text("not a directory")
    backend = _NoSystemPromptBackend(_profile(), NativeStoreConfig())
    row = SessionRow(
        session_id="s1",
        backend="langchain",
        profile="default",
        options_snapshot={},
        cwd=str(blocked_cwd),
    )

    prompt = _emulate_system_prompt(backend, row, "be nice", "hi", is_first_turn=True)

    assert prompt == "[Instructions]\nbe nice\n[Task]\nhi"


def test_emulate_system_prompt_does_not_fold_on_a_later_turn_even_when_cwd_is_unwritable(
    tmp_path: Path,
) -> None:
    # Controller ruling (fix round 1): the fold plants the instructions in
    # the backend's own NATIVE conversation history on turn one; a backend
    # without a native system prompt can still carry that native history
    # forward on its own (e.g. Cursor's `Agent.resume()`), so re-folding on
    # every later turn would re-inject and duplicate the instructions in
    # that native history turn after turn -- gated on `is_first_turn`
    # regardless of whether the rules-file write keeps failing.
    blocked_cwd = tmp_path / "blocked"
    blocked_cwd.write_text("not a directory")
    backend = _NoSystemPromptBackend(_profile(), NativeStoreConfig())
    row = SessionRow(
        session_id="s1",
        backend="langchain",
        profile="default",
        options_snapshot={},
        cwd=str(blocked_cwd),
    )

    prompt = _emulate_system_prompt(backend, row, "be nice", "hi again", is_first_turn=False)

    assert prompt == "hi again"


def test_emulate_system_prompt_folds_when_session_has_no_cwd_at_all() -> None:
    backend = _NoSystemPromptBackend(_profile(), NativeStoreConfig())
    row = SessionRow(session_id="s1", backend="langchain", profile="default", options_snapshot={})

    prompt = _emulate_system_prompt(backend, row, "be nice", "hi", is_first_turn=True)

    assert prompt == "[Instructions]\nbe nice\n[Task]\nhi"


def test_emulate_system_prompt_does_not_fold_when_no_cwd_on_a_later_turn() -> None:
    backend = _NoSystemPromptBackend(_profile(), NativeStoreConfig())
    row = SessionRow(session_id="s1", backend="langchain", profile="default", options_snapshot={})

    prompt = _emulate_system_prompt(backend, row, "be nice", "hi again", is_first_turn=False)

    assert prompt == "hi again"


def test_write_rules_file_returns_false_on_oserror(tmp_path: Path) -> None:
    blocked_cwd = tmp_path / "blocked"
    blocked_cwd.write_text("not a directory")

    assert _write_rules_file(str(blocked_cwd), "be nice") is False


def test_fold_system_prompt_shape() -> None:
    assert _fold_system_prompt("be nice", "hi") == "[Instructions]\nbe nice\n[Task]\nhi"


# --- end-to-end through TurnRunner.execute: the backend actually sees it,
# and the mirror only ever stores the caller's ORIGINAL prompt (fix round 1:
# the earlier shape persisted the fold itself into store.history(), and
# re-applied it on every turn -- both fixed together, both asserted here
# across two turns) --------------------------------------------------------


def _prompt_texts(messages: list[StoredMessage]) -> list[str]:
    return [
        cast(str, m.content.get("text", ""))
        for m in messages
        if m.role == "user" and m.kind == "text"
    ]


async def test_execute_folds_system_prompt_only_on_the_first_turn_and_never_persists_the_fold(
    tmp_path: Path,
) -> None:
    profile = _profile()
    tw = Tradewind(_config(tmp_path, profile))
    fake = _NoSystemPromptBackend(profile, NativeStoreConfig())
    tw._backends["default"] = fake
    # No `cwd` on the session -> the fallback (fold) path is the only one
    # reachable on every turn where it applies at all.
    session = await tw.create(_VALID_ID, SessionOptions(system_prompt="be nice"))

    first = await session.run("hi one")
    second = await session.run("hi two")

    assert first.status == "completed"
    assert second.status == "completed"
    # The backend sees the fold ONLY on turn one; turn two is unfolded --
    # the instructions are assumed already part of the backend's own turn-
    # one exchange (module docstring's "native history" reasoning).
    assert fake.received_prompts == [
        "[Instructions]\nbe nice\n[Task]\nhi one",
        "hi two",
    ]
    # The mirror stores the CALLER's original text, never the fold, on
    # EITHER turn -- this is the bug fix round 1 exists for.
    history = await tw.history(_VALID_ID)
    assert _prompt_texts(history) == ["hi one", "hi two"]
    assert all("[Instructions]" not in text for text in _prompt_texts(history))
    assert all("[Task]" not in text for text in _prompt_texts(history))


async def test_execute_writes_rules_file_every_turn_and_always_persists_originals(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = _profile()
    tw = Tradewind(_config(tmp_path, profile))
    fake = _NoSystemPromptBackend(profile, NativeStoreConfig())
    tw._backends["default"] = fake
    session = await tw.create(_VALID_ID, SessionOptions(system_prompt="be nice", cwd=workspace))

    first = await session.run("hi one")
    second = await session.run("hi two")

    assert first.status == "completed"
    assert second.status == "completed"
    # The rules-file path never folds, on either turn.
    assert fake.received_prompts == ["hi one", "hi two"]
    rules_path = workspace / ".cursor" / "rules" / "tradewind-session.mdc"
    assert rules_path.is_file()
    assert "be nice" in rules_path.read_text()
    # And the mirror always stores exactly what the caller sent, on this
    # path too (regression coverage the reviewer specifically asked for --
    # this path was never buggy, but was previously unasserted here).
    history = await tw.history(_VALID_ID)
    assert _prompt_texts(history) == ["hi one", "hi two"]


# --- max_tool_rounds override (FR-6.5): validated per-call, threaded to
# the backend's TurnContext, and honest end_reason on a stop-interrupted
# turn whose backend ends with no terminal event ---


class _CtxRecordingBackend(_ScriptedBackend):
    """`_ScriptedBackend` that also records every `TurnContext` handed to
    `run()`, so a test can assert what the runner actually threads
    through the port."""

    def __init__(
        self, profile: Profile, native_config: NativeStoreConfig, events: list[Event]
    ) -> None:
        super().__init__(profile, native_config, events)
        self.contexts: list[TurnContext] = []

    async def run(self, ctx: TurnContext) -> AsyncIterator[Event]:
        self.contexts.append(ctx)
        for event in self._events:
            yield event


async def test_max_tool_rounds_override_reaches_the_backend_context(tmp_path: Path) -> None:
    profile = _profile()
    tw = Tradewind(_config(tmp_path, profile))
    fake = _CtxRecordingBackend(
        profile, NativeStoreConfig(), [TurnStarted(turn_id="t"), _completed()]
    )
    tw._backends["default"] = fake
    session = await tw.create(_VALID_ID, SessionOptions())

    await session.run("hi", max_tool_rounds=2)
    await session.run("hi again")

    # Per-call means per-call: the second, uncapped run must not inherit
    # the first call's cap.
    assert [ctx.max_tool_rounds for ctx in fake.contexts] == [2, None]


@pytest.mark.parametrize("bad_value", [-1, "2", 1.5, True])
async def test_invalid_max_tool_rounds_raises_config_error_without_opening_a_turn(
    tmp_path: Path, bad_value: object
) -> None:
    # `True` is the classic footgun: `bool` is an `int` subclass, but a
    # caller passing `max_tool_rounds=True` did not mean "cap at 1".
    profile = _profile()
    tw = Tradewind(_config(tmp_path, profile))
    fake = _CtxRecordingBackend(
        profile, NativeStoreConfig(), [TurnStarted(turn_id="t"), _completed()]
    )
    tw._backends["default"] = fake
    session = await tw.create(_VALID_ID, SessionOptions())

    with pytest.raises(ConfigError, match="max_tool_rounds"):
        await session.run("hi", max_tool_rounds=bad_value)

    # Validation happened before `begin_turn`: no turn row exists at all,
    # in_progress or otherwise, and the backend was never invoked.
    assert await anyio.to_thread.run_sync(_turn_statuses, tw._store, _VALID_ID) == []
    assert fake.contexts == []


class _InterruptibleBackend(_ScriptedBackend):
    """Yields `TurnStarted`, then waits until `interrupt()` fires and ends
    its stream with NO terminal event -- exactly `Backend.run`'s documented
    mid-turn-interrupt shape, forcing the runner's synthesized terminal
    event path."""

    def __init__(self, profile: Profile, native_config: NativeStoreConfig) -> None:
        super().__init__(profile, native_config, [])
        self._interrupted = anyio.Event()

    async def run(self, ctx: TurnContext) -> AsyncIterator[Event]:
        yield TurnStarted(turn_id=ctx.turn_id)
        await self._interrupted.wait()

    async def interrupt(self, session_id: str) -> None:  # noqa: ARG002 -- Backend interface
        self._interrupted.set()


async def test_synthesized_interrupt_result_carries_end_reason_interrupted(
    tmp_path: Path,
) -> None:
    # Sextant-facing honesty (FR-6.5): a consumer switching on
    # `TurnResult.end_reason` alone -- without also consulting `status` --
    # must still see the interrupt for what it is.
    profile = _profile()
    tw = Tradewind(_config(tmp_path, profile))
    tw._backends["default"] = _InterruptibleBackend(profile, NativeStoreConfig())
    session = await tw.create(_VALID_ID, SessionOptions())

    events: list[Event] = []
    async for event in session.stream("hi"):
        events.append(event)
        if isinstance(event, TurnStarted):
            await session.stop()

    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert len(completed) == 1
    assert completed[0].result.status == "interrupted"
    assert completed[0].result.end_reason == "interrupted"


# --- FR-5.7: no store configured -> ephemeral in-memory mirror; a
# langchain session still gets its conversation context across turns
# within the process (the whole point of the mirror), with nothing
# touching disk ---


class _RecordingChatModel(_ScriptedChatModel):
    """`_ScriptedChatModel` plus a record of every messages list it was
    invoked with, for asserting what context the rebuild handed it."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    calls: list[list[BaseMessage]] = Field(default_factory=list)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: object,
    ) -> ChatResult:
        self.calls.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


async def test_no_store_langchain_session_keeps_context_across_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _RecordingChatModel(
        responses=[AIMessage(content="pong one"), AIMessage(content="pong two")]
    )

    def fake_langchain_factory(p: Profile, native_config: NativeStoreConfig) -> Backend:
        return LangchainBackend(p, native_config, chat_model_factory=lambda _spec: model)

    monkeypatch.setattr(_client, "_backend_factories", {"langchain": fake_langchain_factory})

    # No `store=` at all (FR-5.7): the ephemeral in-memory mirror.
    tw = Tradewind(TradewindConfig(profiles={"default": _profile()}, default_profile="default"))
    session = await tw.create(_VALID_ID, SessionOptions())

    first = await session.run("ping one")
    second = await session.run("ping two")

    assert (first.final_text, second.final_text) == ("pong one", "pong two")
    # The second turn's request contained the first turn's full exchange --
    # the in-memory mirror fed the rebuild exactly like a sqlite one would.
    second_request_texts = [str(message.content) for message in model.calls[1]]
    assert "ping one" in second_request_texts
    assert "pong one" in second_request_texts
    assert second_request_texts[-1] == "ping two"


# --- backend_factories injection seam (ADR-0002): the public front door
# for an embedder's no-network tests, replacing monkeypatching of the
# module-private `client._backend_factories` map ---


async def test_backend_factories_kwarg_overrides_the_registry_per_instance(
    tmp_path: Path,
) -> None:
    model = _ScriptedChatModel(responses=[AIMessage(content="scripted pong")])
    built: list[Profile] = []

    def scripted_factory(p: Profile, native_config: NativeStoreConfig) -> Backend:
        built.append(p)
        return LangchainBackend(p, native_config, chat_model_factory=lambda _spec: model)

    tw = Tradewind(
        _config(tmp_path, _profile()),
        backend_factories={"langchain": scripted_factory},
    )
    session = await tw.create(_VALID_ID, SessionOptions())

    result = await session.run("ping")

    assert result.final_text == "scripted pong"
    assert len(built) == 1
    # Per-instance only: the module registry still holds the real factory,
    # so a second, un-overridden Tradewind is untouched by this instance's
    # injection.
    assert _client._backend_factories["langchain"] is not scripted_factory


async def test_backend_factories_leaves_unnamed_backends_on_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Overriding one backend name must not hide the registry's factories
    # for every other name: a "claude" override plus a langchain profile
    # still resolves langchain through the (here: patched-registry) path.
    model = _ScriptedChatModel(responses=[AIMessage(content="registry pong")])
    registry_used: list[str] = []

    def registry_langchain(p: Profile, native_config: NativeStoreConfig) -> Backend:
        registry_used.append(p.backend)
        return LangchainBackend(p, native_config, chat_model_factory=lambda _spec: model)

    monkeypatch.setattr(_client, "_backend_factories", {"langchain": registry_langchain})

    def must_not_run(p: Profile, native_config: NativeStoreConfig) -> Backend:  # noqa: ARG001
        raise AssertionError("override for 'claude' must not be used for a langchain profile")

    tw = Tradewind(
        _config(tmp_path, _profile()),
        backend_factories={"claude": must_not_run},
    )
    session = await tw.create(_VALID_ID, SessionOptions())

    result = await session.run("ping")

    assert result.final_text == "registry pong"
    assert registry_used == ["langchain"]


# --- history_scope (FR-9.3): lazy loading, per-backend defaults, tree
# folding, and the loud Unsupported on impossible explicit scopes ---


class _NativeResumeBackend(_CtxRecordingBackend):
    """A ctx-recording fake that declares native resume -- the runner must
    treat it like the SDK backends: default scope "none", zero history
    reads, explicit "flat"/"tree" refused."""

    def capabilities(self) -> Capabilities:
        base = super().capabilities()
        return base.model_copy(update={"supports_native_resume": True})


async def test_native_resume_backend_turn_issues_no_history_query(tmp_path: Path) -> None:
    profile = _profile()
    tw = Tradewind(_config(tmp_path, profile))
    fake = _NativeResumeBackend(
        profile, NativeStoreConfig(), [TurnStarted(turn_id="t"), _completed()]
    )
    tw._backends["default"] = fake
    session = await tw.create(_VALID_ID, SessionOptions())
    await session.run("hi one")  # seed one turn so a second HAS history to skip

    def must_not_read(*_args: object, **_kwargs: object) -> list[object]:
        raise AssertionError("store.history() must not be called for a native-resume turn")

    tw._store.history = must_not_read  # type: ignore[method-assign]

    result = await session.run("hi two")

    assert result.status == "completed"


async def test_flat_scope_loads_lazily_and_excludes_this_turns_prompt(tmp_path: Path) -> None:
    profile = _profile()
    tw = Tradewind(_config(tmp_path, profile))

    loaded: list[list[StoredMessage]] = []

    class _AwaitingBackend(_CtxRecordingBackend):
        async def run(self, ctx: TurnContext) -> AsyncIterator[Event]:
            loaded.append(await ctx.load_history())
            for event in self._events:
                yield event

    fake = _AwaitingBackend(profile, NativeStoreConfig(), [TurnStarted(turn_id="t"), _completed()])
    tw._backends["default"] = fake
    session = await tw.create(_VALID_ID, SessionOptions())

    await session.run("first prompt")
    await session.run("second prompt")

    # Turn 1 sees nothing; turn 2 sees turn 1's exchange but NOT its own
    # prompt (the backend appends ctx.prompt itself).
    assert [m.content.get("text") for m in loaded[0]] == []
    texts = [m.content.get("text") for m in loaded[1]]
    assert "first prompt" in texts
    assert "second prompt" not in texts


async def test_tree_scope_folds_child_transcript_after_the_spawning_turn(tmp_path: Path) -> None:
    profile = _profile()
    model = _RecordingChatModel(
        responses=[
            AIMessage(content="parent turn one"),
            AIMessage(content="child answer"),
            AIMessage(content="parent turn two"),
        ]
    )

    def factory(p: Profile, native_config: NativeStoreConfig) -> Backend:
        return LangchainBackend(p, native_config, chat_model_factory=lambda _spec: model)

    tw = Tradewind(_config(tmp_path, profile), backend_factories={"langchain": factory})
    session = await tw.create(_VALID_ID, SessionOptions())
    await session.run("do step one")
    await session.spawn("investigate the detail")

    await session.run("wrap up", history_scope="tree")

    final_request = [str(message.content) for message in model.calls[2]]
    folded = [text for text in final_request if text.startswith("[subagent ")]
    assert len(folded) == 1
    # The child's own prompt and reply are inside the wrapped block --
    # verbatim, not as the parent's own conversation turns.
    assert "investigate the detail" in folded[0]
    assert "child answer" in folded[0]
    # Positioned after the spawning turn's exchange, before this prompt.
    fold_at = final_request.index(folded[0])
    assert fold_at > final_request.index("parent turn one")
    assert final_request[-1] == "wrap up"


async def test_flat_scope_does_not_include_child_transcripts(tmp_path: Path) -> None:
    profile = _profile()
    model = _RecordingChatModel(
        responses=[
            AIMessage(content="parent turn one"),
            AIMessage(content="child answer"),
            AIMessage(content="parent turn two"),
        ]
    )

    def factory(p: Profile, native_config: NativeStoreConfig) -> Backend:
        return LangchainBackend(p, native_config, chat_model_factory=lambda _spec: model)

    tw = Tradewind(_config(tmp_path, profile), backend_factories={"langchain": factory})
    session = await tw.create(_VALID_ID, SessionOptions())
    await session.run("do step one")
    await session.spawn("investigate the detail")

    await session.run("wrap up")  # default flat

    final_request = [str(message.content) for message in model.calls[2]]
    assert not any(text.startswith("[subagent ") for text in final_request)


async def test_explicit_tree_scope_on_native_resume_backend_raises_unsupported(
    tmp_path: Path,
) -> None:
    profile = _profile()
    tw = Tradewind(_config(tmp_path, profile))
    tw._backends["default"] = _NativeResumeBackend(
        profile, NativeStoreConfig(), [TurnStarted(turn_id="t"), _completed()]
    )
    session = await tw.create(_VALID_ID, SessionOptions())

    with pytest.raises(Unsupported, match="history_scope"):
        await session.run("hi", history_scope="tree")


async def test_explicit_none_scope_is_accepted_on_native_resume_backend(tmp_path: Path) -> None:
    # "none" on a native-resume backend states what already happens --
    # accepted, not refused (user decision, 2026-09-03).
    profile = _profile()
    tw = Tradewind(_config(tmp_path, profile))
    tw._backends["default"] = _NativeResumeBackend(
        profile, NativeStoreConfig(), [TurnStarted(turn_id="t"), _completed()]
    )
    session = await tw.create(_VALID_ID, SessionOptions())

    result = await session.run("hi", history_scope="none")

    assert result.status == "completed"


async def test_invalid_history_scope_raises_config_error_without_opening_a_turn(
    tmp_path: Path,
) -> None:
    profile = _profile()
    tw = Tradewind(_config(tmp_path, profile))
    fake = _CtxRecordingBackend(
        profile, NativeStoreConfig(), [TurnStarted(turn_id="t"), _completed()]
    )
    tw._backends["default"] = fake
    session = await tw.create(_VALID_ID, SessionOptions())

    with pytest.raises(ConfigError, match="history_scope"):
        await session.run("hi", history_scope="everything")

    assert await anyio.to_thread.run_sync(_turn_statuses, tw._store, _VALID_ID) == []
    assert fake.contexts == []


# --- compaction (FR-5.8): automatic trigger, manual verb, honesty gates ---


def _meta_profile(*, context_window: int = 100, with_cost: bool = False) -> Profile:
    from tradewind.domain.models import ModelCost, ModelMeta

    meta = ModelMeta(
        context_window=context_window,
        max_tokens=50,
        cost=ModelCost(input=3.0, output=15.0, cache_read=0.3, cache_write=3.75)
        if with_cost
        else None,
    )
    return Profile(
        backend="langchain",
        auth=ApiKeyAuth(api_key=SecretStr("sk-test")),
        models={"standard": ModelSpec(model="model-standard", meta=meta)},
    )


def _langchain_tw(
    tmp_path: Path, profile: Profile, model: FakeMessagesListChatModel, **config_kwargs: object
) -> Tradewind:
    def factory(p: Profile, native_config: NativeStoreConfig) -> Backend:
        return LangchainBackend(p, native_config, chat_model_factory=lambda _spec: model)

    return Tradewind(
        _config(tmp_path, profile, **config_kwargs), backend_factories={"langchain": factory}
    )


_CHECKPOINT = "## Goal\nkeep going\n## Progress\nDone: step one"


async def test_auto_compaction_fires_and_the_turn_sees_summary_plus_tail(
    tmp_path: Path,
) -> None:
    # window=100, reserve=20 -> trigger when the feed estimate exceeds 80
    # tokens; prompts/replies of ~200 chars are ~50 tokens each, so turn 3
    # trips it. keep_recent=30 keeps roughly the newest message.
    model = _RecordingChatModel(
        responses=[
            AIMessage(content="reply one " * 10),
            AIMessage(content="reply two " * 10),
            AIMessage(content=_CHECKPOINT),  # consumed by the SUMMARIZER
            AIMessage(content="reply three"),
        ]
    )
    tw = _langchain_tw(
        tmp_path,
        _meta_profile(context_window=100),
        model,
        defaults=TurnDefaults(
            compaction=CompactionSettings(auto=True, reserve_tokens=20, keep_recent_tokens=30)
        ),
    )
    session = await tw.create(_VALID_ID, SessionOptions())
    await session.run("prompt one " * 9)
    await session.run("prompt two " * 9)

    result = await session.run("prompt three")

    assert result.status == "completed"
    # The record reached the mirror as an ordinary item (Kind="compaction").
    records = [m for m in await tw.history(_VALID_ID) if m.kind == "compaction"]
    assert len(records) == 1
    assert records[0].content["summary"].startswith("## Goal")
    assert isinstance(records[0].content["first_kept_seq"], int)
    # The triggering turn itself was fed summary + tail, not full history.
    final_request = [str(message.content) for message in model.calls[-1]]
    assert any("compacted into the following summary" in text for text in final_request)
    assert not any("prompt one" in text for text in final_request)
    # The mirror keeps EVERY row -- compaction changes the feed, not storage.
    assert any("prompt one" in str(m.content.get("text", "")) for m in await tw.history(_VALID_ID))


async def test_no_metadata_means_no_auto_compaction(tmp_path: Path) -> None:
    model = _RecordingChatModel(responses=[AIMessage(content="r1 " * 50), AIMessage(content="r2")])
    tw = _langchain_tw(
        tmp_path,
        _profile(),  # no ModelMeta
        model,
        defaults=TurnDefaults(
            compaction=CompactionSettings(auto=True, reserve_tokens=20, keep_recent_tokens=30)
        ),
    )
    session = await tw.create(_VALID_ID, SessionOptions())
    await session.run("p1 " * 100)
    await session.run("p2")

    assert [m for m in await tw.history(_VALID_ID) if m.kind == "compaction"] == []


async def test_auto_false_suppresses_automatic_compaction(tmp_path: Path) -> None:
    model = _RecordingChatModel(responses=[AIMessage(content="r1 " * 50), AIMessage(content="r2")])
    tw = _langchain_tw(
        tmp_path,
        _meta_profile(context_window=50),
        model,
        defaults=TurnDefaults(
            compaction=CompactionSettings(auto=False, reserve_tokens=20, keep_recent_tokens=30)
        ),
    )
    session = await tw.create(_VALID_ID, SessionOptions())
    await session.run("p1 " * 100)
    await session.run("p2")

    assert [m for m in await tw.history(_VALID_ID) if m.kind == "compaction"] == []


async def test_manual_compact_persists_record_and_feeds_later_turns(tmp_path: Path) -> None:
    model = _RecordingChatModel(
        responses=[
            AIMessage(content="reply one " * 10),
            AIMessage(content="reply two " * 10),
            AIMessage(content=_CHECKPOINT),  # summarizer
            AIMessage(content="post-compact reply"),
        ]
    )
    # No ModelMeta and auto=False: manual must work regardless.
    tw = _langchain_tw(
        tmp_path,
        _profile(),
        model,
        defaults=TurnDefaults(
            compaction=CompactionSettings(auto=False, reserve_tokens=20, keep_recent_tokens=30)
        ),
    )
    session = await tw.create(_VALID_ID, SessionOptions())
    await session.run("prompt one " * 9)
    await session.run("prompt two " * 9)

    record = await session.compact("focus on the auth work")

    assert record.kind == "compaction"
    assert record.seq > 0  # persisted, with its assigned mirror seq
    stored = [m for m in await tw.history(_VALID_ID) if m.kind == "compaction"]
    assert len(stored) == 1
    # The caller's instructions reached the summarization prompt.
    summarizer_request = " ".join(str(m.content) for m in model.calls[2])
    assert "Additional focus: focus on the auth work" in summarizer_request

    result = await session.run("wrap up")
    assert result.final_text == "post-compact reply"
    final_request = [str(message.content) for message in model.calls[-1]]
    assert any("compacted into the following summary" in text for text in final_request)


async def test_compact_on_native_resume_backend_raises_unsupported(tmp_path: Path) -> None:
    profile = _profile()
    tw = Tradewind(_config(tmp_path, profile))
    tw._backends["default"] = _NativeResumeBackend(
        profile, NativeStoreConfig(), [TurnStarted(turn_id="t"), _completed()]
    )
    session = await tw.create(_VALID_ID, SessionOptions())

    with pytest.raises(Unsupported, match="compact"):
        await session.compact()


async def test_compact_with_nothing_to_summarize_fails_loudly(tmp_path: Path) -> None:
    model = _RecordingChatModel(responses=[AIMessage(content="r1")])
    tw = _langchain_tw(tmp_path, _profile(), model)
    session = await tw.create(_VALID_ID, SessionOptions())
    await session.run("short")

    with pytest.raises(CompactionFailed, match="nothing to compact"):
        await session.compact()


async def test_busy_session_rejects_both_run_and_compact(tmp_path: Path) -> None:
    model = _RecordingChatModel(responses=[AIMessage(content="r1")])
    tw = _langchain_tw(tmp_path, _profile(), model)
    session = await tw.create(_VALID_ID, SessionOptions())

    tw._turn_runner._busy.add(_VALID_ID)
    try:
        with pytest.raises(TurnInProgress):
            await session.run("hi")
        with pytest.raises(TurnInProgress):
            await session.compact()
    finally:
        tw._turn_runner._busy.discard(_VALID_ID)


async def test_cost_usd_computed_from_tier_cost_table(tmp_path: Path) -> None:
    model = _RecordingChatModel(responses=[AIMessage(content="pong")])
    tw = _langchain_tw(tmp_path, _meta_profile(context_window=100_000, with_cost=True), model)
    session = await tw.create(_VALID_ID, SessionOptions())

    result = await session.run("ping")

    # A cost table exists: computed cost is a number (the fake model
    # reports no usage, so it is 0.0) -- never None. Without a table
    # (every other langchain test here) cost_usd stays None.
    assert result.cost_usd == 0.0


async def test_manual_compact_splits_a_turn_with_prefix_summary(tmp_path: Path) -> None:
    # The cut lands INSIDE turn 2 (its long reply carries the whole
    # keep_recent budget), so Pi's split-turn path runs: a second
    # summarizer call covers the turn PREFIX and both merge under the
    # split-turn marker.
    model = _RecordingChatModel(
        responses=[
            AIMessage(content="reply one " * 10),
            AIMessage(content="reply two " * 30),  # long tail carrying the budget
            AIMessage(content=_CHECKPOINT),  # summarizer: main history
            AIMessage(content="## Original Request\nprefix part"),  # summarizer: turn prefix
        ]
    )
    tw = _langchain_tw(
        tmp_path,
        _profile(),
        model,
        defaults=TurnDefaults(
            compaction=CompactionSettings(auto=False, reserve_tokens=20, keep_recent_tokens=60)
        ),
    )
    session = await tw.create(_VALID_ID, SessionOptions())
    await session.run("prompt one " * 9)
    await session.run("prompt two " * 9)

    record = await session.compact()

    summary = str(record.content["summary"])
    assert "**Turn Context (split turn):**" in summary
    assert summary.index("## Goal") < summary.index("prefix part")
    # Two summarizer calls happened (main + prefix): 2 turns + 2 = 4.
    assert len(model.calls) == 4


# --- tw.usage() rollup (FR-5.9 / Phase 2a) ---


def _um(inp: int, out: int) -> dict[str, int]:
    return {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}


async def test_usage_rollup_sums_turns_and_reports_summarizer_separately(
    tmp_path: Path,
) -> None:
    """The no-double-count contract: turn costs cover turn tokens only;
    summarizer spend (tokens AND cost) lives on the compaction record and
    is rolled up separately -- the two sum to the session total exactly
    once."""
    model = _RecordingChatModel(
        responses=[
            AIMessage(content="reply one " * 10, usage_metadata=_um(10, 10)),
            AIMessage(content="reply two " * 10, usage_metadata=_um(20, 20)),
            AIMessage(content=_CHECKPOINT, usage_metadata=_um(100, 7)),  # summarizer
            AIMessage(content="reply three", usage_metadata=_um(30, 30)),
        ]
    )
    tw = _langchain_tw(
        tmp_path,
        _meta_profile(context_window=100, with_cost=True),  # input=3.0, output=15.0 $/Mtok
        model,
        defaults=TurnDefaults(
            compaction=CompactionSettings(auto=True, reserve_tokens=20, keep_recent_tokens=30)
        ),
    )
    session = await tw.create(_VALID_ID, SessionOptions())
    await session.run("prompt one " * 9)
    await session.run("prompt two " * 9)
    await session.run("prompt three")  # triggers compaction

    rollup = await tw.usage(_VALID_ID)

    assert rollup.turns == 3
    # Turn tokens only -- the summarizer's 100/7 never leaks into these.
    assert rollup.usage == {"input_tokens": 60, "output_tokens": 60, "total_tokens": 120}
    assert rollup.summarizer_usage == {"input_tokens": 100, "output_tokens": 7, "total_tokens": 107}
    assert rollup.cost_usd == pytest.approx((60 * 3.0 + 60 * 15.0) / 1e6)
    assert rollup.summarizer_cost_usd == pytest.approx((100 * 3.0 + 7 * 15.0) / 1e6)
    # And the record itself carries its own cost (computed at compact time).
    record = next(m for m in await tw.history(_VALID_ID) if m.kind == "compaction")
    assert record.content["summarizer_cost_usd"] == pytest.approx((100 * 3.0 + 7 * 15.0) / 1e6)


class _ScriptedRunsBackend(_ScriptedBackend):
    """Replays a DIFFERENT event list per run() call, so one session can
    have turns with different results."""

    def __init__(
        self, profile: Profile, native_config: NativeStoreConfig, runs: list[list[Event]]
    ) -> None:
        super().__init__(profile, native_config, [])
        self._runs_events = runs

    async def run(self, ctx: TurnContext) -> AsyncIterator[Event]:  # noqa: ARG002 -- Backend interface
        for event in self._runs_events.pop(0):
            yield event


def _completed_with(turn_usage: dict[str, int], cost_usd: float | None) -> TurnCompleted:
    return TurnCompleted(
        result=TurnResult(
            turn_id="fake-turn",
            status="completed",
            end_reason="end_turn",
            final_text="ok",
            usage=turn_usage,
            cost_usd=cost_usd,
        )
    )


async def test_usage_cost_is_none_only_when_every_turn_cost_is_none(tmp_path: Path) -> None:
    # A zero-cost session and an unknown-cost session must not look alike.
    profile = _profile()
    tw = Tradewind(_config(tmp_path, profile))
    tw._backends["default"] = _ScriptedRunsBackend(
        profile,
        NativeStoreConfig(),
        [
            [TurnStarted(turn_id="t"), _completed_with({"input_tokens": 1}, None)],
            [TurnStarted(turn_id="t"), _completed_with({"input_tokens": 2}, 0.0)],
        ],
    )
    session = await tw.create(_VALID_ID, SessionOptions())
    await session.run("one")
    await session.run("two")

    rollup = await tw.usage(_VALID_ID)

    assert rollup.usage == {"input_tokens": 3}
    assert rollup.cost_usd == 0.0  # one known zero beats "unknown"

    tw_b = Tradewind(
        TradewindConfig(
            profiles={"default": profile}, default_profile="default", store=tmp_path / "b.db"
        )
    )
    tw_b._backends["default"] = _ScriptedRunsBackend(
        profile,
        NativeStoreConfig(),
        [[TurnStarted(turn_id="t"), _completed_with({}, None)]],
    )
    unknown_cost_id = "44444444-4444-4444-4444-444444444444"
    session_b = await tw_b.create(unknown_cost_id, SessionOptions())
    await session_b.run("one")

    assert (await tw_b.usage(unknown_cost_id)).cost_usd is None


async def test_usage_unknown_session_raises(tmp_path: Path) -> None:
    tw = Tradewind(_config(tmp_path, _profile()))

    with pytest.raises(SessionNotFound):
        await tw.usage("99999999-9999-9999-9999-999999999999")


async def test_manual_compact_spend_lands_on_record_and_in_the_rollup(tmp_path: Path) -> None:
    # No cost table: tokens are recorded, cost is honest None -- not zero.
    model = _RecordingChatModel(
        responses=[
            AIMessage(content="reply one " * 10, usage_metadata=_um(5, 5)),
            AIMessage(content="reply two " * 10, usage_metadata=_um(6, 6)),
            AIMessage(content=_CHECKPOINT, usage_metadata=_um(50, 4)),  # summarizer
        ]
    )
    tw = _langchain_tw(
        tmp_path,
        _profile(),
        model,
        defaults=TurnDefaults(
            compaction=CompactionSettings(auto=False, reserve_tokens=20, keep_recent_tokens=30)
        ),
    )
    session = await tw.create(_VALID_ID, SessionOptions())
    await session.run("prompt one " * 9)
    await session.run("prompt two " * 9)
    record = await session.compact()

    rollup = await tw.usage(_VALID_ID)

    assert record.content["summarizer_cost_usd"] is None
    assert rollup.summarizer_usage == {"input_tokens": 50, "output_tokens": 4, "total_tokens": 54}
    assert rollup.summarizer_cost_usd is None
    assert rollup.cost_usd is None  # no cost table anywhere
