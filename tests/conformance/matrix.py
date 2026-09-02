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
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

import anyio
import pytest

from tradewind.application.client import Tradewind
from tradewind.domain.events import Event, PermissionRequested, TurnCompleted, TurnFailed
from tradewind.domain.models import Capabilities, SessionOptions, Tool, Verdict


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
    order -- the baseline every backend must support."""
    harness.script_text_response("hello there")
    session_id = str(uuid.uuid4())
    session = await harness.tradewind.create(session_id, SessionOptions())

    result = await session.run("hi")

    assert result.status == "completed"
    assert result.final_text == "hello there"
    history = await harness.tradewind.history(session_id)
    assert [(m.role, m.content.get("text")) for m in history] == [
        ("user", "hi"),
        ("assistant", "hello there"),
    ]


async def tool_allow_deny(harness: ConformanceHarness) -> None:
    """The broker gates each tool call independently: an allowed call
    executes and its result reaches the model; a denied call never
    executes and is surfaced as `PermissionRequested` (FR-4.1)."""
    if not (
        harness.capabilities.supports_interactive_permissions
        and harness.capabilities.supports_in_process_tools
    ):
        pytest.skip("backend does not support broker-gated in-process tools")

    async def handler(**kwargs: object) -> str:
        return f"ok:{kwargs}"

    allowed = Tool(
        name="allowed_tool", description="d", input_schema={"type": "object"}, handler=handler
    )
    denied = Tool(
        name="denied_tool", description="d", input_schema={"type": "object"}, handler=handler
    )

    class _Broker:
        async def decide(self, tool_name: str, _tool_input: dict[str, object]) -> Verdict:
            return "deny" if tool_name == "denied_tool" else "allow"

    harness.script_tool_calls_then_text(
        [
            ScriptedToolCall(name="allowed_tool", args={"x": 1}, id="call_a"),
            ScriptedToolCall(name="denied_tool", args={"y": 2}, id="call_b"),
        ],
        final_text="done",
    )

    session_id = str(uuid.uuid4())
    session = await harness.tradewind.create(
        session_id, SessionOptions(tools=[allowed, denied], permission_broker=_Broker())
    )

    events = [event async for event in session.stream("go")]

    permission_events = [e for e in events if isinstance(e, PermissionRequested)]
    assert permission_events == [
        PermissionRequested(tool_name="denied_tool", tool_input={"y": 2}, verdict="deny")
    ]
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
    continues it."""
    harness.script_text_response("first reply")
    session_id = str(uuid.uuid4())
    session = await harness.tradewind.create(session_id, SessionOptions())
    await session.run("first prompt")

    harness.script_text_response("second reply")
    resumed = await harness.tradewind.resume(session_id, SessionOptions())
    result = await resumed.run("second prompt")

    assert result.final_text == "second reply"
    history = await harness.tradewind.history(session_id)
    assert [m.content.get("text") for m in history] == [
        "first prompt",
        "first reply",
        "second prompt",
        "second reply",
    ]


async def history_flat_and_tree(harness: ConformanceHarness) -> None:
    """`history(include_children=False)` is one session's own log;
    `include_children=True` also includes a spawned child's (ARCHITECTURE
    §4.2, I-1)."""
    harness.script_text_response("parent reply")
    parent_id = str(uuid.uuid4())
    parent = await harness.tradewind.create(parent_id, SessionOptions())
    await parent.run("parent prompt")

    harness.script_text_response("child reply")
    child = await parent.spawn("child prompt")

    flat = await harness.tradewind.history(parent_id)
    assert [m.content.get("text") for m in flat] == ["parent prompt", "parent reply"]

    tree = await harness.tradewind.history(parent_id, include_children=True)
    tree_texts = {(m.session_id, m.content.get("text")) for m in tree}
    assert (parent_id, "parent prompt") in tree_texts
    assert (parent_id, "parent reply") in tree_texts
    assert (child.id, "child prompt") in tree_texts
    assert (child.id, "child reply") in tree_texts


async def structured_output(harness: ConformanceHarness) -> None:
    """A turn given `output_schema` completes with a final response shaped
    by it (FR-8, deferred per-backend via `Capabilities.
    supports_structured_output` -- `langchain` skips this today)."""
    if not harness.capabilities.supports_structured_output:
        pytest.skip("backend does not support structured output")
    harness.script_text_response('{"answer": 42}')
    session_id = str(uuid.uuid4())
    session = await harness.tradewind.create(
        session_id,
        SessionOptions(
            output_schema={"type": "object", "properties": {"answer": {"type": "integer"}}}
        ),
    )

    result = await session.run("what is the answer?")

    assert result.status == "completed"
    assert result.final_text is not None


async def system_prompt_respected(harness: ConformanceHarness) -> None:
    """`SessionOptions.system_prompt` actually reaches the backend's
    request (FR-8)."""
    if not harness.capabilities.supports_system_prompt:
        pytest.skip("backend does not support a system prompt")
    harness.script_echo_system_prompt()
    session_id = str(uuid.uuid4())
    session = await harness.tradewind.create(
        session_id, SessionOptions(system_prompt="be extremely terse")
    )

    result = await session.run("hi")

    assert result.final_text == "be extremely terse"


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
