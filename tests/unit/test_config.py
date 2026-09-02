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
from tradewind.application.config import StoreConfig, TradewindConfig
from tradewind.domain.errors import ConfigError
from tradewind.domain.models import ApiKeyAuth, ModelSpec, Profile


def _profile(*, tiers: tuple[str, ...] = ("standard",)) -> Profile:
    return Profile(
        backend="claude",
        auth=ApiKeyAuth(api_key=SecretStr("sk-test")),
        models={tier: ModelSpec(model=f"model-{tier}") for tier in tiers},
    )


def _store_config(tmp_path: Path, name: str = "sessions.db") -> StoreConfig:
    return StoreConfig(sqlite_path=tmp_path / name)


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


# --- StoreConfig: exactly one of sqlite_path/store (Step 1) ---


def test_store_config_with_both_sqlite_path_and_store_raises_config_error(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    with pytest.raises(ConfigError):
        TradewindConfig(
            profiles={"default": _profile()},
            default_profile="default",
            store=StoreConfig(sqlite_path=tmp_path / "sessions.db", store=store),
        )


def test_store_config_with_neither_sqlite_path_nor_store_raises_config_error() -> None:
    with pytest.raises(ConfigError):
        TradewindConfig(
            profiles={"default": _profile()},
            default_profile="default",
            store=StoreConfig(),
        )


def test_store_config_with_only_store_constructs(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    config = TradewindConfig(
        profiles={"default": _profile()},
        default_profile="default",
        store=StoreConfig(store=store),
    )
    assert config.store.store is store


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
