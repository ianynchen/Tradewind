"""Smoke test for `ClaudeBackend` against a real Claude Code CLI session
(task-10 brief's Files list; omitted from the original task-10 submission
and added in fix round 1 -- see the task-10 report's "Fix round 1" section).

Unlike `tests/conformance/test_claude.py` (the shared conformance matrix,
run through the full `Tradewind`/`Session`/`TurnRunner` stack with a
scripting harness), this drives `ClaudeBackend.run()` directly with a
hand-built `TurnContext` -- the same pattern
`tests/integration/test_langchain_live.py` uses for its own live-API smoke
test -- and asserts only the bare minimum: one tiny real turn completes
with some final text. Gated behind the same
`TRADEWIND_RUN_CLAUDE_INTEGRATION` opt-in env var and `@pytest.mark.
integration` marker as the conformance file, for the same reason (real,
billed API calls; `scripts/check.sh` has no `-m "not integration"` filter,
so an ungated live test would run on every future `check.sh` invocation).
"""

from __future__ import annotations

import os
import uuid

import pytest

from tradewind.adapters.claude_backend import ClaudeBackend
from tradewind.application.config import NativeStoreConfig
from tradewind.application.ports import TurnContext
from tradewind.application.tool_host import ToolHost
from tradewind.domain.events import TurnCompleted
from tradewind.domain.models import ModelSpec, Profile, SessionRow, SubscriptionAuth, Verdict

pytestmark = pytest.mark.integration

if not os.environ.get("TRADEWIND_RUN_CLAUDE_INTEGRATION"):
    pytest.skip(
        "set TRADEWIND_RUN_CLAUDE_INTEGRATION=1 to run this against a real "
        "Claude Code CLI session (billed API calls, requires local auth) "
        "-- same gate as tests/conformance/test_claude.py",
        allow_module_level=True,
    )

# Same cheap model as tests/conformance/test_claude.py.
_MODEL = "claude-haiku-4-5"


class _AllowAllBroker:
    async def decide(self, _tool_name: str, _tool_input: dict[str, object]) -> Verdict:
        return "allow"


def _session_row() -> SessionRow:
    return SessionRow(
        session_id=str(uuid.uuid4()),
        backend="claude",
        profile="default",
        options_snapshot={},
    )


async def test_run_against_real_claude_code_session_completes_with_final_text() -> None:
    model_spec = ModelSpec(model=_MODEL)
    profile = Profile(
        backend="claude",
        auth=SubscriptionAuth(),
        models={"standard": model_spec},
    )
    backend = ClaudeBackend(profile, NativeStoreConfig())
    ctx = TurnContext(
        session=_session_row(),
        turn_id=str(uuid.uuid4()),
        prompt="Reply with exactly one word: pong",
        model_spec=model_spec,
        system_prompt=None,
        output_schema=None,
        tools=ToolHost([], [], lambda ref: ref),
        broker=_AllowAllBroker(),
        load_history=list,
    )

    events = [event async for event in backend.run(ctx)]

    assert isinstance(events[-1], TurnCompleted)
    assert events[-1].result.status == "completed"
    assert events[-1].result.final_text
