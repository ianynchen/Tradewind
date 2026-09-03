"""Tests for tradewind.application.client.Tradewind session lifecycle
verbs: create/resume/ensure/fork, id validation, tier/tool checks
(task-6 brief); run/stream/stop/spawn client-level wiring against a fake
`Backend` (task-9 brief -- the happy-path behaviour of these four is
covered end to end by `tests/conformance/test_langchain.py` against the
real adapter; this file stays focused on client-level error paths and
lineage bookkeeping that conformance doesn't need to re-prove).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import SecretStr

from tradewind.adapters.sqlite_store import SqliteSessionStore
from tradewind.application.client import Session, Tradewind
from tradewind.application.config import NativeStoreConfig, StoreConfig, TradewindConfig
from tradewind.application.ports import Backend, SessionStorePort
from tradewind.domain.errors import (
    ConfigError,
    SessionExists,
    SessionNotFound,
    ToolMismatch,
    TurnExecutionFailed,
    Unsupported,
)
from tradewind.domain.events import Event, TurnCompleted, TurnFailed
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

_VALID_ID_A = "11111111-1111-1111-1111-111111111111"
_VALID_ID_B = "22222222-2222-2222-2222-222222222222"


def _profile(*, tiers: tuple[str, ...] = ("standard", "fast")) -> Profile:
    return Profile(
        backend="claude",
        auth=ApiKeyAuth(api_key=SecretStr("sk-test")),
        models={tier: ModelSpec(model=f"model-{tier}") for tier in tiers},
    )


def _config(tmp_path: Path, name: str = "sessions.db") -> TradewindConfig:
    return TradewindConfig(
        profiles={"default": _profile()},
        default_profile="default",
        store=StoreConfig(sqlite_path=tmp_path / name),
    )


async def _handler(**_: object) -> dict[str, object]:
    return {"ok": True}


class _SpyStore(SessionStorePort):
    """Minimal `SessionStorePort` stub that records `history()` calls; every
    other verb is unused by the history() passthrough test and raises if
    ever called."""

    def __init__(self) -> None:
        self.history_calls: list[dict[str, object]] = []

    def migrate(self):
        pass

    def create_session(self, row):
        raise NotImplementedError

    def get_session(self, session_id):
        raise NotImplementedError

    def ensure_session(self, row):
        raise NotImplementedError

    def update_options(self, session_id, snapshot):
        raise NotImplementedError

    def rehome_native(self, session_id, backend, native_session_id):
        raise NotImplementedError

    def begin_turn(self, session_id, turn_id, native_turn_id):
        raise NotImplementedError

    def append_message(self, session_id, turn_id, msg):
        raise NotImplementedError

    def finalize_turn(self, turn_id, *, status, final_text, usage, cost_usd, error):
        raise NotImplementedError

    def sweep_stale_turns(self, session_id):
        raise NotImplementedError

    def history(
        self,
        session_id,
        *,
        include_children=False,
        include_raw=False,
        after_seq=None,  # noqa: ARG002 -- SessionStorePort.history() signature
        limit=None,  # noqa: ARG002 -- SessionStorePort.history() signature
    ):
        self.history_calls.append(
            {
                "session_id": session_id,
                "include_children": include_children,
                "include_raw": include_raw,
            }
        )
        return []

    def copy_history(self, src_session_id, dst_row, up_to_seq=None):
        raise NotImplementedError

    def import_native_items(self, session_id, turn_id, items):
        raise NotImplementedError

    def last_native_id(self, session_id):
        raise NotImplementedError


def _tool(name: str = "search") -> Tool:
    return Tool(
        name=name,
        description="search the web",
        input_schema={"type": "object"},
        handler=_handler,
    )


# --- session id must be a UUID (Step 2) ---


async def test_create_with_non_uuid_id_raises_value_error(tmp_path: Path) -> None:
    tw = Tradewind(_config(tmp_path))
    with pytest.raises(ValueError):
        await tw.create("not-a-uuid", SessionOptions())


async def test_resume_with_non_uuid_id_raises_value_error(tmp_path: Path) -> None:
    tw = Tradewind(_config(tmp_path))
    with pytest.raises(ValueError):
        await tw.resume("not-a-uuid")


# --- create: twice raises SessionExists (Step 2) ---


async def test_create_returns_session_bound_to_the_id(tmp_path: Path) -> None:
    tw = Tradewind(_config(tmp_path))
    session = await tw.create(_VALID_ID_A, SessionOptions())
    assert isinstance(session, Session)
    assert session.id == _VALID_ID_A


async def test_create_twice_raises_session_exists(tmp_path: Path) -> None:
    tw = Tradewind(_config(tmp_path))
    await tw.create(_VALID_ID_A, SessionOptions())
    with pytest.raises(SessionExists):
        await tw.create(_VALID_ID_A, SessionOptions())


# --- resume: missing id raises SessionNotFound (Step 2) ---


async def test_resume_missing_session_raises_session_not_found(tmp_path: Path) -> None:
    tw = Tradewind(_config(tmp_path))
    with pytest.raises(SessionNotFound):
        await tw.resume(_VALID_ID_A)


# --- ensure: idempotent (Step 2) ---


async def test_ensure_is_idempotent(tmp_path: Path) -> None:
    tw = Tradewind(_config(tmp_path))
    first = await tw.ensure(_VALID_ID_A, SessionOptions())
    second = await tw.ensure(_VALID_ID_A, SessionOptions())
    assert first.id == second.id == _VALID_ID_A


# --- resume with options: tool NAME mismatch raises ToolMismatch (Step 2) ---


async def test_resume_with_mismatched_tool_names_raises_tool_mismatch(tmp_path: Path) -> None:
    tw = Tradewind(_config(tmp_path))
    await tw.create(_VALID_ID_A, SessionOptions(tools=[_tool("search")]))

    with pytest.raises(ToolMismatch):
        await tw.resume(_VALID_ID_A, SessionOptions(tools=[_tool("other")]))


async def test_resume_with_matching_tool_names_does_not_raise(tmp_path: Path) -> None:
    # Only tool-name equivalence is asserted here; actually rebinding live
    # handlers onto a resumed session is Task 9's scope (turn runner).
    tw = Tradewind(_config(tmp_path))
    await tw.create(_VALID_ID_A, SessionOptions(tools=[_tool("search")]))

    resumed = await tw.resume(_VALID_ID_A, SessionOptions(tools=[_tool("search")]))
    assert resumed.id == _VALID_ID_A


async def test_resume_without_options_does_not_require_tools(tmp_path: Path) -> None:
    tw = Tradewind(_config(tmp_path))
    await tw.create(_VALID_ID_A, SessionOptions(tools=[_tool("search")]))

    resumed = await tw.resume(_VALID_ID_A)
    assert resumed.id == _VALID_ID_A


# --- unknown tier at session-acquisition time raises ConfigError (Step 2) ---


async def test_create_with_unknown_tier_raises_config_error(tmp_path: Path) -> None:
    tw = Tradewind(_config(tmp_path))
    with pytest.raises(ConfigError):
        await tw.create(_VALID_ID_A, SessionOptions(tier="does-not-exist"))


async def test_resume_with_unknown_tier_raises_config_error(tmp_path: Path) -> None:
    tw = Tradewind(_config(tmp_path))
    await tw.create(_VALID_ID_A, SessionOptions())
    with pytest.raises(ConfigError):
        await tw.resume(_VALID_ID_A, SessionOptions(tier="does-not-exist"))


# --- fork: uses copy_history, spawn_kind="fork" (Step 2) ---


async def test_fork_copies_history_and_sets_spawn_kind_fork(tmp_path: Path) -> None:
    tw = Tradewind(_config(tmp_path))
    await tw.create(_VALID_ID_A, SessionOptions())

    forked = await tw.fork(_VALID_ID_A, _VALID_ID_B)
    assert forked.id == _VALID_ID_B

    store = SqliteSessionStore(tmp_path / "sessions.db")
    dst_row = store.get_session(_VALID_ID_B)
    assert dst_row is not None
    assert dst_row.spawn_kind == "fork"
    assert dst_row.parent_session_id == _VALID_ID_A


async def test_fork_missing_source_raises_session_not_found(tmp_path: Path) -> None:
    tw = Tradewind(_config(tmp_path))
    with pytest.raises(SessionNotFound):
        await tw.fork(_VALID_ID_A, _VALID_ID_B)


# --- run/stream/stop/spawn: client-level wiring against a fake Backend
# (task-9) -- happy-path behaviour end to end is covered by
# tests/conformance/test_langchain.py against the real adapter. ---


class _FakeBackend(Backend):
    """A minimal `Backend` whose `run()` replays a fixed, caller-supplied
    event script -- enough to drive `TurnRunner`'s client-level wiring
    (mirroring, finalization, stop()) without a real adapter."""

    name: ClassVar[BackendName] = "claude"

    def __init__(
        self, profile: Profile, native_config: NativeStoreConfig, events: list[Event]
    ) -> None:
        super().__init__(profile, native_config)
        self._events = events
        self.interrupted: list[str] = []

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

    async def run(self, ctx: object) -> AsyncIterator[Event]:  # noqa: ARG002 -- Backend interface
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
        self.interrupted.append(session_id)


def _seed_backend(
    tw: Tradewind, events: list[Event], profile_name: str = "default"
) -> _FakeBackend:
    fake = _FakeBackend(_profile(), NativeStoreConfig(), events)
    tw._backends[profile_name] = fake
    return fake


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


async def test_run_returns_turn_result_from_turn_completed(tmp_path: Path) -> None:
    tw = Tradewind(_config(tmp_path))
    _seed_backend(tw, [_completed("hi there")])
    session = await tw.create(_VALID_ID_A, SessionOptions())

    result = await session.run("hello")

    assert result.status == "completed"
    assert result.final_text == "hi there"


async def test_run_raises_turn_execution_failed_on_turn_failed_event(tmp_path: Path) -> None:
    tw = Tradewind(_config(tmp_path))
    _seed_backend(tw, [TurnFailed(turn_id="fake-turn", error="boom")])
    session = await tw.create(_VALID_ID_A, SessionOptions())

    with pytest.raises(TurnExecutionFailed, match="boom"):
        await session.run("hello")


async def test_run_with_unknown_tier_override_raises_config_error(tmp_path: Path) -> None:
    tw = Tradewind(_config(tmp_path))
    _seed_backend(tw, [_completed()])
    session = await tw.create(_VALID_ID_A, SessionOptions())

    with pytest.raises(ConfigError):
        await session.run("hello", tier="does-not-exist")


async def test_stop_calls_backend_interrupt_for_this_session(tmp_path: Path) -> None:
    tw = Tradewind(_config(tmp_path))
    fake = _seed_backend(tw, [_completed()])
    session = await tw.create(_VALID_ID_A, SessionOptions())

    await session.stop()

    assert fake.interrupted == [_VALID_ID_A]


async def test_stop_of_unknown_session_raises_session_not_found(tmp_path: Path) -> None:
    tw = Tradewind(_config(tmp_path))
    session = Session(id=_VALID_ID_A, _client=tw)

    with pytest.raises(SessionNotFound):
        await session.stop()


async def test_spawn_creates_child_with_subagent_lineage_and_runs_the_prompt(
    tmp_path: Path,
) -> None:
    tw = Tradewind(_config(tmp_path))
    _seed_backend(tw, [_completed("child reply")])
    parent = await tw.create(_VALID_ID_A, SessionOptions())

    child = await parent.spawn("child prompt")

    assert child.id != parent.id
    store = SqliteSessionStore(tmp_path / "sessions.db")
    child_row = store.get_session(child.id)
    assert child_row is not None
    assert child_row.spawn_kind == "subagent"
    assert child_row.parent_session_id == _VALID_ID_A
    assert child_row.spawned_by_message_id is None


# --- history: id validation, unknown session, kwarg passthrough (fix round 1) ---


async def test_history_with_non_uuid_id_raises_value_error(tmp_path: Path) -> None:
    tw = Tradewind(_config(tmp_path))
    with pytest.raises(ValueError):
        await tw.history("not-a-uuid")


async def test_history_of_unknown_session_returns_empty_list(tmp_path: Path) -> None:
    tw = Tradewind(_config(tmp_path))
    assert await tw.history(_VALID_ID_A) == []


async def test_history_passes_include_children_and_include_raw_through_to_store() -> None:
    spy = _SpyStore()
    config = TradewindConfig(
        profiles={"default": _profile()},
        default_profile="default",
        store=StoreConfig(store=spy),
    )
    tw = Tradewind(config)

    await tw.history(_VALID_ID_A, include_children=True, include_raw=True)

    assert spy.history_calls == [
        {"session_id": _VALID_ID_A, "include_children": True, "include_raw": True}
    ]


async def test_history_defaults_include_children_and_include_raw_to_false() -> None:
    spy = _SpyStore()
    config = TradewindConfig(
        profiles={"default": _profile()},
        default_profile="default",
        store=StoreConfig(store=spy),
    )
    tw = Tradewind(config)

    await tw.history(_VALID_ID_A)

    assert spy.history_calls == [
        {"session_id": _VALID_ID_A, "include_children": False, "include_raw": False}
    ]
