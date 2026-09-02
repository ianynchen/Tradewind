"""Tests for `ClaudeBackend` instance behaviour that isn't a pure mapping
function -- `interrupt()`'s handling of a client registered before
`ClaudeSDKClient.connect()` finishes (task-10 fix round 1), and
`take_native_session_id()`'s per-session pop semantics (task-11 review, fix
round 1). Pure event mapping lives in `tests/unit/test_claude_mapping.py`;
a real live session is `tests/conformance/test_claude.py` /
`tests/integration/test_claude_live.py` (`@pytest.mark.integration`).
"""

from __future__ import annotations

from typing import Any, cast

from claude_agent_sdk import CLIConnectionError

from tradewind.adapters.claude_backend import ClaudeBackend
from tradewind.application.config import NativeStoreConfig
from tradewind.domain.models import ModelSpec, Profile, SubscriptionAuth


def _profile() -> Profile:
    return Profile(
        backend="claude",
        auth=SubscriptionAuth(),
        models={"standard": ModelSpec(model="claude-sonnet-4-5")},
    )


class _RaisesConnectionErrorClient:
    """Stands in for a `ClaudeSDKClient` registered in `_clients` before its
    `connect()` has resolved: `ClaudeSDKClient.interrupt()` raises
    `CLIConnectionError` in exactly that state (`client.py`: "Not connected.
    Call connect() first.")."""

    async def interrupt(self) -> None:
        raise CLIConnectionError("Not connected. Call connect() first.")


async def test_interrupt_is_a_noop_when_no_client_is_registered() -> None:
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    await backend.interrupt("no-such-session")  # must not raise


async def test_interrupt_swallows_cliconnectionerror_from_a_not_yet_connected_client() -> None:
    backend = ClaudeBackend(_profile(), NativeStoreConfig())
    backend._clients["sess-1"] = cast(Any, _RaisesConnectionErrorClient())

    await backend.interrupt("sess-1")  # must not raise


# --- take_native_session_id: per-session pop, not a shared attribute
# (task-11 review, fix round 1: a single shared `last_native_session_id`
# attribute let one session's native id rehome ANOTHER session sharing the
# same cached-per-profile backend instance) ---


def test_take_native_session_id_pops_and_second_call_returns_none() -> None:
    backend = ClaudeBackend(_profile(), NativeStoreConfig())
    backend._native_ids["sess-1"] = "native-abc"

    first = backend.take_native_session_id("sess-1")
    second = backend.take_native_session_id("sess-1")

    assert first == "native-abc"
    assert second is None


def test_take_native_session_id_is_scoped_per_session() -> None:
    # One `ClaudeBackend` instance is cached and reused across every
    # session on its profile (`Tradewind._resolve_backend`) -- a value
    # recorded for one session_id must never be handed back for another's.
    backend = ClaudeBackend(_profile(), NativeStoreConfig())
    backend._native_ids["sess-a"] = "native-a"

    assert backend.take_native_session_id("sess-b") is None
    assert backend.take_native_session_id("sess-a") == "native-a"


def test_take_native_session_id_returns_none_when_nothing_recorded() -> None:
    backend = ClaudeBackend(_profile(), NativeStoreConfig())

    assert backend.take_native_session_id("no-such-session") is None
