"""Conformance matrix: one scenario function per named behaviour every
backend is expected to honor (task-9 brief). Each function takes a
`ConformanceHarness` and is guarded by `pytest.skip` on the `Capabilities`
flag it needs -- a backend that doesn't support a scenario's precondition
skips it rather than failing (`structured_output` skips on `langchain`
today; that is the correct, expected result and exercises the skip
machinery itself).

Every assertion here is in terms of Tradewind's own normalized surface
(`Session`, `TurnResult`, `Event`, `StoredMessage`) so scenarios stay
backend-agnostic; only the *scripting* knobs on `ConformanceHarness` are
backend-specific by necessity (each `tests/conformance/test_<backend>.py`
supplies its own concrete harness wired to a scripted/fake double for that
backend -- see `tests/conformance/test_langchain.py`).

Task-11 generalization (controller-expanded scope, task-10 report's live-run
findings): these scenarios ran only against `langchain`'s fake chat models
until task 11 -- run live against `ClaudeBackend`
(`tests/conformance/test_claude.py`), several failed for reasons that were
never bugs in the adapter, only in scenario assumptions baked in against a
scripted double:

  - History assertions were exact-list comparisons that implicitly assumed
    "no `kind==\"thinking\"` item ever appears" -- true of every fake model
    used here, false of a real model under extended thinking (`ClaudeBackend`
    emits one on essentially every turn). `_non_thinking()` below filters
    those out before any history assertion; what's asserted afterward is
    invariants (prompt present, assistant reply present, ordering) rather
    than a snapshot of the exact item list.
  - `tool_allow_deny` declared tools with `input_schema={"type": "object"}`
    -- no `properties` -- giving a real model no schema-level anchor for a
    call's shape, so it substituted its own guessed input instead of the
    scripted one. Both tools now declare a real `properties`/`required`
    shape, and the scenario asserts gating *behavior* (which handler ran,
    which didn't, that the denial produced an error result keyed to the
    right tool call) instead of an exact `tool_input`.
  - `system_prompt_respected` asked a real model to reproduce its system
    prompt byte-for-byte as its entire reply -- structurally impossible
    against `ClaudeBackend`'s preset+append system-prompt shape (its own
    module docstring). It now asks for a fixed token to appear in the reply
    instead, which both a scripted fake and a real model reliably satisfy.

`structured_output` is unchanged: it stays purely capability-gated (skips on
any backend with `supports_structured_output=False`) rather than behavioral,
since no backend advertises that capability yet to exercise against.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol, cast

import anyio
import pytest

from tradewind.application.client import Tradewind
from tradewind.domain.events import (
    Event,
    ItemCompleted,
    PermissionRequested,
    TurnCompleted,
    TurnFailed,
)
from tradewind.domain.models import Capabilities, SessionOptions, StoredMessage, Tool, Verdict


def _non_thinking(messages: list[StoredMessage]) -> list[StoredMessage]:
    """Drop `kind==\"thinking\"` items before a history assertion.

    A real model may interleave extended-thinking blocks that no scenario
    here scripts for or cares about (module docstring) -- `ClaudeBackend`
    persists them faithfully (`kind=\"thinking\"` `ItemCompleted`, per its
    own mapping spec), so filtering here, not suppressing them in the
    adapter, is what keeps this scenario backend-agnostic.
    """
    return [m for m in messages if m.kind != "thinking"]


@dataclass(frozen=True)
class ScriptedToolCall:
    """One tool call a scripted backend response should emit."""

    name: str
    args: dict[str, object]
    id: str


class ConformanceHarness(Protocol):
    """What each `tests/conformance/test_<backend>.py` module supplies to
    drive the scenarios below against its own backend + fixture (a fake
    model, a mocked SDK, ...)."""

    tradewind: Tradewind
    capabilities: Capabilities

    def script_text_response(self, text: str) -> None:
        """Queue the backend double to answer the next turn with plain
        text `text` and no tool calls."""
        ...

    def script_tool_calls_then_text(
        self, tool_calls: list[ScriptedToolCall], final_text: str
    ) -> None:
        """Queue a two-step turn: one assistant response issuing every call
        in `tool_calls` at once, then (after all tool results round-trip) a
        final plain-text response `final_text`."""
        ...

    def script_blocking(self) -> anyio.Event:
        """Queue a response that blocks until the turn is interrupted.
        Returns an `anyio.Event` that is set once the backend double has
        actually started handling the (now in-flight) call -- scenarios
        await it before calling `session.stop()` so the interrupt lands
        deterministically, never via a sleep (GUIDELINES §10)."""
        ...

    def script_echo_system_prompt(self) -> None:
        """Queue a response whose text is exactly the `system_prompt` the
        backend double received for that turn (or a fixed sentinel when
        none was sent), so a scenario can assert the system prompt actually
        reached the backend without inspecting its internals."""
        ...


async def single_turn_text(harness: ConformanceHarness) -> None:
    """A turn with no tools completes and its exchange lands in history in
    order -- the baseline every backend must support.

    History is asserted as invariants, not an exact list (module
    docstring): the prompt is present as the first non-thinking item, and
    exactly one assistant `text` item follows it with the scripted reply.
    """
    harness.script_text_response("hello there")
    session_id = str(uuid.uuid4())
    session = await harness.tradewind.create(session_id, SessionOptions())

    result = await session.run("hi")

    assert result.status == "completed"
    assert result.final_text == "hello there"
    history = _non_thinking(await harness.tradewind.history(session_id))
    assert history[0].role == "user"
    assert history[0].content.get("text") == "hi"
    assistant_texts = [m for m in history if m.role == "assistant" and m.kind == "text"]
    assert len(assistant_texts) == 1
    assert assistant_texts[0].content.get("text") == "hello there"


async def tool_allow_deny(harness: ConformanceHarness) -> None:
    """The broker gates each tool call independently: an allowed call's
    handler executes; a denied call's handler never runs and is surfaced
    as `PermissionRequested` plus an error `tool_result` keyed to the right
    tool call (FR-4.1).

    Both tools declare a real `properties`/`required` JSON Schema (module
    docstring: a bare `{"type": "object"}` gives a real model no anchor for
    a call's shape, so it substitutes its own guess) -- this scenario
    therefore asserts gating *behavior*, not an exact `tool_input`, since a
    live model's exact argument choice is not something this matrix
    controls or cares about.

    Gated on `supports_interactive_permissions` alone (task-14 fix round 1)
    -- the only capability this scenario actually exercises (whether the
    broker gates a tool call at all). `supports_in_process_tools` used to be
    ANDed in here too, but that flag is about *delivery mechanism* (in-
    process bridge vs. an out-of-process subprocess shim), which this
    scenario doesn't care about and never asserts on -- it silently skipped
    every shim-only backend (codex, `supports_in_process_tools=False`) even
    though broker gating works identically for it (`ToolHost.call()`'s own
    authoritative gate, `tool_host.py`).
    """
    if not harness.capabilities.supports_interactive_permissions:
        pytest.skip("backend does not support broker-gated tool calls")

    allowed_calls: list[dict[str, object]] = []
    denied_calls: list[dict[str, object]] = []

    async def allowed_handler(**kwargs: object) -> str:
        allowed_calls.append(kwargs)
        return "ok"

    async def denied_handler(**kwargs: object) -> str:
        denied_calls.append(kwargs)
        return "should never run"

    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
    allowed = Tool(
        name="allowed_tool", description="d", input_schema=schema, handler=allowed_handler
    )
    denied = Tool(name="denied_tool", description="d", input_schema=schema, handler=denied_handler)

    class _Broker:
        async def decide(self, tool_name: str, _tool_input: dict[str, object]) -> Verdict:
            return "deny" if tool_name == "denied_tool" else "allow"

    harness.script_tool_calls_then_text(
        [
            ScriptedToolCall(name="allowed_tool", args={"x": "a"}, id="call_a"),
            ScriptedToolCall(name="denied_tool", args={"x": "b"}, id="call_b"),
        ],
        final_text="done",
    )

    session_id = str(uuid.uuid4())
    session = await harness.tradewind.create(
        session_id, SessionOptions(tools=[allowed, denied], permission_broker=_Broker())
    )

    events = [event async for event in session.stream("go")]

    assert len(allowed_calls) == 1
    assert denied_calls == []

    permission_events = [e for e in events if isinstance(e, PermissionRequested)]
    assert len(permission_events) == 1
    assert permission_events[0].tool_name == "denied_tool"
    assert permission_events[0].verdict == "deny"

    # Resolve `denied_tool`'s own tool_use id from the mirror rather than
    # assuming the scripted "call_b" (a real model mints its own id; only
    # the langchain fake harness happens to honor the scripted one) --
    # backend-agnostic by construction. Matched by suffix, not equality:
    # `ClaudeBackend` renders the wire tool name MCP-namespaced
    # (`mcp__tradewind__denied_tool`, its own module docstring) in the
    # `tool_use` item's `content["name"]` -- unlike `PermissionRequested.
    # tool_name` above, which that adapter strips back to the bare name
    # before eventizing it (`_strip_server_prefix`) -- while `langchain`
    # never prefixes at all, so the caller-declared name is always at least
    # a suffix of whatever wire name ends up in the mirror.
    completed = [e.message for e in events if isinstance(e, ItemCompleted)]
    tool_use_id_by_name = {
        cast(str, m.content.get("name", "")): m.content.get("id")
        for m in completed
        if m.kind == "tool_use"
    }
    denied_id = next(
        tool_id for name, tool_id in tool_use_id_by_name.items() if name.endswith("denied_tool")
    )
    tool_results_by_id = {
        m.content.get("tool_use_id"): m for m in completed if m.kind == "tool_result"
    }
    assert denied_id in tool_results_by_id
    assert tool_results_by_id[denied_id].content.get("is_error") is True

    # Invariant: `tool_use` precedes its own matching `tool_result` in the
    # event stream.
    def _event_index(kind: str) -> int:
        return next(
            i
            for i, e in enumerate(events)
            if isinstance(e, ItemCompleted)
            and e.message.kind == kind
            and (
                e.message.content.get("id")
                if kind == "tool_use"
                else e.message.content.get("tool_use_id")
            )
            == denied_id
        )

    assert _event_index("tool_use") < _event_index("tool_result")

    assert isinstance(events[-1], TurnCompleted)
    assert events[-1].result.final_text == "done"


async def interrupt_midturn(harness: ConformanceHarness) -> None:
    """`session.stop()` ends an in-flight turn as `interrupted`, never as
    `TurnFailed` (FR-6.2)."""
    started = harness.script_blocking()
    session_id = str(uuid.uuid4())
    session = await harness.tradewind.create(session_id, SessionOptions())

    events: list[Event] = []

    async def consume() -> None:
        async for event in session.stream("hi"):
            events.append(event)

    async with anyio.create_task_group() as tg:
        tg.start_soon(consume)
        await started.wait()
        await session.stop()

    assert not any(isinstance(e, TurnFailed) for e in events)
    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert len(completed) == 1
    assert completed[0].result.status == "interrupted"


async def resume_continues_context(harness: ConformanceHarness) -> None:
    """A turn run after `resume()` sees the prior turn's exchange in its
    rebuilt context -- the store is the conversation (component spec
    "History" decision), so a fresh handle on the same session still
    continues it. History is filtered to non-thinking `text` items before
    comparing (module docstring)."""
    harness.script_text_response("first reply")
    session_id = str(uuid.uuid4())
    session = await harness.tradewind.create(session_id, SessionOptions())
    await session.run("first prompt")

    harness.script_text_response("second reply")
    resumed = await harness.tradewind.resume(session_id, SessionOptions())
    result = await resumed.run("second prompt")

    assert result.final_text == "second reply"
    history = _non_thinking(await harness.tradewind.history(session_id))
    texts = [m.content.get("text") for m in history if m.kind == "text"]
    assert texts == ["first prompt", "first reply", "second prompt", "second reply"]


async def history_flat_and_tree(harness: ConformanceHarness) -> None:
    """`history(include_children=False)` is one session's own log;
    `include_children=True` also includes a spawned child's (ARCHITECTURE
    §4.2, I-1). History is filtered to non-thinking `text` items before
    comparing (module docstring)."""
    harness.script_text_response("parent reply")
    parent_id = str(uuid.uuid4())
    parent = await harness.tradewind.create(parent_id, SessionOptions())
    await parent.run("parent prompt")

    harness.script_text_response("child reply")
    child = await parent.spawn("child prompt")

    flat = _non_thinking(await harness.tradewind.history(parent_id))
    flat_texts = [m.content.get("text") for m in flat if m.kind == "text"]
    assert flat_texts == ["parent prompt", "parent reply"]

    tree = _non_thinking(await harness.tradewind.history(parent_id, include_children=True))
    tree_texts = {(m.session_id, m.content.get("text")) for m in tree if m.kind == "text"}
    assert (parent_id, "parent prompt") in tree_texts
    assert (parent_id, "parent reply") in tree_texts
    assert (child.id, "child prompt") in tree_texts
    assert (child.id, "child reply") in tree_texts


async def structured_output(harness: ConformanceHarness) -> None:
    """A turn given `output_schema` completes with a final response shaped
    by it (FR-8, deferred per-backend via `Capabilities.
    supports_structured_output` -- `langchain`/`claude` skip this today, so
    `CodexBackend` (task-14) is the first to actually exercise it live).

    `additionalProperties: false` is required in the schema (module
    docstring's own category of fix, same as `tool_allow_deny`'s): a bare
    `{"type": "object", "properties": {...}}` -- valid JSON Schema in
    general -- is REJECTED live by Codex's strict structured-output mode
    (`invalid_json_schema: 'additionalProperties' is required to be
    supplied and to be false`, confirmed against a real `codex app-server`,
    task-14 report). Making the scripted schema fully specified rather than
    Codex-specific keeps this scenario backend-agnostic.
    """
    if not harness.capabilities.supports_structured_output:
        pytest.skip("backend does not support structured output")
    harness.script_text_response('{"answer": 42}')
    session_id = str(uuid.uuid4())
    session = await harness.tradewind.create(
        session_id,
        SessionOptions(
            output_schema={
                "type": "object",
                "properties": {"answer": {"type": "integer"}},
                "required": ["answer"],
                "additionalProperties": False,
            }
        ),
    )

    result = await session.run("what is the answer?")

    assert result.status == "completed"
    assert result.final_text is not None


_ECHO_TOKEN = "ZANZIBAR"


async def system_prompt_respected(harness: ConformanceHarness) -> None:
    """`SessionOptions.system_prompt` actually reaches the backend's
    request (FR-8) -- a behavioral check, not an exact-echo assertion
    (module docstring): a real model given a full system-prompt override
    "reply with exactly this text" still won't literally echo it verbatim
    once wrapped in a preset+append shape (`ClaudeBackend`'s own module
    docstring), but reliably weaves in a fixed instructed token, which both
    a scripted fake and a real model satisfy."""
    if not harness.capabilities.supports_system_prompt:
        pytest.skip("backend does not support a system prompt")
    harness.script_echo_system_prompt()
    session_id = str(uuid.uuid4())
    session = await harness.tradewind.create(
        session_id,
        SessionOptions(
            system_prompt=f"Always include the exact token {_ECHO_TOKEN} somewhere in every reply."
        ),
    )

    result = await session.run("hi")

    assert result.final_text is not None
    assert _ECHO_TOKEN in result.final_text


SCENARIOS = (
    single_turn_text,
    tool_allow_deny,
    interrupt_midturn,
    resume_continues_context,
    history_flat_and_tree,
    structured_output,
    system_prompt_respected,
)

__all__ = [
    "SCENARIOS",
    "ConformanceHarness",
    "ScriptedToolCall",
    "history_flat_and_tree",
    "interrupt_midturn",
    "resume_continues_context",
    "single_turn_text",
    "structured_output",
    "system_prompt_respected",
    "tool_allow_deny",
]
