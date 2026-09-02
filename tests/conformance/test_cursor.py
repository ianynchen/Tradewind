"""Runs the conformance matrix (`tests/conformance/matrix.py`) against
`CursorBackend` over a REAL `cursor-sdk-bridge` subprocess (task-15 brief) --
mirrors `tests/conformance/test_codex.py`'s own structure and its module
docstring's full rationale for the scripting mechanism (no injectable fake
"model" seam for a real SDK client, so each scenario's exact expected reply
is smuggled in as a per-turn `system_prompt` override; `CursorHarness.
script_*` queues one instruction string per upcoming turn, and `_ScriptedSession.
run`/`.stream` pop it into `overrides["system_prompt"]`).

**PERMANENTLY BLOCKED on this machine: no Cursor subscription exists (P-5
open, `docs/ARCHITECTURE.md` §7).** Both module-level skips below fire
before a single test in this file is collected as runnable -- this module
is discoverable (imports cleanly, shows up as one "skipped" module in a
`pytest` run) but never actually executes, honoring the task-15 brief's
explicit instruction to mark this BLOCKED rather than fake it. The
env-gated skip (`TRADEWIND_RUN_CURSOR_INTEGRATION`) is written the same way
`test_codex.py`/`test_claude.py` gate their own live runs and is left in
place, dead for now, so that once a Cursor subscription exists AND the P-5
spike (`docs/research/2026-09-XX-cursor-cli-resume-spike.md`, not yet
written -- Step 1 of the task-15 brief) has actually run, a future task can
delete just the unconditional skip immediately below and this module
starts working exactly like its Codex/Claude siblings, with no other
changes needed here.

Everything below the two skips is therefore unverified against a real
`cursor-sdk-bridge` process -- written directly against the pinned SDK's
installed types (same as `cursor_backend.py` itself), not exercised.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import anyio
import pytest

from tests.conformance import matrix
from tradewind.adapters.cursor_backend import CursorBackend
from tradewind.application import client as _client_module
from tradewind.application.client import Session, Tradewind
from tradewind.application.config import NativeStoreConfig, StoreConfig, TradewindConfig
from tradewind.domain.errors import SessionNotFound
from tradewind.domain.events import Event, ItemCompleted
from tradewind.domain.models import (
    Capabilities,
    ModelSpec,
    Profile,
    SessionOptions,
    SessionRow,
    SubscriptionAuth,
    TierName,
    TurnResult,
)

pytestmark = pytest.mark.integration

# Governs TODAY regardless of the env-gate below (module docstring): no
# Cursor subscription exists on this machine to run any of this against.
# Delete this one skip -- and only this one -- once that changes and the
# P-5 spike has actually run.
pytest.skip(
    "BLOCKED: no cursor subscription (P-5 open, docs/ARCHITECTURE.md §7) -- "
    "remove this skip once a Cursor subscription exists and the P-5 spike "
    "(docs/research/2026-09-XX-cursor-cli-resume-spike.md) has run live",
    allow_module_level=True,
)

# Same opt-in rationale as `test_codex.py`/`test_claude.py`'s own module
# docstrings: real local-agent runs and a real subprocess spawn on every
# test here, so this module is gated behind an explicit env var rather than
# running on every plain `uv run pytest` (which `scripts/check.sh` does not
# filter `-m "not integration"` out of) -- dead code today (the skip above
# always fires first), kept so this module needs no other change once
# unblocked.
if not os.environ.get("TRADEWIND_RUN_CURSOR_INTEGRATION"):
    pytest.skip(
        "set TRADEWIND_RUN_CURSOR_INTEGRATION=1 to run these against a real Cursor "
        "local agent session (requires a Cursor subscription)",
        allow_module_level=True,
    )

_MODEL = "composer-2"


def _profile() -> Profile:
    return Profile(
        backend="cursor",
        auth=SubscriptionAuth(),
        models={"standard": ModelSpec(model=_MODEL)},
    )


def _verbatim_instruction(text: str) -> str:
    return (
        "For the very next message from the user in this conversation, "
        "regardless of what it says: do not call any tool, and reply with "
        f"exactly this text and nothing else -- no greeting, no punctuation "
        f"added, no markdown, no explanation: {text}"
    )


def _tool_calls_instruction(tool_calls: list[matrix.ScriptedToolCall], final_text: str) -> str:
    calls = "; ".join(
        f"a tool named exactly {call.name!r} with input exactly {call.args!r}"
        for call in tool_calls
    )
    return (
        f"For the very next message from the user in this conversation: call {calls}. "
        "Call every one of them -- some may come back denied by the permission system; "
        "that is an expected, normal tool result, not an error, so do not stop or retry "
        "for it. Once you have a result (allowed or denied) for every one of them, reply "
        f"with exactly this text and nothing else: {final_text}"
    )


def _blocking_instruction() -> str:
    return (
        "For the very next message from the user in this conversation, ignore its content "
        "and instead count out loud from one to one thousand, one number per line and "
        "nothing else. Do not stop early, do not summarize -- keep counting until you are "
        "cut off."
    )


class CursorHarness:
    """`matrix.ConformanceHarness` for `CursorBackend`, driven through the
    real `Tradewind`/`Session`/`TurnRunner` stack against a live subprocess
    (module docstring explains the per-turn `system_prompt` scripting
    mechanism this class implements -- identical to `CodexHarness`'s/
    `ClaudeHarness`'s own copy)."""

    def __init__(self, tmp_path: Path) -> None:
        self.capabilities: Capabilities = CursorBackend(
            _profile(), NativeStoreConfig()
        ).capabilities()
        self._pending: list[str | None] = []
        self._blocking_started: anyio.Event | None = None
        config = TradewindConfig(
            profiles={"default": _profile()},
            default_profile="default",
            store=StoreConfig(sqlite_path=tmp_path / "sessions.db"),
            on_event=self._on_event,
        )
        self.tradewind: Tradewind = _ScriptedTradewind(config, self)

    def _pop_instruction(self) -> str | None:
        return self._pending.pop(0) if self._pending else None

    def _on_event(self, event: Event) -> None:
        if self._blocking_started is not None and isinstance(event, ItemCompleted):
            self._blocking_started.set()

    def script_text_response(self, text: str) -> None:
        self._pending.append(_verbatim_instruction(text))

    def script_tool_calls_then_text(
        self, tool_calls: list[matrix.ScriptedToolCall], final_text: str
    ) -> None:
        self._pending.append(_tool_calls_instruction(tool_calls, final_text))

    def script_blocking(self) -> anyio.Event:
        started = anyio.Event()
        self._blocking_started = started
        self._pending.append(_blocking_instruction())
        return started

    def script_echo_system_prompt(self) -> None:
        self._pending.append(None)


class _ScriptedSession:
    """Wraps a real `Session`, injecting `harness`'s next queued instruction
    as this call's `system_prompt` override (module docstring). Even though
    `CursorBackend.capabilities().supports_system_prompt` is False, the
    override still reaches the model: `turn_runner._emulate_system_prompt`
    (task-15) folds it into the actual prompt text above the port whenever a
    backend can't natively accept one -- the same smuggling channel, one
    layer further out."""

    def __init__(self, inner: Session, harness: CursorHarness) -> None:
        self._inner = inner
        self._harness = harness

    @property
    def id(self) -> str:
        return self._inner.id

    async def run(self, prompt: str, **overrides: object) -> TurnResult:
        overrides.setdefault("system_prompt", self._harness._pop_instruction())
        return await self._inner.run(prompt, **overrides)

    def stream(self, prompt: str, **overrides: object) -> AsyncIterator[Event]:
        overrides.setdefault("system_prompt", self._harness._pop_instruction())
        return self._inner.stream(prompt, **overrides)

    async def stop(self) -> None:
        await self._inner.stop()

    async def spawn(self, prompt: str, *, tier: TierName | None = None) -> _ScriptedSession:
        child = await self._inner.spawn(prompt, tier=tier)
        return _ScriptedSession(child, self._harness)


class _ScriptedTradewind(Tradewind):
    def __init__(self, config: TradewindConfig, harness: CursorHarness) -> None:
        super().__init__(config)
        self._harness = harness

    async def create(self, session_id: str, options: SessionOptions) -> Session:
        inner = await super().create(session_id, options)
        return cast(Session, _ScriptedSession(inner, self._harness))

    async def resume(self, session_id: str, options: SessionOptions | None = None) -> Session:
        inner = await super().resume(session_id, options)
        return cast(Session, _ScriptedSession(inner, self._harness))

    async def ensure(self, session_id: str, options: SessionOptions) -> Session:
        inner = await super().ensure(session_id, options)
        return cast(Session, _ScriptedSession(inner, self._harness))

    async def _spawn(self, parent: Session, prompt: str, tier: TierName | None) -> Session:
        # `Tradewind._spawn`'s own body (client.py), reimplemented verbatim
        # plus the one extra line injecting this harness's next queued
        # instruction -- see module docstring for why (no override
        # parameter exists on `Session.spawn()`/`Tradewind._spawn` to hook
        # into instead). Identical to `test_codex.py`'s/`test_claude.py`'s
        # own copy.
        parent_row = await anyio.to_thread.run_sync(self._store.get_session, parent.id)
        if parent_row is None:
            raise SessionNotFound(parent.id)
        _, profile = _client_module._resolve_profile(self._config, parent_row.profile)
        _client_module._validate_tier(profile, tier)
        child_id = str(uuid.uuid4())
        child_row = SessionRow(
            session_id=child_id,
            backend=parent_row.backend,
            profile=parent_row.profile,
            options_snapshot=cast("dict[str, object]", parent_row.options_snapshot),
            parent_session_id=parent.id,
            spawn_kind="subagent",
            spawned_by_message_id=None,
            cwd=parent_row.cwd,
            system_prompt=parent_row.system_prompt,
        )
        await anyio.to_thread.run_sync(self._store.create_session, child_row)
        await anyio.to_thread.run_sync(self._store.sweep_stale_turns, child_id)
        child = Session(id=child_id, _client=self, _options=parent._options)
        overrides: dict[str, object] = {"tier": tier} if tier is not None else {}
        instruction = self._harness._pop_instruction()
        if instruction is not None:
            overrides["system_prompt"] = instruction
        await child.run(prompt, **overrides)
        return child


@pytest.fixture
def harness(tmp_path: Path) -> CursorHarness:
    return CursorHarness(tmp_path)


# --- the matrix, run against a live cursor-sdk-bridge session ---


async def test_single_turn_text(harness: CursorHarness) -> None:
    await matrix.single_turn_text(cast(Any, harness))


async def test_tool_allow_deny(harness: CursorHarness) -> None:
    await matrix.tool_allow_deny(cast(Any, harness))


async def test_interrupt_midturn(harness: CursorHarness) -> None:
    await matrix.interrupt_midturn(cast(Any, harness))


async def test_resume_continues_context(harness: CursorHarness) -> None:
    await matrix.resume_continues_context(cast(Any, harness))


async def test_history_flat_and_tree(harness: CursorHarness) -> None:
    await matrix.history_flat_and_tree(cast(Any, harness))


async def test_structured_output(harness: CursorHarness) -> None:
    await matrix.structured_output(cast(Any, harness))


async def test_system_prompt_respected(harness: CursorHarness) -> None:
    # Capability-gated in matrix.py: `CursorBackend.capabilities().
    # supports_system_prompt` is False, so this scenario always skips (R-1:
    # emulation lives above the port, not advertised as a native capability).
    await matrix.system_prompt_respected(cast(Any, harness))
