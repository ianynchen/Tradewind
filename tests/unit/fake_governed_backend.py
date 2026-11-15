"""A scripted backend that uses every turn-governance seam the way the
Antigravity adapter will (docs/components/04-antigravity-adapter.md "Turn
algorithm"; 05-turn-governance §2-§4 and §6), with no engine behind it.

It declares `uses_result_tool=True`, floor and default rules, and two
preflight checks: `customizations` (an `Ask` with the safe default
`stop`, only when `customizations` files are given) and `login` (a `Stop`,
only when `logged_in=False`). `run()` follows the turn algorithm: apply
the preflight answer before `TurnStarted`, serve the shim, emit
`TurnStarted` and any preflight notice, play the script (gate, observe,
work signals, result-tool calls over the shim socket), report the
engine done, wait for open work to settle, then emit the process report
(if work was closed after done) and the terminal event.

Its `name` is `"codex"` only so the runner gives `ToolHost` the gate
broker, as it will for Antigravity (04 "Interface changes").
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import anyio
from anyio.streams.buffered import BufferedByteReceiveStream

from tradewind.application.config import NativeStoreConfig
from tradewind.application.ports import Backend, TurnContext
from tradewind.application.preflight import Ask, Ok, Stop, run_checks
from tradewind.application.process_report import report_event
from tradewind.application.tool_host import RESULT_TOOL_NAME
from tradewind.domain.errors import ConfigError, Unsupported
from tradewind.domain.events import (
    Event,
    ItemCompleted,
    PermissionRequested,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from tradewind.domain.models import (
    BackendName,
    Capabilities,
    NormalizedMessage,
    Profile,
    SessionRow,
    TurnResult,
)
from tradewind.domain.policy import Action, ActionKind, Matcher, Rule
from tradewind.domain.supervision import WorkKind
from tradewind.toolproxy import protocol


def action(kind: ActionKind, **fields: object) -> Action:
    """An action as the adapter would map it from an ACP update."""
    return Action(
        kind=kind,
        fields=dict(fields),
        backend="fake",
        native_name=kind,
        native_args=dict(fields),
        summary=f"{kind} {fields}",
    )


@dataclass(frozen=True)
class Say:
    """An `agent_message_chunk`: model activity and visible text."""

    text: str


@dataclass(frozen=True)
class Request:
    """A `session/request_permission`: decided at the gate."""

    action: Action


@dataclass(frozen=True)
class Observed:
    """A `tool_call` for a tool that never asks: opens a work item, then
    observe decides it."""

    id: str
    action: Action
    kind: WorkKind = "tool"
    has_end_signal: bool = True


@dataclass(frozen=True)
class Active:
    """A `tool_call_update` with content, or a subagent transcript write."""

    id: str


@dataclass(frozen=True)
class Ended:
    """A `tool_call_update` with status `completed` or `failed`."""

    id: str


@dataclass(frozen=True)
class SubmitResult:
    """The model calls the result tool through the shim."""

    arguments: dict[str, object]


Step = Say | Request | Observed | Active | Ended | SubmitResult


class FakeGovernedBackend(Backend):
    """Plays `script` through the governance seams; records what it saw.

    Recorded: `calls` (`prepare_turn`, `run`), `contexts`, `decisions`
    (`(kind, outcome)` per gated or observed action), `result_replies`
    (the shim's answer to each result-tool call) and `deleted` (files a
    `delete` preflight answer removed).
    """

    name: ClassVar[BackendName] = "codex"
    uses_result_tool: ClassVar[bool] = True

    def __init__(
        self,
        profile: Profile,
        native_config: NativeStoreConfig,
        script: list[Step],
        *,
        customizations: list[str] | None = None,
        logged_in: bool = True,
    ) -> None:
        super().__init__(profile, native_config)
        self._script = script
        self._customizations = customizations or []
        self._logged_in = logged_in
        self._interrupts: dict[str, asyncio.Event] = {}
        self.calls: list[str] = []
        self.contexts: list[TurnContext] = []
        self.decisions: list[tuple[str, str]] = []
        self.result_replies: list[str] = []
        self.deleted: list[str] = []

    def capabilities(self) -> Capabilities:
        return Capabilities(
            supports_system_prompt=True,
            supports_structured_output=True,
            supports_interactive_permissions=True,
            supports_in_process_tools=False,
            # Antigravity resumes natively; the fake keeps no native store.
            supports_native_resume=False,
            supports_fork=False,
            supports_transcript_read=False,
            supports_tool_round_cap=False,
            supports_turn_retry=False,
            supports_deny_reason=False,
            supports_engine_auto=True,
            supports_work_supervisor=True,
            supports_action_policy=True,
        )

    def floor_rules(self) -> list[Rule]:
        return [
            Rule(
                id="fake.no-escape",
                kind="shell",
                where={"sandboxed": Matcher("is_false")},
                outcome="deny",
                record=True,
                modes=frozenset({"broker"}),
                reason="commands must stay in the sandbox",
            ),
            Rule(
                id="fake.no-browser",
                kind="subagent",
                where={"type": Matcher("equals", "browser")},
                outcome="stop",
                record=True,
                reason="browser subagents are not supported",
            ),
        ]

    def default_rules(self) -> list[Rule]:
        return [
            Rule(id="fake.search", kind="web_search", where={}, outcome="allow", record=True),
            Rule(
                id="fake.read-workspace",
                kind="file_read",
                where={"in_workspace": Matcher("is_true")},
                outcome="allow",
            ),
        ]

    async def _check_customizations(self, _ctx: TurnContext) -> Ok | Ask | Stop:
        if not self._customizations:
            return Ok()
        return Ask(
            question="Workspace customizations found",
            details={"files": list(self._customizations)},
            choices=["stop", "delete", "proceed"],
            default_choice="stop",
        )

    async def _check_login(self, _ctx: TurnContext) -> Ok | Ask | Stop:
        return Ok() if self._logged_in else Stop("fake engine is not logged in")

    async def prepare_turn(self, ctx: TurnContext) -> None:
        self.calls.append("prepare_turn")
        self.contexts.append(ctx)
        # In flight from the start of `prepare_turn` (05 §4).
        self._interrupts[ctx.session.session_id] = asyncio.Event()
        await run_checks(
            ctx,
            [("customizations", self._check_customizations), ("login", self._check_login)],
        )

    async def run(self, ctx: TurnContext) -> AsyncIterator[Event]:
        self.calls.append("run")
        # Step 2: apply the preflight answer, then start the shim.
        answer = ctx.preflight.get("customizations")
        if answer == "stop":
            raise ConfigError(f"workspace customizations found: {self._customizations}")
        if answer == "delete":
            self.deleted = list(self._customizations)
        socket_path = await ctx.tools.serve_socket() if ctx.output_schema is not None else None
        # Step 3.
        yield TurnStarted(turn_id=ctx.turn_id)
        if answer == "delete":
            yield ItemCompleted(
                message=NormalizedMessage(
                    role="assistant",
                    kind="event",
                    content={"type": "preflight_applied", "deleted": self.deleted},
                )
            )
        # Step 4.
        policy, work = ctx.policy, ctx.work
        assert policy is not None and work is not None
        final_text: list[str] = []
        for step in self._script:
            if isinstance(step, Say):
                work.model_active()
                final_text.append(step.text)
                yield ItemCompleted(
                    message=NormalizedMessage(
                        role="assistant", kind="text", content={"text": step.text}
                    )
                )
            elif isinstance(step, Request):
                decision = await policy.gate(
                    step.action,
                    broker_name=step.action.native_name,
                    broker_args=step.action.native_args,
                )
                self.decisions.append((step.action.kind, decision.outcome))
                if decision.outcome == "stop":
                    yield TurnFailed(turn_id=ctx.turn_id, error=decision.reason or "stopped")
                    return
                if decision.outcome == "deny":
                    yield PermissionRequested(
                        tool_name=step.action.native_name,
                        tool_input=step.action.native_args,
                        verdict="deny",
                        reason=decision.reason,
                    )
            elif isinstance(step, Observed):
                work.started(step.id, step.kind, step.action.summary, step.has_end_signal)
                outcome = policy.observe(step.action)
                self.decisions.append((step.action.kind, outcome))
                if outcome == "stop":
                    yield TurnFailed(turn_id=ctx.turn_id, error=f"stopped: {step.action.summary}")
                    return
            elif isinstance(step, Active):
                work.active(step.id)
            elif isinstance(step, Ended):
                work.ended(step.id)
            else:
                assert socket_path is not None
                self.result_replies.append(await _shim_call(socket_path, step.arguments))
        # Steps 5-6: the prompt result, then wait for open work (05 §3.3).
        work.engine_done()
        if not await self._settled(ctx):
            return  # the runner records the limit or the interrupt
        # Step 7.
        closed = work.closed_after_done()
        if closed:
            yield report_event(
                turn_id=ctx.turn_id,
                session_id=ctx.session.session_id,
                backend=self.name,
                timeout_reason=None,
                open_items=closed,
                alive=[],
                last_action=None,
            )
        yield TurnCompleted(
            result=TurnResult(
                turn_id=ctx.turn_id,
                status="completed",
                end_reason="end_turn",
                final_text="".join(final_text) or None,
                usage={},
                cost_usd=None,
                structured_output=ctx.tools.result_slot.last_valid,
            )
        )

    async def _settled(self, ctx: TurnContext) -> bool:
        """`ctx.work.settled()`, or False when `interrupt()` comes first."""
        assert ctx.work is not None
        settled = asyncio.ensure_future(ctx.work.settled())
        interrupted = asyncio.ensure_future(self._interrupts[ctx.session.session_id].wait())
        try:
            await asyncio.wait([settled, interrupted], return_when=asyncio.FIRST_COMPLETED)
            return settled.done() and not interrupted.done() and settled.result()
        finally:
            settled.cancel()
            interrupted.cancel()

    async def probe_native(self, session: SessionRow) -> bool:  # noqa: ARG002
        return False

    async def read_native_transcript(
        self,
        session: SessionRow,  # noqa: ARG002
        after_native_id: str | None,  # noqa: ARG002
    ) -> list[NormalizedMessage]:
        raise Unsupported("fake backend has no native transcript")

    async def interrupt(self, session_id: str) -> None:
        event = self._interrupts.get(session_id)
        if event is not None:
            event.set()


async def _shim_call(socket_path: Path, arguments: dict[str, object]) -> str:
    """One result-tool call over the shim's unix socket, as `agy` makes it."""
    stream = await anyio.connect_unix(socket_path)
    async with stream:
        request = protocol.CallRequest(name=RESULT_TOOL_NAME, arguments=arguments)
        await stream.send((protocol.encode_request(request) + "\n").encode("utf-8"))
        buffered = BufferedByteReceiveStream(stream)
        line = await buffered.receive_until(b"\n", protocol.MAX_LINE_BYTES)
    response = protocol.decode_response(line.decode("utf-8"))
    assert isinstance(response, protocol.CallResponse)
    return response.content
