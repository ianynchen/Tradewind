"""Runs the conformance matrix (`tests/conformance/matrix.py`) against
`CodexBackend` over a REAL `codex app-server` subprocess (task-14 brief) --
every test here is `@pytest.mark.integration`: it shells out to the bundled
Codex CLI runtime and talks to the live API (ChatGPT subscription auth on
this machine, per `docs/research/2026-09-02-codex-approvals-spike.md`),
unlike `tests/unit/test_codex_mapping.py`'s pure, offline mapping tests.

Same scripting mechanism as `tests/conformance/test_claude.py` (its own
module docstring explains the full mechanics and the two structural gaps
this reuses verbatim): there is no injectable fake "model" seam for a real
`CodexClient` subprocess, so each scenario's exact expected reply is smuggled
in as a per-turn `system_prompt` override (`CodexHarness.script_*` queues one
instruction string per upcoming turn; `_ScriptedSession.run`/`.stream` pop it
and pass it through `overrides["system_prompt"]`, which `_drive_turn` wires
to `base_instructions`).

`tool_allow_deny` is expected to SKIP here, not run: `matrix.py`'s own gate
is `supports_interactive_permissions AND supports_in_process_tools`, and
`CodexBackend.capabilities().supports_in_process_tools` is `False` per the
brief's explicit capability table (Codex has no in-process tool bridge --
tools reach it only through the `tradewind.toolproxy` stdio shim). This is a
real gap worth flagging: the scenario's own behaviour (broker gates a tool
call regardless of delivery mechanism) does NOT actually depend on
"in-process" vs "shim", so `matrix.py`'s skip condition is testing the wrong
thing for a shim-only backend -- flagged in the task-14 report for
controller follow-up, not fixed here (`matrix.py` is shared test
infrastructure, out of this task's file scope; ripping it up unilaterally
would be exactly the kind of unrequested change GUIDELINES §7 warns against).
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
from tradewind.adapters.codex_backend import CodexBackend
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

# Same opt-in rationale as `test_claude.py`'s own module docstring: real,
# billed API calls and a real subprocess spawn on every test here, so this
# module is gated behind an explicit env var rather than running on every
# plain `uv run pytest` (which `scripts/check.sh` does not filter
# `-m "not integration"` out of).
if not os.environ.get("TRADEWIND_RUN_CODEX_INTEGRATION"):
    pytest.skip(
        "set TRADEWIND_RUN_CODEX_INTEGRATION=1 to run these against a real codex "
        "app-server session (billed API calls, requires local ChatGPT auth)",
        allow_module_level=True,
    )

# Cheapest model confirmed to exist on this machine's Codex model list
# (`~/.codex/models_cache.json`) at task-14 time -- kept deliberately small
# since every test here is a real, billed API call.
_MODEL = "gpt-5.4-mini"


def _profile() -> Profile:
    return Profile(
        backend="codex",
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


class CodexHarness:
    """`matrix.ConformanceHarness` for `CodexBackend`, driven through the
    real `Tradewind`/`Session`/`TurnRunner` stack against a live subprocess
    (module docstring explains the per-turn `system_prompt` scripting
    mechanism this class implements)."""

    def __init__(self, tmp_path: Path) -> None:
        self.capabilities: Capabilities = CodexBackend(
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
    as this call's `system_prompt` override (module docstring)."""

    def __init__(self, inner: Session, harness: CodexHarness) -> None:
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
    def __init__(self, config: TradewindConfig, harness: CodexHarness) -> None:
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
        # into instead). Identical to `test_claude.py`'s own copy.
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
def harness(tmp_path: Path) -> CodexHarness:
    return CodexHarness(tmp_path)


# --- the matrix, run against a live codex app-server session ---


async def test_single_turn_text(harness: CodexHarness) -> None:
    await matrix.single_turn_text(cast(Any, harness))


async def test_tool_allow_deny(harness: CodexHarness) -> None:
    # Expected to skip: `supports_in_process_tools` is False (module
    # docstring) -- exercises the skip machinery, not tool gating.
    await matrix.tool_allow_deny(cast(Any, harness))


async def test_interrupt_midturn(harness: CodexHarness) -> None:
    await matrix.interrupt_midturn(cast(Any, harness))


async def test_resume_continues_context(harness: CodexHarness) -> None:
    await matrix.resume_continues_context(cast(Any, harness))


async def test_history_flat_and_tree(harness: CodexHarness) -> None:
    await matrix.history_flat_and_tree(cast(Any, harness))


async def test_structured_output(harness: CodexHarness) -> None:
    await matrix.structured_output(cast(Any, harness))


async def test_system_prompt_respected(harness: CodexHarness) -> None:
    await matrix.system_prompt_respected(cast(Any, harness))
