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


class NativeStoreConfig(BaseModel):
    """Backend-native session storage a later task wires up (codex/cursor)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    isolation_mode: bool = False
    codex_home: Path | None = None
    cursor_store: Any | None = None


class ToolHostConfig(BaseModel):
    socket_dir: Path | None = None


class CompactionSettings(BaseModel):
    """Mirror-compaction settings (FR-5.8), config-wide. `auto` governs
    AUTOMATIC triggering only — `auto=False` never disables the manual
    `Session.compact()` verb. Automatic compaction is additionally inert
    unless the turn's tier declares `ModelMeta.context_window` (honest: no
    guessed windows). Defaults ported from Pi's."""

    auto: bool = True
    reserve_tokens: int = 16384
    keep_recent_tokens: int = 20000


class RetrySettings(BaseModel):
    """Model-call retry policy (FR-6.6), config-wide. Full-strength only
    where the backend owns its turn loop (`supports_turn_retry`:
    langchain); SDK backends apply it to the pre-turn connect/spawn step
    only -- re-running a started turn could duplicate tool side effects.
    `max_attempts=0` disables retrying entirely. Backoff is Pi's schedule:
    `base_delay_s * 2**(attempt-1)`."""

    max_attempts: int = 3
    base_delay_s: float = 2.0


class TurnDefaults(BaseModel):
    tier: TierName = "standard"
    # Enforced since FR-6.6 (previously accepted-but-inert): a turn
    # exceeding this wall-clock deadline is interrupted via the backend's
    # own interrupt() and ends status="interrupted", end_reason="timeout".
    # No disable knob -- raise it instead.
    request_timeout_s: float = 600.0
    compaction: CompactionSettings = CompactionSettings()
    retry: RetrySettings = RetrySettings()


EventHook = Callable[[Event], None]


class TradewindConfig(BaseModel):
    """Everything a `Tradewind` instance needs: profiles, store, and
    defaults. Validated as a whole in `model_post_init`; raises
    `ConfigError` (never `pydantic.ValidationError`) for cross-field rules
    pydantic's own field validation can't express."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    profiles: dict[str, Profile]
    default_profile: str
    # Where session state lives — one union field so the illegal
    # two-sources state is unrepresentable (ADR-0001):
    #   Path              -> tradewind builds its default sqlite engine there
    #                        (WAL, user_version migrations)
    #   SessionStorePort  -> a caller-built store (Postgres later, P-4)
    #   None (default)    -> EPHEMERAL in-memory mirror (FR-5.7): private to
    #                        this instance, gone at process exit. Turns run
    #                        identically (history, single-flight, reconcile),
    #                        but nothing tradewind-side persists — durability
    #                        is then only the SDK backends' own native stores
    #                        (claude/codex/cursor write those regardless),
    #                        and a langchain session's conversation context
    #                        lives exactly as long as the instance.
    store: Path | SessionStorePort | None = None
    # Default broker for every session that doesn't supply its own via
    # `SessionOptions.permission_broker`. Absent here too (the common case:
    # `None`), `TurnRunner` falls back to an allow-all policy -- with no
    # broker configured anywhere, every caller-registered tool is callable
    # (task-9 fix round 1 ruling).
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
