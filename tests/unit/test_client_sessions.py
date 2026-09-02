"""Tests for tradewind.application.client.Tradewind session lifecycle
verbs: create/resume/ensure/fork, id validation, tier/tool checks
(task-6 brief).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from tradewind.adapters.sqlite_store import SqliteSessionStore
from tradewind.application.client import Session, Tradewind
from tradewind.application.config import StoreConfig, TradewindConfig
from tradewind.domain.errors import ConfigError, SessionExists, SessionNotFound, ToolMismatch
from tradewind.domain.models import ApiKeyAuth, ModelSpec, Profile, SessionOptions, Tool

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


async def test_resume_with_matching_tool_names_rebinds_handlers(tmp_path: Path) -> None:
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


# --- run/stream/stop/spawn: not wired yet (Step 2, task-9 note) ---


async def test_run_raises_not_implemented(tmp_path: Path) -> None:
    tw = Tradewind(_config(tmp_path))
    session = await tw.create(_VALID_ID_A, SessionOptions())
    with pytest.raises(NotImplementedError):
        await session.run("hello")


async def test_stop_raises_not_implemented(tmp_path: Path) -> None:
    tw = Tradewind(_config(tmp_path))
    session = await tw.create(_VALID_ID_A, SessionOptions())
    with pytest.raises(NotImplementedError):
        await session.stop()


async def test_spawn_raises_not_implemented(tmp_path: Path) -> None:
    tw = Tradewind(_config(tmp_path))
    session = await tw.create(_VALID_ID_A, SessionOptions())
    with pytest.raises(NotImplementedError):
        await session.spawn("hello")


def test_stream_raises_not_implemented(tmp_path: Path) -> None:
    tw = Tradewind(_config(tmp_path))
    session = Session(id=_VALID_ID_A, _client=tw)
    with pytest.raises(NotImplementedError):
        session.stream("hello")
