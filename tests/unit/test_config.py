"""Tests for tradewind.application.config.TradewindConfig: cross-field
validation raised as ConfigError, and that two clients built from separate
sqlite files coexist (task-6 brief).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from tradewind import Tradewind
from tradewind.adapters.sqlite_store import SqliteSessionStore
from tradewind.application.config import TradewindConfig
from tradewind.domain.errors import ConfigError, SessionNotFound
from tradewind.domain.models import ApiKeyAuth, ModelSpec, Profile, SessionOptions


def _profile(*, tiers: tuple[str, ...] = ("standard",)) -> Profile:
    return Profile(
        backend="claude",
        auth=ApiKeyAuth(api_key=SecretStr("sk-test")),
        models={tier: ModelSpec(model=f"model-{tier}") for tier in tiers},
    )


def _store_config(tmp_path: Path, name: str = "sessions.db") -> Path:
    return tmp_path / name


# --- default_profile must be a key of profiles (Step 1) ---


def test_missing_default_profile_key_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        TradewindConfig(
            profiles={"default": _profile()},
            default_profile="does-not-exist",
            store=_store_config(tmp_path),
        )


# --- all profiles must share one identical tier-name set (Step 1) ---


def test_profiles_with_differing_tier_sets_raise_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        TradewindConfig(
            profiles={
                "a": _profile(tiers=("standard",)),
                "b": _profile(tiers=("standard", "fast")),
            },
            default_profile="a",
            store=_store_config(tmp_path),
        )


def test_profiles_with_matching_tier_sets_construct(tmp_path: Path) -> None:
    config = TradewindConfig(
        profiles={
            "a": _profile(tiers=("standard", "fast")),
            "b": _profile(tiers=("fast", "standard")),
        },
        default_profile="a",
        store=_store_config(tmp_path),
    )
    assert config.default_profile == "a"


# --- every profile's models must be non-empty (Step 1) ---


def test_profile_with_empty_models_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        TradewindConfig(
            profiles={"default": _profile(tiers=())},
            default_profile="default",
            store=_store_config(tmp_path),
        )


# --- store: one union field (ADR-0001) -- Path | SessionStorePort | None,
# where None (the default) is the ephemeral in-memory mirror (FR-5.7).
# The old two-field StoreConfig's "both set" state is now unrepresentable,
# so there is deliberately no both-set test to keep. ---


async def test_omitted_store_constructs_an_ephemeral_instance(tmp_path: Path) -> None:
    # FR-5.7: no store configured is legal -- the mirror is an ephemeral
    # in-memory sqlite store. `store=` may be omitted entirely; the
    # instance is fully usable (session creation works against the
    # in-memory mirror) and writes nothing to disk.
    config = TradewindConfig(profiles={"default": _profile()}, default_profile="default")

    tw = Tradewind(config)

    assert config.store is None
    # Proves the :memory: store actually migrated and accepts writes.
    await tw.create("44444444-4444-4444-4444-444444444444", SessionOptions())
    assert list(tmp_path.iterdir()) == []


async def test_two_ephemeral_instances_never_share_sessions() -> None:
    # sqlite ":memory:" is per-connection and `SqliteSessionStore` holds one
    # connection per instance -- a session created on one no-store
    # `Tradewind` must be invisible to another (no accidental process-wide
    # shared memory database).
    session_id = "55555555-5555-5555-5555-555555555555"
    tw_a = Tradewind(TradewindConfig(profiles={"default": _profile()}, default_profile="default"))
    tw_b = Tradewind(TradewindConfig(profiles={"default": _profile()}, default_profile="default"))

    await tw_a.create(session_id, SessionOptions())

    with pytest.raises(SessionNotFound):
        await tw_b.resume(session_id)


def test_caller_built_store_is_used_as_is(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    config = TradewindConfig(
        profiles={"default": _profile()},
        default_profile="default",
        store=store,
    )
    assert config.store is store


# --- two Tradewind instances on two sqlite files coexist (Step 1) ---


async def test_two_tradewind_instances_on_two_sqlite_files_coexist(tmp_path: Path) -> None:
    config_a = TradewindConfig(
        profiles={"default": _profile()},
        default_profile="default",
        store=_store_config(tmp_path, "a.db"),
    )
    config_b = TradewindConfig(
        profiles={"default": _profile()},
        default_profile="default",
        store=_store_config(tmp_path, "b.db"),
    )

    tw_a = Tradewind(config_a)
    tw_b = Tradewind(config_b)

    assert (tmp_path / "a.db").exists()
    assert (tmp_path / "b.db").exists()

    await tw_a.aclose()
    await tw_b.aclose()
