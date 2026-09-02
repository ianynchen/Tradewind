"""Application config: the assembled `TradewindConfig` a caller builds once
and passes to `Tradewind()` (task-6 brief).

Validation happens eagerly in `TradewindConfig.model_post_init` so a
misconfigured client fails at construction, not on the first session call
(GUIDELINES §9: typed errors, not surprises at use time).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, SecretStr

from tradewind.application.ports import SessionStorePort
from tradewind.domain.errors import ConfigError
from tradewind.domain.events import Event
from tradewind.domain.models import PermissionBroker, Profile, TierName


class StoreConfig(BaseModel):
    """Where session state lives: either a sqlite file path (the store is
    built for the caller) or an already-constructed store. Exactly one of
    the two must be set (`TradewindConfig.model_post_init` enforces it)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    sqlite_path: Path | None = None
    store: SessionStorePort | None = None


class NativeStoreConfig(BaseModel):
    """Backend-native session storage a later task wires up (codex/cursor)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    isolation_mode: bool = False
    codex_home: Path | None = None
    cursor_store: Any | None = None


class ToolHostConfig(BaseModel):
    socket_dir: Path | None = None


class TurnDefaults(BaseModel):
    tier: TierName = "standard"
    request_timeout_s: float = 600.0


EventHook = Callable[[Event], None]


class TradewindConfig(BaseModel):
    """Everything a `Tradewind` instance needs: profiles, store, and
    defaults. Validated as a whole in `model_post_init`; raises
    `ConfigError` (never `pydantic.ValidationError`) for cross-field rules
    pydantic's own field validation can't express."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    profiles: dict[str, Profile]
    default_profile: str
    store: StoreConfig
    permission_broker: PermissionBroker | None = None
    native_stores: NativeStoreConfig = NativeStoreConfig()
    tool_host: ToolHostConfig = ToolHostConfig()
    defaults: TurnDefaults = TurnDefaults()
    on_event: EventHook | None = None
    secret_refs: dict[str, SecretStr] = {}

    def model_post_init(self, _context: Any, /) -> None:
        if not self.profiles:
            raise ConfigError("TradewindConfig.profiles must not be empty")
        if self.default_profile not in self.profiles:
            raise ConfigError(f"default_profile {self.default_profile!r} is not a key of profiles")

        tier_sets = {name: frozenset(profile.models) for name, profile in self.profiles.items()}
        for name, tiers in tier_sets.items():
            if not tiers:
                raise ConfigError(f"profile {name!r} has no models")
        distinct_tier_sets = set(tier_sets.values())
        if len(distinct_tier_sets) > 1:
            raise ConfigError(
                "all profiles must share one identical tier-name set, got: "
                f"{ {name: sorted(tiers) for name, tiers in tier_sets.items()} }"
            )

        has_sqlite_path = self.store.sqlite_path is not None
        has_store = self.store.store is not None
        if has_sqlite_path == has_store:
            raise ConfigError(
                "StoreConfig requires exactly one of sqlite_path/store, got "
                f"sqlite_path={self.store.sqlite_path!r} store={self.store.store!r}"
            )
