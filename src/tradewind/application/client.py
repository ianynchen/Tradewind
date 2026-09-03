"""Application client: `Tradewind` (session lifecycle) and `Session` (a
thin per-session handle bound to it) (task-6 brief; turn wiring task-9).

Layering note (controller ruling): the application layer may not import
adapters (import-linter `layers` contract; GUIDELINES §8 "dependencies
flow inward"), but a `TradewindConfig.store` path needs a concrete
`SessionStorePort` built from it, and a session's `Profile.backend` needs a
concrete `Backend` adapter. Rather than construct either here, this module
exposes two module-private, constant import-time seams (GUIDELINES §8
permits mutation "as a deliberate choice with a stated reason"), each
assigned exactly once, at import time, by the top-level `tradewind`
package (outside the layers contract):

- `_set_default_store_factory`: builds `SqliteSessionStore`.
- `_set_backend_factories`: maps `BackendName -> Callable[[Profile,
  NativeStoreConfig], Backend]`; only `"langchain"` is registered as of
  task-9, the others arrive with their own adapter tasks.

Neither is per-instance state: every `Tradewind` in a process shares them.
`Tradewind.__init__` takes the config plus one optional keyword-only
`backend_factories` mapping (ADR-0002 amends the original single-argument
ruling): per-instance factory overrides consulted before the module
registry -- the public injection seam an embedder's no-network tests use
instead of monkeypatching this module's private map. It still has no
knowledge of which adapter modules back it. Per the task-9 ruling
("adapters are built lazily per profile, cached on the instance" --
component spec 01), each `Tradewind` instance keeps its own `_backends`
cache keyed by profile name, populated on first use via the shared (or
overridden) factory.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import cast

import anyio

from tradewind.application.config import NativeStoreConfig, TradewindConfig
from tradewind.application.ports import Backend, SessionStorePort
from tradewind.application.turn_runner import TurnRunner
from tradewind.domain.errors import (
    ConfigError,
    SessionNotFound,
    ToolMismatch,
    TurnExecutionFailed,
)
from tradewind.domain.events import Event, TurnCompleted, TurnFailed
from tradewind.domain.models import (
    BackendName,
    Profile,
    SessionOptions,
    SessionRow,
    StoredMessage,
    TierName,
    TurnResult,
)

StoreFactory = Callable[[Path], SessionStorePort]
BackendFactory = Callable[[Profile, NativeStoreConfig], Backend]

_default_store_factory: StoreFactory | None = None
_backend_factories: dict[BackendName, BackendFactory] = {}


def _set_default_store_factory(factory: StoreFactory) -> None:
    """Assign the constant, import-time `SessionStorePort` constructor used
    when `TradewindConfig.store` is a filesystem path — or, as `Path(":memory:")`,
    when it is None (the ephemeral mirror, FR-5.7) — rather than an
    already-built `SessionStorePort`.

    Module-private: not part of the public API, and callers never invoke
    this directly. It is assigned exactly once, by `tradewind/__init__.py`
    at package import time — it is a layering seam (see the module
    docstring), not per-instance or per-call configuration. Every
    `Tradewind` in the process that resolves a store from a `sqlite_path`
    shares this one factory.
    """
    global _default_store_factory
    _default_store_factory = factory


def _set_backend_factories(factories: dict[BackendName, BackendFactory]) -> None:
    """Assign the constant, import-time registry of `Backend` constructors,
    one per `BackendName`, used by every `Tradewind` instance in the
    process to build the adapter behind a session's profile.

    Module-private, assigned exactly once by `tradewind/__init__.py` at
    package import time (see the module docstring); replaces the whole
    registry rather than merging into it, matching `_set_default_store_
    factory`'s "assigned exactly once" contract.
    """
    global _backend_factories
    _backend_factories = dict(factories)


def _resolve_store(config: TradewindConfig) -> SessionStorePort:
    if isinstance(config.store, SessionStorePort):
        return config.store
    if _default_store_factory is None:
        raise ConfigError(
            "no sqlite store factory assigned; `import tradewind` (not just "
            "`tradewind.application.client`) before constructing Tradewind from a store path"
        )
    if config.store is None:
        # No store configured (FR-5.7): an ephemeral in-memory mirror.
        # sqlite ":memory:" is per-connection, and `SqliteSessionStore`
        # holds exactly one shared connection for its lifetime, so this is
        # a fully functional store that simply vanishes with the
        # `Tradewind` instance -- and two instances never share one.
        return _default_store_factory(Path(":memory:"))
    return _default_store_factory(config.store)


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

    Carries identity plus the live `SessionOptions` (tools, `mcp_servers`,
    `permission_broker`) supplied when this handle was acquired -- the
    parts I-2 forbids the store from persisting, so they only ever live on
    the in-memory handle that was given them. A bare `resume(session_id)`
    (no `options`) gets the empty default: that handle can still run turns,
    just without tools/a custom broker for the duration of this handle.
    Everything else behaviour needs (profile, tier, system_prompt,
    output_schema) is read back from the persisted session row on every
    call, so it stays correct regardless of how the handle was acquired.
    """

    id: str
    _client: Tradewind
    _options: SessionOptions = field(default_factory=lambda: SessionOptions())

    async def run(self, prompt: str, **overrides: object) -> TurnResult:
        result: TurnResult | None = None
        async for event in self.stream(prompt, **overrides):
            if isinstance(event, TurnCompleted):
                result = event.result
            elif isinstance(event, TurnFailed):
                raise TurnExecutionFailed(event.error)
        if result is None:
            # Unreachable in practice: TurnRunner.execute always yields one
            # of the two events above before its stream ends. Fails loud
            # rather than returning `None` through a `TurnResult`-typed API
            # if that invariant is ever broken.
            raise TurnExecutionFailed("turn ended without a result")
        return result

    def stream(self, prompt: str, **overrides: object) -> AsyncIterator[Event]:
        return self._client._turn_runner.execute(self.id, self._options, prompt, overrides)

    async def stop(self) -> None:
        await self._client._stop(self.id)

    async def compact(self, instructions: str | None = None) -> StoredMessage:
        """Manually compact this session's mirror transcript NOW (FR-5.8):
        older history is summarized into a checkpoint through the same
        machinery automatic compaction uses; `instructions` is appended to
        the summarization prompt as "Additional focus: ...". Available
        regardless of `CompactionSettings.auto` and without `ModelMeta`.
        Returns the persisted compaction record. The mirror keeps every
        row — compaction changes what is FED to the model, never what is
        stored.

        Failure modes: see `TurnRunner.compact` (SessionNotFound,
        Unsupported on native-resume backends, TurnInProgress mid-turn,
        CompactionFailed when the summarizer output is unusable).
        """
        return await self._client._turn_runner.compact(self.id, instructions)

    async def spawn(self, prompt: str, *, tier: TierName | None = None) -> Session:
        return await self._client._spawn(self, prompt, tier)


class Tradewind:
    """Top-level client: owns the session store and hands out `Session`
    handles bound to itself. Construction opens/migrates the store and
    validates config; it never touches the network."""

    def __init__(
        self,
        config: TradewindConfig,
        *,
        backend_factories: dict[BackendName, BackendFactory] | None = None,
    ) -> None:
        """`backend_factories` (keyword-only, optional; ADR-0002): per-name
        overrides consulted BEFORE the module-level registry, per instance.
        The front-door injection seam for an embedder's no-network tests —
        e.g. a `"langchain"` factory returning a `LangchainBackend` with a
        scripted `chat_model_factory` — replacing monkeypatching of the
        private `_backend_factories` map. Names not in the mapping fall
        through to the real registry, so overriding one backend leaves the
        others fully functional. Overrides are per-instance and never touch
        the module registry or other `Tradewind` instances."""
        self._config = config
        self._backend_factory_overrides: dict[BackendName, BackendFactory] = dict(
            backend_factories or {}
        )
        self._store = _resolve_store(config)
        self._store.migrate()
        self._backends: dict[str, Backend] = {}
        self._turn_runner = TurnRunner(
            store=self._store, config=config, resolve_backend=self._resolve_backend
        )

    def _resolve_backend(self, profile_name: str, profile: Profile) -> Backend:
        cached = self._backends.get(profile_name)
        if cached is not None:
            return cached
        factory = self._backend_factory_overrides.get(profile.backend) or _backend_factories.get(
            profile.backend
        )
        if factory is None:
            raise ConfigError(
                f"no backend factory registered for backend {profile.backend!r}; "
                "`import tradewind` before constructing Tradewind"
            )
        backend = factory(profile, self._config.native_stores)
        self._backends[profile_name] = backend
        return backend

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
        await anyio.to_thread.run_sync(self._store.sweep_stale_turns, session_id)
        return Session(id=session_id, _client=self, _options=options)

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
        await anyio.to_thread.run_sync(self._store.sweep_stale_turns, session_id)
        return Session(
            id=session_id,
            _client=self,
            _options=options if options is not None else SessionOptions(),
        )

    async def ensure(self, session_id: str, options: SessionOptions) -> Session:
        _validate_session_id(session_id)
        profile_name, profile = _resolve_profile(self._config, options.profile)
        _validate_tier(profile, options.tier)
        row = self._new_row(session_id, options, profile_name=profile_name, profile=profile)
        await anyio.to_thread.run_sync(self._store.ensure_session, row)
        await anyio.to_thread.run_sync(self._store.sweep_stale_turns, session_id)
        return Session(id=session_id, _client=self, _options=options)

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

    async def _stop(self, session_id: str) -> None:
        row = await anyio.to_thread.run_sync(self._store.get_session, session_id)
        if row is None:
            raise SessionNotFound(session_id)
        _, profile = _resolve_profile(self._config, row.profile)
        backend = self._resolve_backend(row.profile, profile)
        await self._turn_runner.request_stop(session_id, backend)

    async def _spawn(self, parent: Session, prompt: str, tier: TierName | None) -> Session:
        parent_row = await anyio.to_thread.run_sync(self._store.get_session, parent.id)
        if parent_row is None:
            raise SessionNotFound(parent.id)
        _, profile = _resolve_profile(self._config, parent_row.profile)
        _validate_tier(profile, tier)
        child_id = str(uuid.uuid4())
        child_row = SessionRow(
            session_id=child_id,
            backend=parent_row.backend,
            profile=parent_row.profile,
            options_snapshot=cast("dict[str, object]", parent_row.options_snapshot),
            parent_session_id=parent.id,
            spawn_kind="subagent",
            # Message-level spawn linkage (which assistant tool_use item
            # triggered this child) has no id to attach yet -- `spawn()`
            # is a plain method call, not itself driven by a tool call
            # event -- documented deferral (task-9 brief), not an omission.
            spawned_by_message_id=None,
            cwd=parent_row.cwd,
            system_prompt=parent_row.system_prompt,
        )
        await anyio.to_thread.run_sync(self._store.create_session, child_row)
        await anyio.to_thread.run_sync(self._store.sweep_stale_turns, child_id)
        child = Session(id=child_id, _client=self, _options=parent._options)
        overrides: dict[str, object] = {"tier": tier} if tier is not None else {}
        await child.run(prompt, **overrides)
        return child
