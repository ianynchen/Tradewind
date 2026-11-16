"""Turn governance end to end through the real runner, on a fake backend
that uses every seam the way the Antigravity adapter will
(`fake_governed_backend.py`; 05-turn-governance Acceptance, PR 1)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from fake_governed_backend import (
    Active,
    Ended,
    FakeGovernedBackend,
    Observed,
    Request,
    Say,
    Step,
    SubmitResult,
    action,
)
from pydantic import SecretStr

from tradewind import Tradewind
from tradewind.application import process_report, turn_runner
from tradewind.application.config import NativeStoreConfig, TradewindConfig
from tradewind.application.process_report import ProcInfo
from tradewind.domain.errors import ConfigError, Unsupported
from tradewind.domain.events import (
    Event,
    ItemCompleted,
    PermissionRequested,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from tradewind.domain.models import ApiKeyAuth, ModelSpec, Profile, SessionOptions
from tradewind.domain.policy import Matcher, Rule

_ID = "44444444-4444-4444-4444-444444444444"
_OTHER_ID = "55555555-5555-5555-5555-555555555555"
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


def _profile() -> Profile:
    return Profile(
        backend="codex",
        auth=ApiKeyAuth(api_key=SecretStr("sk-test")),
        models={"standard": ModelSpec(model="model-standard")},
    )


def _tradewind(
    tmp_path: Path, script: list[Step], **fake_kwargs: Any
) -> tuple[Tradewind, FakeGovernedBackend]:
    profile = _profile()
    tw = Tradewind(
        TradewindConfig(
            profiles={"default": profile}, default_profile="default", store=tmp_path / "s.db"
        )
    )
    fake = FakeGovernedBackend(profile, NativeStoreConfig(), script, **fake_kwargs)
    tw._backends["default"] = fake
    return tw, fake


class _CountingBroker:
    def __init__(self, verdict: str = "allow") -> None:
        self.verdict = verdict
        self.calls: list[str] = []

    async def decide(self, tool_name: str, _tool_input: dict[str, object]) -> str:
        self.calls.append(tool_name)
        return self.verdict


def _notices(events: list[Event], notice_type: str) -> list[dict[str, Any]]:
    return [
        e.message.content
        for e in events
        if isinstance(e, ItemCompleted) and e.message.content.get("type") == notice_type
    ]


# --- action policy -----------------------------------------------------


async def test_floor_deny_cannot_be_weakened_by_a_caller_rule(tmp_path: Path) -> None:
    escape = action("shell", command="curl example.com", sandboxed=False)
    tw, fake = _tradewind(tmp_path, [Request(escape), Say("done")])
    broker = _CountingBroker()
    loosen = Rule(id="caller.allow-shell", kind="shell", where={}, outcome="allow")
    session = await tw.create(_ID, SessionOptions(permission_broker=broker, policy_rules=[loosen]))

    events = [e async for e in session.stream("go")]

    assert fake.decisions == [("shell", "deny")]
    assert broker.calls == []
    denial = next(e for e in events if isinstance(e, PermissionRequested))
    assert denial.verdict == "deny"
    assert denial.reason == "commands must stay in the sandbox"
    [notice] = _notices(events, "policy_recorded")
    assert "fake.no-escape" in notice["rule_ids"]
    assert isinstance(events[-1], TurnCompleted)
    assert events[-1].result.status == "completed"


async def test_a_caller_rule_tightens_a_default(tmp_path: Path) -> None:
    read = action("file_read", path="a.txt", in_workspace=True)
    tw, fake = _tradewind(tmp_path, [Request(read)])
    broker = _CountingBroker()
    plain = await tw.create(_ID, SessionOptions(permission_broker=broker))
    tighten = Rule(id="caller.no-read", kind="file_read", where={}, outcome="deny")
    tightened = await tw.create(
        _OTHER_ID, SessionOptions(permission_broker=broker, policy_rules=[tighten])
    )

    await plain.run("go")
    await tightened.run("go")

    # The default allows a workspace read without asking; the caller's
    # rule turns it into a denial.
    assert fake.decisions == [("file_read", "allow"), ("file_read", "deny")]
    assert broker.calls == []


@pytest.mark.parametrize(
    ("caller_rules", "observed"),
    [
        # A caller `deny` on a default-allowed action that never asks is
        # applied as `stop` (05 §2.1).
        (
            [Rule(id="caller.no-search", kind="web_search", where={}, outcome="deny")],
            action("web_search", query="news"),
        ),
        # A floor `stop` at observe.
        ([], action("subagent", type="browser", task="browse")),
    ],
)
async def test_observe_stop_ends_the_turn_as_failed(
    tmp_path: Path, caller_rules: list[Rule], observed: Any
) -> None:
    tw, fake = _tradewind(tmp_path, [Observed("c1", observed), Say("never said")])
    session = await tw.create(_ID, SessionOptions(policy_rules=caller_rules))

    events = [e async for e in session.stream("go")]

    assert fake.decisions == [(observed.kind, "stop")]
    assert isinstance(events[-1], TurnFailed)
    assert not any(
        isinstance(e, ItemCompleted) and e.message.content.get("text") == "never said"
        for e in events
    )


async def test_engine_auto_without_a_broker_allows_without_asking(tmp_path: Path) -> None:
    tw, fake = _tradewind(tmp_path, [Request(action("shell", command="ls", sandboxed=True))])
    session = await tw.create(_ID, SessionOptions(permission_mode="engine_auto"))

    result = await session.run("go")

    assert result.status == "completed"
    assert fake.decisions == [("shell", "allow")]


# --- work supervisor -----------------------------------------------------


async def test_unsignalled_subagent_closes_as_a_normal_end_and_keeps_the_result(
    tmp_path: Path,
) -> None:
    script: list[Step] = [
        Say("delegating"),
        Observed(
            "sa1",
            action("subagent", type="general", task="t"),
            kind="subagent",
            has_end_signal=False,
        ),
        Active("sa1"),
        SubmitResult({"answer": "42"}),
    ]
    tw, _fake = _tradewind(tmp_path, script)
    session = await tw.create(_ID, SessionOptions(output_schema=_SCHEMA))

    events = [e async for e in session.stream("go", work_silence_timeout_s=0.2)]

    assert _notices(events, "turn_timeout") == []
    terminal = events[-1]
    assert isinstance(terminal, TurnCompleted)
    assert terminal.result.status == "completed"
    assert terminal.result.end_reason == "end_turn"
    assert terminal.result.structured_output == {"answer": "42"}
    # The process report names the closed item and comes before the terminal.
    assert _notices([events[-2]], "process_report") != []
    [report] = _notices(events, "process_report")
    assert report["open_items"] == [
        {"kind": "subagent", "label": "subagent {'type': 'general', 'task': 't'}"}
    ]


async def test_open_work_with_an_end_signal_ends_the_turn_as_a_timeout(tmp_path: Path) -> None:
    script: list[Step] = [
        Observed("bg1", action("native", name="build"), kind="background"),
        Observed(
            "sa1",
            action("subagent", type="general", task="t"),
            kind="subagent",
            has_end_signal=False,
        ),
        SubmitResult({"answer": "42"}),
    ]
    tw, _fake = _tradewind(tmp_path, script)
    session = await tw.create(_ID, SessionOptions(output_schema=_SCHEMA))

    events = [e async for e in session.stream("go", work_silence_timeout_s=0.2)]

    [timeout] = _notices(events, "turn_timeout")
    assert timeout["limit"] == "work_silence"
    terminal = events[-1]
    assert isinstance(terminal, TurnCompleted)
    assert terminal.result.status == "interrupted"
    assert terminal.result.end_reason == "timeout"
    assert terminal.result.structured_output is None


async def test_ended_work_lets_the_turn_complete(tmp_path: Path) -> None:
    script: list[Step] = [
        Observed("t1", action("native", name="list")),
        Active("t1"),
        Ended("t1"),
        Say("listed"),
    ]
    tw, _fake = _tradewind(tmp_path, script)
    session = await tw.create(_ID, SessionOptions())

    events = [e async for e in session.stream("go", work_silence_timeout_s=5)]

    assert isinstance(events[-1], TurnCompleted)
    assert events[-1].result.final_text == "listed"
    assert _notices(events, "process_report") == []


# --- preflight -----------------------------------------------------------


async def test_preflight_ask_stores_the_callers_answer_and_run_applies_it(
    tmp_path: Path,
) -> None:
    asked: list[tuple[str, dict[str, object], list[str]]] = []

    async def on_preflight(question: str, details: dict[str, object], choices: list[str]) -> str:
        asked.append((question, details, choices))
        return "delete"

    tw, fake = _tradewind(tmp_path, [Say("ok")], customizations=[".agents/hooks.json"])
    session = await tw.create(_ID, SessionOptions(on_preflight=on_preflight))

    events = [e async for e in session.stream("go")]

    assert asked == [
        (
            "Workspace customizations found",
            {"files": [".agents/hooks.json"]},
            ["stop", "delete", "proceed"],
        )
    ]
    assert fake.contexts[0].preflight == {"customizations": "delete"}
    assert fake.deleted == [".agents/hooks.json"]
    # The notice about the change comes right after `TurnStarted`.
    assert isinstance(events[0], TurnStarted)
    assert _notices([events[1]], "preflight_applied") != []
    assert isinstance(events[-1], TurnCompleted)


async def test_preflight_ask_without_a_callback_uses_the_safe_default(tmp_path: Path) -> None:
    tw, fake = _tradewind(tmp_path, [Say("ok")], customizations=[".agents/hooks.json"])
    session = await tw.create(_ID, SessionOptions())

    events: list[Event] = []
    with pytest.raises(ConfigError, match="customizations"):
        async for event in session.stream("go"):
            events.append(event)

    assert fake.contexts[0].preflight == {"customizations": "stop"}
    assert fake.deleted == []
    assert not any(isinstance(e, TurnStarted) for e in events)


async def test_preflight_stop_raises_config_error_and_run_is_not_called(tmp_path: Path) -> None:
    tw, fake = _tradewind(tmp_path, [Say("ok")], logged_in=False)
    session = await tw.create(_ID, SessionOptions())

    with pytest.raises(ConfigError, match="not logged in"):
        await session.run("go")

    assert fake.calls == ["prepare_turn"]


# --- result tool through the shim -----------------------------------------


async def test_result_tool_via_the_shim_records_the_last_valid_result(tmp_path: Path) -> None:
    script: list[Step] = [
        SubmitResult({"answer": 1}),
        SubmitResult({"answer": "first"}),
        SubmitResult({"answer": "second"}),
        SubmitResult({"answer": "fourth"}),
    ]
    tw, fake = _tradewind(tmp_path, script)
    broker = _CountingBroker(verdict="deny")
    session = await tw.create(_ID, SessionOptions(output_schema=_SCHEMA, permission_broker=broker))

    result = await session.run("go")

    assert fake.result_replies[1:] == ["Result recorded.", "Result recorded.", "No more attempts."]
    # The invalid call is answered with the schema error itself.
    assert fake.result_replies[0] == "1 is not of type 'string'"
    assert result.structured_output == {"answer": "second"}
    # A structural pass-through: the broker is never asked.
    assert broker.calls == []
    conn = cast(Any, tw._store)._conn
    row = await anyio.to_thread.run_sync(
        lambda: conn.execute(
            "SELECT structured_output_json FROM turns WHERE session_id = ?", (_ID,)
        ).fetchone()
    )
    assert json.loads(row[0]) == {"answer": "second"}


# --- the stored snapshot is the source of truth; one capability check ----


async def test_a_bare_resume_keeps_the_stored_caller_rules(tmp_path: Path) -> None:
    read = action("file_read", path="a.txt", in_workspace=True)
    tw, fake = _tradewind(tmp_path, [Request(read)])
    deny = Rule(id="caller.no-read", kind="file_read", where={}, outcome="deny")
    await tw.create(_ID, SessionOptions(policy_rules=[deny]))

    resumed = await tw.resume(_ID)
    await resumed.run("go")

    assert fake.decisions == [("file_read", "deny")]


async def test_a_bare_resume_keeps_the_stored_permission_mode(tmp_path: Path) -> None:
    shell = action("shell", command="ls", sandboxed=True)
    tw, fake = _tradewind(tmp_path, [Request(shell)])
    # Matches only in engine_auto mode, so the deny proves the stored mode
    # reached the gate.
    auto_only = Rule(
        id="caller.auto-no-shell",
        kind="shell",
        where={},
        outcome="deny",
        modes=frozenset({"engine_auto"}),
    )
    await tw.create(_ID, SessionOptions(permission_mode="engine_auto", policy_rules=[auto_only]))

    resumed = await tw.resume(_ID)
    await resumed.run("go")

    assert fake.decisions == [("shell", "deny")]


@pytest.mark.parametrize(
    ("flags", "options", "match"),
    [
        (
            {"engine_auto": False},
            SessionOptions(permission_mode="engine_auto"),
            "engine_auto",
        ),
        (
            {"action_policy": False},
            SessionOptions(permission_mode="broker"),
            "permission_mode",
        ),
        (
            {"action_policy": False},
            SessionOptions(policy_rules=[Rule(id="r", kind="shell", where={}, outcome="deny")]),
            "policy_rules",
        ),
    ],
)
async def test_the_runner_refuses_governance_the_backend_cannot_honor(
    tmp_path: Path, flags: dict[str, bool], options: SessionOptions, match: str
) -> None:
    tw, fake = _tradewind(tmp_path, [Say("ok")], **flags)
    session = await tw.create(_ID, options)

    events: list[Event] = []
    with pytest.raises(Unsupported, match=match):
        async for event in session.stream("go"):
            events.append(event)

    assert events == []
    assert fake.calls == []
    conn = cast(Any, tw._store)._conn
    rows = await anyio.to_thread.run_sync(
        lambda: conn.execute("SELECT status FROM turns WHERE session_id = ?", (_ID,)).fetchall()
    )
    assert rows == [("failed",)]


async def test_governance_without_flags_off_is_not_refused(tmp_path: Path) -> None:
    tw, _fake = _tradewind(tmp_path, [Say("ok")], engine_auto=False)
    session = await tw.create(_ID, SessionOptions(permission_mode="broker"))

    result = await session.run("go")

    assert result.status == "completed"


async def test_a_bare_resume_keeps_the_stored_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    read_root = action("file_read", path=str(workspace), in_workspace=True)
    tw, fake = _tradewind(tmp_path, [Request(read_root)])
    no_root = Rule(
        id="caller.no-root-read",
        kind="file_read",
        where={"path": Matcher("equals", "$turn.workspace")},
        outcome="deny",
    )
    await tw.create(_ID, SessionOptions(cwd=workspace, policy_rules=[no_root]))

    resumed = await tw.resume(_ID)
    await resumed.run("go")

    assert fake.decisions == [("file_read", "deny")]


# --- process report (05 §3.4) -----------------------------------------------

_ENGINE, _CHILD, _GRANDCHILD, _SIBLING, _SHIM = 90001, 90002, 90003, 90004, 90005
_ENGINE_TREE = {_ENGINE, _CHILD, _GRANDCHILD, _SHIM}


class _FakeProcesses:
    """A fake process table for `process_report`: the real caller, its
    parent and a sibling in the caller's group; the engine (the caller's
    child, in the caller's group, as both SDKs spawn it), its child, a
    grandchild in its own group, and the shim. No real process is listed
    for signalling: `kill_tree` is replaced and only records."""

    def __init__(self) -> None:
        me, parent, group = os.getpid(), os.getppid(), os.getpgrp()
        rows = [
            (parent, 1, group, "zsh"),
            (me, parent, group, "pytest"),
            (_SIBLING, parent, group, "sibling-of-caller"),
            (_ENGINE, me, group, "engine --app-server"),
            (_CHILD, _ENGINE, group, "node mcp-server.js"),
            (_GRANDCHILD, _CHILD, _GRANDCHILD, "sleep 1000"),
            (_SHIM, _ENGINE, group, f"{sys.executable} -m tradewind.toolproxy"),
        ]
        self.alive = {
            pid: ProcInfo(pid, ppid, pgid, "Sun Nov 15 15:11:42 2026", cmd)
            for pid, ppid, pgid, cmd in rows
        }
        self.killed: list[set[int]] = []
        self.on_kill: Callable[[], None] | None = None

    def list(self) -> list[ProcInfo]:
        return list(self.alive.values())

    def close_engine(self, *, leaves: set[int] = frozenset({_GRANDCHILD, _SHIM})) -> None:
        """The engine exits; of its tree, only `leaves` stay alive."""
        for pid in _ENGINE_TREE - leaves:
            self.alive.pop(pid, None)

    def kill_tree(self, procs: list[ProcInfo], **_kwargs: object) -> None:
        self.killed.append({p.pid for p in procs})
        for p in procs:
            self.alive.pop(p.pid, None)
        if self.on_kill is not None:
            self.on_kill()


@pytest.fixture
def procs(monkeypatch: pytest.MonkeyPatch) -> _FakeProcesses:
    fake = _FakeProcesses()
    monkeypatch.setattr(process_report, "list_processes", fake.list)
    monkeypatch.setattr(process_report, "kill_tree", fake.kill_tree)
    monkeypatch.setattr(turn_runner, "_REPORT_SETTLE_S", 0.0)
    return fake


def _engine_tradewind(
    tmp_path: Path, script: list[Step], procs: _FakeProcesses, **fake_kwargs: Any
) -> tuple[Tradewind, FakeGovernedBackend]:
    return _tradewind(
        tmp_path, script, engine_pid=_ENGINE, on_engine_closed=procs.close_engine, **fake_kwargs
    )


def _pids(report: dict[str, Any]) -> list[int]:
    return [p["pid"] for p in report["processes"]]


async def test_normal_end_reports_a_live_descendant_and_kills_nothing(
    tmp_path: Path, procs: _FakeProcesses, caplog: pytest.LogCaptureFixture
) -> None:
    tw, _fake = _engine_tradewind(tmp_path, [Say("done")], procs)
    session = await tw.create(_ID, SessionOptions())

    with caplog.at_level(logging.WARNING, logger="tradewind"):
        events = [e async for e in session.stream("go")]

    assert procs.killed == []
    assert isinstance(events[-1], TurnCompleted)
    assert events[-1].result.status == "completed"
    # One report, just before the terminal: the grandchild outlived the
    # engine; the shim is excluded; the caller and its sibling never appear.
    [report] = _notices([events[-2]], "process_report")
    assert _pids(report) == [_GRANDCHILD]
    assert report["limit"] is None
    assert len(_notices(events, "process_report")) == 1
    assert sum("process report" in r.getMessage() for r in caplog.records) == 1
    # Stored in the mirror like any other event item.
    history = await tw.history(_ID)
    assert any(m.content.get("type") == "process_report" for m in history)


async def test_plain_turn_without_a_live_descendant_emits_the_same_events(
    tmp_path: Path, procs: _FakeProcesses
) -> None:
    (tmp_path / "plain").mkdir()
    (tmp_path / "tracked").mkdir()
    plain_tw, _ = _tradewind(tmp_path / "plain", [Say("done")])
    tracked_tw, _ = _tradewind(
        tmp_path / "tracked",
        [Say("done")],
        engine_pid=_ENGINE,
        on_engine_closed=lambda: procs.close_engine(leaves={_SHIM}),
    )
    plain = await plain_tw.create(_ID, SessionOptions())
    tracked = await tracked_tw.create(_ID, SessionOptions())

    plain_events = [e async for e in plain.stream("go")]
    tracked_events = [e async for e in tracked.stream("go")]

    assert [type(e) for e in tracked_events] == [type(e) for e in plain_events]
    assert _notices(tracked_events, "process_report") == []
    assert procs.killed == []


async def test_fired_limit_kills_the_engine_tree_and_reports_before_the_terminal(
    tmp_path: Path, procs: _FakeProcesses
) -> None:
    build = action("native", name="build")
    tw, _fake = _engine_tradewind(tmp_path, [Observed("bg1", build, kind="background")], procs)
    session = await tw.create(_ID, SessionOptions())

    events = [e async for e in session.stream("go", work_silence_timeout_s=0.2)]

    # The recorded tree (engine and descendants, shim included) is ended;
    # never the caller, its parent or its sibling in the same group.
    assert procs.killed == [_ENGINE_TREE]
    terminal = events[-1]
    assert isinstance(terminal, TurnCompleted)
    assert (terminal.result.status, terminal.result.end_reason) == ("interrupted", "timeout")
    [report] = _notices([events[-2]], "process_report")
    assert report["limit"]["limit"] == "work_silence"
    assert report["open_items"] == [{"kind": "background", "label": build.summary}]
    assert report["last_action"] == build.summary
    assert _pids(report) == []  # no survivors
    types = [e.message.content.get("type") for e in events if isinstance(e, ItemCompleted)]
    assert types.index("turn_timeout") < types.index("process_report")


async def test_answered_interrupt_reports_without_killing(
    tmp_path: Path, procs: _FakeProcesses
) -> None:
    tw, _fake = _engine_tradewind(
        tmp_path, [Observed("bg1", action("native", name="build"), kind="background")], procs
    )
    session = await tw.create(_ID, SessionOptions())

    events: list[Event] = []
    async for event in session.stream("go"):
        events.append(event)
        if isinstance(event, TurnStarted):
            await session.stop()

    assert procs.killed == []
    terminal = events[-1]
    assert isinstance(terminal, TurnCompleted)
    assert terminal.result.status == "interrupted"
    # The report is always made on an interrupt.
    [report] = _notices([events[-2]], "process_report")
    assert report["limit"] is None
    assert _pids(report) == [_GRANDCHILD]


async def test_unanswered_interrupt_kills_the_engine_tree_after_the_grace(
    tmp_path: Path, procs: _FakeProcesses, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(turn_runner, "_INTERRUPT_GRACE_S", 0.05)
    tw, fake = _engine_tradewind(
        tmp_path,
        [Observed("bg1", action("native", name="build"), kind="background")],
        procs,
        answers_interrupt=False,
    )
    loop = asyncio.get_running_loop()
    # The engine's death ends its stream.
    procs.on_kill = lambda: loop.call_soon_threadsafe(fake.end_engine, _ID)
    session = await tw.create(_ID, SessionOptions())

    events: list[Event] = []
    async for event in session.stream("go"):
        events.append(event)
        if isinstance(event, TurnStarted):
            await session.stop()

    assert procs.killed and procs.killed[0] == _ENGINE_TREE
    terminal = events[-1]
    assert isinstance(terminal, TurnCompleted)
    assert terminal.result.status == "interrupted"
    [report] = _notices([events[-2]], "process_report")
    assert _pids(report) == []
