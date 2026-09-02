"""Application client: `Tradewind` (session lifecycle) and `Session` (a
thin per-session handle bound to it) (task-6 brief).

Layering note (controller ruling): the application layer may not import
adapters (import-linter `layers` contract; GUIDELINES §8 "dependencies
flow inward"), but `StoreConfig.sqlite_path` needs a concrete
`SessionStorePort` built from it. Rather than construct one here, this
module exposes `_set_default_store_factory` — a module-private, constant
import-time seam (GUIDELINES §8 permits mutation "as a deliberate choice
with a stated reason") assigned exactly once, at import time, by the
top-level `tradewind` package (outside the layers contract) with a
factory that builds `SqliteSessionStore`. It is not per-instance state:
every `Tradewind` built from a `sqlite_path` in a process shares the same
factory, and `Tradewind.__init__` stays a single-argument constructor
exactly as specified, with no knowledge of which adapter module backs it.

`run`/`stream`/`stop`/`spawn` raise `NotImplementedError`: the turn runner
lands in a later task (task-6 brief, Step 3-5 note).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import cast

import anyio

from tradewind.application.config import TradewindConfig
from tradewind.application.ports import SessionStorePort
from tradewind.domain.errors import ConfigError, SessionNotFound, ToolMismatch
from tradewind.domain.events import Event
from tradewind.domain.models import (
    Profile,
    SessionOptions,
    SessionRow,
    StoredMessage,
    TierName,
    TurnResult,
)

StoreFactory = Callable[[Path], SessionStorePort]

_default_store_factory: StoreFactory | None = None


def _set_default_store_factory(factory: StoreFactory) -> None:
    """Assign the constant, import-time `SessionStorePort` constructor used
    when `StoreConfig.sqlite_path` is given instead of `StoreConfig.store`.

    Module-private: not part of the public API, and callers never invoke
    this directly. It is assigned exactly once, by `tradewind/__init__.py`
    at package import time — it is a layering seam (see the module
    docstring), not per-instance or per-call configuration. Every
    `Tradewind` in the process that resolves a store from a `sqlite_path`
    shares this one factory.
    """
    global _default_store_factory
    _default_store_factory = factory


def _resolve_store(config: TradewindConfig) -> SessionStorePort:
    if config.store.store is not None:
        return config.store.store
    sqlite_path = config.store.sqlite_path
    if sqlite_path is None:
        # Unreachable: TradewindConfig.model_post_init already guarantees
        # exactly one of sqlite_path/store is set.
        raise ConfigError("StoreConfig has neither sqlite_path nor store set")
    if _default_store_factory is None:
        raise ConfigError(
            "no sqlite store factory assigned; `import tradewind` (not just "
            "`tradewind.application.client`) before constructing Tradewind from a sqlite_path"
        )
    return _default_store_factory(sqlite_path)


def _validate_session_id(session_id: str) -> None:
    uuid.UUID(session_id)


def _resolve_profile(config: TradewindConfig, profile_name: str | None) -> tuple[str, Profile]:
    name = profile_name if profile_name is not None else config.default_profile
    if name not in config.profiles:
        raise ConfigError(f"unknown profile {name!r}")
    return name, config.profiles[name]


def _validate_tier(profile: Profile, tier: TierName | None) -> None:
    if tier is not None and tier not in profile.models:
        raise ConfigError(f"unknown tier {tier!r} for profile with models {sorted(profile.models)}")


def _snapshot_tool_names(snapshot: dict[str, object]) -> set[str]:
    tools = cast("list[dict[str, object]]", snapshot.get("tools", []))
    return {cast(str, tool["name"]) for tool in tools}


@dataclass
class Session:
    """A thin handle bound to the `Tradewind` instance that produced it.

    Carries only identity; all behaviour is delegated back to the owning
    client (`_client`), which holds the store, profiles, and config.
    """

    id: str
    _client: Tradewind

    async def run(self, prompt: str, **overrides: object) -> TurnResult:
        raise NotImplementedError("wired in turn runner task")

    def stream(self, prompt: str, **overrides: object) -> AsyncIterator[Event]:
        raise NotImplementedError("wired in turn runner task")

    async def stop(self) -> None:
        raise NotImplementedError("wired in turn runner task")

    async def spawn(self, prompt: str, *, tier: TierName | None = None) -> Session:
        raise NotImplementedError("wired in turn runner task")


class Tradewind:
    """Top-level client: owns the session store and hands out `Session`
    handles bound to itself. Construction opens/migrates the store and
    validates config; it never touches the network."""

    def __init__(self, config: TradewindConfig) -> None:
        self._config = config
        self._store = _resolve_store(config)
        self._store.migrate()

    async def aclose(self) -> None:
        """No resources to release yet; kept for `__aexit__` symmetry and
        so future store/connection teardown has a home."""

    async def __aenter__(self) -> Tradewind:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def _new_row(
        self, session_id: str, options: SessionOptions, *, profile_name: str, profile: Profile
    ) -> SessionRow:
        return SessionRow(
            session_id=session_id,
            backend=profile.backend,
            profile=profile_name,
            options_snapshot=cast("dict[str, object]", options.snapshot()),
            cwd=str(options.cwd) if options.cwd is not None else None,
            system_prompt=options.system_prompt,
        )

    async def create(self, session_id: str, options: SessionOptions) -> Session:
        _validate_session_id(session_id)
        profile_name, profile = _resolve_profile(self._config, options.profile)
        _validate_tier(profile, options.tier)
        row = self._new_row(session_id, options, profile_name=profile_name, profile=profile)
        await anyio.to_thread.run_sync(self._store.create_session, row)
        return Session(id=session_id, _client=self)

    async def resume(self, session_id: str, options: SessionOptions | None = None) -> Session:
        _validate_session_id(session_id)
        row = await anyio.to_thread.run_sync(self._store.get_session, session_id)
        if row is None:
            raise SessionNotFound(session_id)
        if options is not None:
            snapshot_tool_names = _snapshot_tool_names(
                cast("dict[str, object]", row.options_snapshot)
            )
            live_tool_names = {tool.name for tool in options.tools}
            if snapshot_tool_names != live_tool_names:
                raise ToolMismatch(
                    f"session {session_id!r}: resume tools {sorted(live_tool_names)} do not "
                    f"match snapshot tools {sorted(snapshot_tool_names)}"
                )
            profile_name = options.profile if options.profile is not None else row.profile
            _, profile = _resolve_profile(self._config, profile_name)
            _validate_tier(profile, options.tier)
            new_snapshot = cast("dict[str, object]", options.snapshot())
            await anyio.to_thread.run_sync(self._store.update_options, session_id, new_snapshot)
        return Session(id=session_id, _client=self)

    async def ensure(self, session_id: str, options: SessionOptions) -> Session:
        _validate_session_id(session_id)
        profile_name, profile = _resolve_profile(self._config, options.profile)
        _validate_tier(profile, options.tier)
        row = self._new_row(session_id, options, profile_name=profile_name, profile=profile)
        await anyio.to_thread.run_sync(self._store.ensure_session, row)
        return Session(id=session_id, _client=self)

    async def fork(self, src_session_id: str, dst_session_id: str) -> Session:
        _validate_session_id(src_session_id)
        _validate_session_id(dst_session_id)
        src_row = await anyio.to_thread.run_sync(self._store.get_session, src_session_id)
        if src_row is None:
            raise SessionNotFound(src_session_id)
        dst_row = SessionRow(
            session_id=dst_session_id,
            backend=src_row.backend,
            profile=src_row.profile,
            options_snapshot=cast("dict[str, object]", src_row.options_snapshot),
            cwd=src_row.cwd,
            system_prompt=src_row.system_prompt,
        )
        await anyio.to_thread.run_sync(self._store.copy_history, src_session_id, dst_row, None)
        return Session(id=dst_session_id, _client=self)

    async def history(
        self, session_id: str, *, include_children: bool = False, include_raw: bool = False
    ) -> list[StoredMessage]:
        _validate_session_id(session_id)
        return await anyio.to_thread.run_sync(
            lambda: self._store.history(
                session_id, include_children=include_children, include_raw=include_raw
            )
        )
