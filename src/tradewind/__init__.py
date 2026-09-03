"""Tradewind: Agent infrastructure and policy execution engine.

Public surface: `Tradewind` (the client), `TradewindConfig` and its config
sub-models, and `Session`. This module sits outside the import-linter
`layers` contract (which only covers `tradewind.adapters` /
`tradewind.application` / `tradewind.domain`), so — unlike
`tradewind.application.client` — it may import the adapters that back
a path-valued `TradewindConfig.store` and each `Profile.backend` and, exactly once at
import time, assign the application layer's private factory seams
(`client._set_default_store_factory`, `client._set_backend_factories`) to
build them (see `tradewind.application.client` module docstring for the
full rationale).
"""

from pathlib import Path

from tradewind.adapters.claude_backend import ClaudeBackend
from tradewind.adapters.codex_backend import CodexBackend
from tradewind.adapters.cursor_backend import CursorBackend
from tradewind.adapters.langchain_backend import LangchainBackend
from tradewind.adapters.sqlite_store import SqliteSessionStore
from tradewind.application import client as _client
from tradewind.application.client import Session, Tradewind
from tradewind.application.config import (
    EventHook,
    NativeStoreConfig,
    ToolHostConfig,
    TradewindConfig,
    TurnDefaults,
)
from tradewind.application.ports import Backend, SessionStorePort
from tradewind.domain.models import Profile

__version__ = "0.5.0"


def _build_default_store(path: Path) -> SessionStorePort:
    return SqliteSessionStore(path)


def _build_langchain_backend(profile: Profile, native_config: NativeStoreConfig) -> Backend:
    return LangchainBackend(profile, native_config)


def _build_claude_backend(profile: Profile, native_config: NativeStoreConfig) -> Backend:
    return ClaudeBackend(profile, native_config)


def _build_codex_backend(profile: Profile, native_config: NativeStoreConfig) -> Backend:
    return CodexBackend(profile, native_config)


def _build_cursor_backend(profile: Profile, native_config: NativeStoreConfig) -> Backend:
    return CursorBackend(profile, native_config)


_client._set_default_store_factory(_build_default_store)
_client._set_backend_factories(
    {
        "langchain": _build_langchain_backend,
        "claude": _build_claude_backend,
        "codex": _build_codex_backend,
        "cursor": _build_cursor_backend,
    }
)

__all__ = [
    "EventHook",
    "NativeStoreConfig",
    "Session",
    "ToolHostConfig",
    "Tradewind",
    "TradewindConfig",
    "TurnDefaults",
]
