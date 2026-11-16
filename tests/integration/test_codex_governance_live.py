"""Live check of Codex approvals through the turn-governance gate (05 §7).

Gated by `TRADEWIND_RUN_CODEX_INTEGRATION=1`, marked `integration`, never run
by `check.sh` or CI. It uses the machine's ChatGPT subscription login and a
real `codex app-server`. Run it time-boxed:

    TRADEWIND_RUN_CODEX_INTEGRATION=1 timeout 300 \
        uv run pytest tests/integration/test_codex_governance_live.py -v -m integration

What it proves (the Task 9 assumptions the unit tests cannot): a
`fileChange/requestApproval` finds its item's changes from `item/started`
(same item id, start before request), so the broker gets a `file_write`
action with the changed path -- not a `missing_critical_field` floor deny.
The sandbox is `read-only`, so every write must ask first.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from tradewind.application.client import Tradewind
from tradewind.application.config import TradewindConfig
from tradewind.domain.events import Event, PermissionRequested, TurnCompleted, TurnFailed
from tradewind.domain.models import ModelSpec, Profile, SessionOptions, SubscriptionAuth

pytestmark = pytest.mark.integration

if not os.environ.get("TRADEWIND_RUN_CODEX_INTEGRATION"):
    pytest.skip(
        "set TRADEWIND_RUN_CODEX_INTEGRATION=1 to run against a real codex app-server",
        allow_module_level=True,
    )

_MODEL = "gpt-5.6-sol"


class _RecordingBroker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def decide(self, tool_name: str, tool_input: dict[str, object]) -> str:
        self.calls.append((tool_name, tool_input))
        return "allow"


def _tradewind(tmp_path: Path) -> Tradewind:
    profile = Profile(
        backend="codex",
        auth=SubscriptionAuth(),
        models={"standard": ModelSpec(model=_MODEL)},
        backend_options={"sandbox": "read-only"},
    )
    return Tradewind(
        TradewindConfig(
            profiles={"default": profile},
            default_profile="default",
            store=tmp_path / "sessions.db",
        )
    )


def _actions(broker: _RecordingBroker) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for _name, args in broker.calls:
        shared = args.get("tradewind")
        if isinstance(shared, dict):
            found.append(shared)
    return found


def _describe(events: list[Event], broker: _RecordingBroker) -> str:
    lines = [f"broker calls: {broker.calls!r}"]
    lines += [repr(e) for e in events if isinstance(e, PermissionRequested | TurnFailed)]
    return "\n".join(lines)


async def test_a_file_change_request_reaches_the_broker_with_its_changes(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    broker = _RecordingBroker()
    session = await _tradewind(tmp_path).create(
        str(uuid.uuid4()),
        SessionOptions(cwd=workspace, permission_broker=broker),
    )

    events = [
        e
        async for e in session.stream(
            "Create a new file named hello.txt in the current directory whose whole "
            "content is the single line: hi. Use your apply_patch file-editing tool, "
            "not a shell command. Then reply with the word done."
        )
    ]

    detail = _describe(events, broker)
    assert any(isinstance(e, TurnCompleted) for e in events), detail
    writes = [a for a in _actions(broker) if a.get("kind") == "file_write"]
    assert writes, f"no file_write reached the broker\n{detail}"
    changes = writes[0]["fields"]["changes"]  # type: ignore[index]
    assert any("hello.txt" in str(c) for c in changes), detail
    denied = [e for e in events if isinstance(e, PermissionRequested) and e.verdict == "deny"]
    assert denied == [], detail
    assert (workspace / "hello.txt").read_text().strip() == "hi"


async def test_a_shell_request_reaches_the_broker_as_a_shell_action(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    broker = _RecordingBroker()
    session = await _tradewind(tmp_path).create(
        str(uuid.uuid4()),
        SessionOptions(cwd=workspace, permission_broker=broker),
    )

    events = [
        e
        async for e in session.stream(
            "Run exactly this shell command once: touch marker.txt -- then reply with "
            "the word done. Do not run any other command."
        )
    ]

    detail = _describe(events, broker)
    assert any(isinstance(e, TurnCompleted) for e in events), detail
    shells = [a for a in _actions(broker) if a.get("kind") == "shell"]
    assert shells, f"no shell action reached the broker\n{detail}"
    assert (workspace / "marker.txt").exists(), detail
