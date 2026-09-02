"""Tradewind: Agent infrastructure and policy execution engine.

Public surface: `Tradewind` (the client), `TradewindConfig` and its config
sub-models, and `Session`. This module sits outside the import-linter
`layers` contract (which only covers `tradewind.adapters` /
`tradewind.application` / `tradewind.domain`), so — unlike
`tradewind.application.client` — it may import the adapter that backs
`StoreConfig.sqlite_path` and, exactly once at import time, assign the
application layer's private store-factory seam
(`client._set_default_store_factory`) to build it (see
`tradewind.application.client` module docstring for the full rationale).
"""

from pathlib import Path

from tradewind.adapters.sqlite_store import SqliteSessionStore
from tradewind.application import client as _client
from tradewind.application.client import Session, Tradewind
from tradewind.application.config import (
    EventHook,
    NativeStoreConfig,
    StoreConfig,
    ToolHostConfig,
    TradewindConfig,
    TurnDefaults,
)
from tradewind.application.ports import SessionStorePort

__version__ = "0.1.0"


def _build_default_store(path: Path) -> SessionStorePort:
    return SqliteSessionStore(path)


_client._set_default_store_factory(_build_default_store)

__all__ = [
    "EventHook",
    "NativeStoreConfig",
    "Session",
    "StoreConfig",
    "ToolHostConfig",
    "Tradewind",
    "TradewindConfig",
    "TurnDefaults",
]
