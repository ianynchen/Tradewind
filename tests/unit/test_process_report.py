"""Process listing and the process report (05-turn-governance §3.4)."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time

from tradewind.application import process_report as pr
from tradewind.application.process_report import (
    ProcInfo,
    descendants,
    kill_tree,
    list_processes,
    log_report,
    report_event,
    still_alive,
)
from tradewind.domain.events import ItemCompleted
from tradewind.domain.supervision import WorkItem

PS = """\
    1     0     1 Sun Nov 15 10:00:00 2026 /sbin/launchd
  100     1   100 Sun Nov 15 15:11:42 2026 claude --print --flag
  101   100   100 Sun Nov 15 15:11:43 2026 node server.js
  102   101   102 Sun Nov 15 15:11:44 2026 sleep 1000
  103     1   100 Sun Nov 15 15:11:45 2026 stray-in-group
  200     1   200 Sun Nov 15 15:12:00 2026 unrelated
"""


def _p(pid, ppid=1, pgid=1, start="s", command="c"):
    return ProcInfo(pid, ppid, pgid, start, command)


def test_list_processes_parses_ps_output():
    procs = list_processes(lambda: PS)
    assert len(procs) == 6
    assert procs[1] == ProcInfo(100, 1, 100, "Sun Nov 15 15:11:42 2026", "claude --print --flag")


def test_list_processes_skips_malformed_lines():
    assert list_processes(lambda: "garbage\n\n  1 2\n") == []


def test_descendants_group_and_tree_walk():
    procs = list_processes(lambda: PS)
    found = {p.pid for p in descendants(100, 100, procs)}
    # 101, 103 via group; 102 outside the group via the parent tree.
    assert found == {101, 102, 103}


def test_still_alive_requires_same_pid_and_start():
    rec = [_p(5, start="a"), _p(6, start="a")]
    now = [_p(5, start="a"), _p(6, start="b")]  # pid 6 was reused
    assert [p.pid for p in still_alive(rec, now, lambda _p: False)] == [5]


def test_still_alive_exclusion_predicate():
    rec = [_p(5, command="shim"), _p(6, command="work")]
    alive = still_alive(rec, rec, lambda p: p.command == "shim")
    assert [p.pid for p in alive] == [6]


def test_kill_tree_term_then_kill_and_ignores_gone(monkeypatch):
    sent = []

    def fake_kill(pid, sig):
        sent.append((pid, sig))
        if pid == 8:
            raise ProcessLookupError

    monkeypatch.setattr(pr.os, "kill", fake_kill)
    procs = [_p(7), _p(8)]
    kill_tree(procs, grace_s=0.0, sleep=lambda _s: None, list_fn=lambda: procs)
    assert (7, signal.SIGTERM) in sent
    assert (8, signal.SIGTERM) in sent
    assert (7, signal.SIGKILL) in sent


def test_kill_tree_does_not_signal_reused_pid(monkeypatch):
    sent = []
    monkeypatch.setattr(pr.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    rec = [_p(7, start="a"), _p(8, start="a")]
    listings = iter([rec, [_p(7, start="a"), _p(8, start="NEW")]])
    kill_tree(rec, grace_s=0.0, sleep=lambda _s: None, list_fn=lambda: next(listings))
    assert (8, signal.SIGTERM) in sent
    assert (8, signal.SIGKILL) not in sent
    assert (7, signal.SIGKILL) in sent


def test_kill_tree_returns_early_when_nothing_alive(monkeypatch):
    sent = []
    slept = []
    monkeypatch.setattr(pr.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    kill_tree([_p(7, start="a")], sleep=slept.append, list_fn=lambda: [_p(7, start="b")])
    assert sent == [] and slept == []


def test_kill_tree_survives_permission_error(monkeypatch, caplog):
    sent = []

    def fake_kill(pid, sig):
        sent.append((pid, sig))
        if pid == 7:
            raise PermissionError

    monkeypatch.setattr(pr.os, "kill", fake_kill)
    procs = [_p(7), _p(8)]
    with caplog.at_level(logging.WARNING):
        kill_tree(procs, grace_s=0.0, sleep=lambda _s: None, list_fn=lambda: procs)
    assert (8, signal.SIGTERM) in sent and (8, signal.SIGKILL) in sent
    assert any("7" in r.getMessage() for r in caplog.records)


def test_kill_tree_real_child():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        info = next(p for p in list_processes() if p.pid == child.pid)
        kill_tree([info], grace_s=3.0)
        assert child.wait(timeout=5) != 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def _report_kwargs():
    return {
        "turn_id": "t1",
        "session_id": "s1",
        "backend": "claude",
        "timeout_reason": {"limit": "model_silence", "value": 300.0, "elapsed": 301.5},
        "open_items": [WorkItem(id="w1", kind="tool", label="Bash: sleep")],
        "alive": [ProcInfo(102, 101, 102, "Sun Nov 15 15:11:44 2026", "sleep 1000")],
        "last_action": "Bash: sleep 1000",
    }


def test_report_event_fields():
    ev = report_event(**_report_kwargs())
    assert isinstance(ev, ItemCompleted)
    assert ev.message.kind == "event"
    c = ev.message.content
    assert c["type"] == "process_report"
    assert c["turn_id"] == "t1" and c["session_id"] == "s1" and c["backend"] == "claude"
    assert c["limit"] == {"limit": "model_silence", "value": 300.0, "elapsed": 301.5}
    assert c["open_items"] == [{"kind": "tool", "label": "Bash: sleep"}]
    assert c["processes"] == [
        {
            "name": "sleep",
            "pid": 102,
            "command": "sleep 1000",
            "start": "Sun Nov 15 15:11:44 2026",
        }
    ]
    assert c["last_action"] == "Bash: sleep 1000"


def test_report_event_without_limit():
    kw = _report_kwargs() | {"timeout_reason": None}
    assert report_event(**kw).message.content["limit"] is None


def test_log_report_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="tradewind"):
        log_report(**_report_kwargs())
    recs = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(recs) == 1
    assert "t1" in recs[0].getMessage() and "102" in recs[0].getMessage()


# --- the caller guard (05 §3.4: engines share the caller's process group) ---

_ENGINE, _CHILD, _GRANDCHILD, _SIBLING, _SHIM = 90001, 90002, 90003, 90004, 90005


def _caller_ps() -> str:
    """A listing where the engine is the caller's child in the caller's
    group: a child in that group, a grandchild in its own group, the shim,
    and a sibling of the caller in the same group."""
    me, parent, group = os.getpid(), os.getppid(), os.getpgrp()
    when = "Sun Nov 15 15:11:42 2026"
    rows = [
        (parent, 1, group, "zsh"),
        (me, parent, group, "pytest"),
        (_SIBLING, parent, group, "sibling-of-caller"),
        (_ENGINE, me, group, "claude --print"),
        (_CHILD, _ENGINE, group, "node server.js"),
        (_GRANDCHILD, _CHILD, _GRANDCHILD, "sleep 1000"),
        (_SHIM, _ENGINE, group, f"{sys.executable} -m tradewind.toolproxy"),
    ]
    return "".join(f"{pid} {ppid} {pgid} {when} {cmd}\n" for pid, ppid, pgid, cmd in rows)


def test_descendants_skip_the_group_when_it_is_the_callers():
    procs = list_processes(_caller_ps)
    found = {p.pid for p in descendants(_ENGINE, os.getpgrp(), procs)}
    # The tree walk only: never the caller, its parent or the caller's sibling.
    assert found == {_CHILD, _GRANDCHILD, _SHIM}


def test_descendants_without_a_group_walk_the_tree_only():
    procs = list_processes(lambda: PS)
    # 103 shares the root's group but is not its descendant.
    assert {p.pid for p in descendants(100, None, procs)} == {101, 102}


def test_descendants_never_include_the_caller_or_its_ancestors():
    procs = list_processes(_caller_ps)
    # A root that is the caller's parent: the walk would reach the caller.
    found = {p.pid for p in descendants(os.getppid(), None, procs)}
    assert os.getpid() not in found and os.getppid() not in found
    assert descendants(os.getpid(), None, procs) == []


def test_kill_tree_never_signals_the_caller_or_its_ancestors(monkeypatch):
    sent = []
    monkeypatch.setattr(pr.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    procs = list_processes(_caller_ps)
    kill_tree(procs, grace_s=0.0, sleep=lambda _s: None, list_fn=lambda: procs)
    signalled = {pid for pid, _sig in sent}
    assert os.getpid() not in signalled and os.getppid() not in signalled
    assert _ENGINE in signalled


def test_real_tree_is_found_and_killed_without_touching_the_caller():
    # A `sh` this test starts, with two `sleep` children; it shares the
    # caller's group, so only the tree walk finds them.
    root = subprocess.Popen(["sh", "-c", "sleep 30 & sleep 30 & wait"])
    try:
        deadline = time.monotonic() + 5
        kids: list[ProcInfo] = []
        while time.monotonic() < deadline and len(kids) < 2:
            procs = list_processes()
            kids = descendants(root.pid, None, procs)
            time.sleep(0.05)
        assert len(kids) == 2 and all("sleep" in p.command for p in kids)
        engine = next(p for p in procs if p.pid == root.pid)
        kill_tree([engine, *kids], grace_s=0.5)
        root.wait(timeout=5)
        alive = {(p.pid, p.start) for p in list_processes()}
        assert all((p.pid, p.start) not in alive for p in kids)
    finally:
        if root.poll() is None:
            root.kill()
            root.wait()


def test_engine_watch_survives_a_failed_listing(monkeypatch, caplog):
    def broken():
        raise subprocess.CalledProcessError(1, "ps")

    monkeypatch.setattr(pr, "list_processes", broken)
    watch = pr.EngineWatch(exclude=lambda _p: False)
    with caplog.at_level(logging.WARNING):
        watch.started(_ENGINE)
        watch.snapshot()
    assert not watch.tracked
    assert any("listing processes failed" in r.getMessage() for r in caplog.records)


def test_engine_watch_ignores_the_callers_own_pid(monkeypatch):
    monkeypatch.setattr(pr, "list_processes", lambda: list_processes(_caller_ps))
    watch = pr.EngineWatch(exclude=lambda _p: False)
    watch.started(os.getpid())
    assert not watch.tracked
    watch.started(_ENGINE)
    watch.snapshot()
    assert {p.pid for p in watch.recorded()} == {_ENGINE, _CHILD, _GRANDCHILD, _SHIM}


def _watch_with_fakes(monkeypatch, listing):
    killed = []
    monkeypatch.setattr(pr, "list_processes", lambda: list_processes(listing))
    monkeypatch.setattr(pr, "kill_tree", lambda procs, **_kw: killed.append({p.pid for p in procs}))
    return pr.EngineWatch(exclude=lambda _p: False), killed


async def test_a_stop_before_the_engine_started_still_gets_the_grace(monkeypatch):
    watch, killed = _watch_with_fakes(monkeypatch, _caller_ps)
    await watch.interrupting(0.05)  # no engine yet
    await asyncio.to_thread(watch.started, _ENGINE)
    await asyncio.sleep(0.3)
    assert watch.unanswered
    assert killed and _ENGINE in killed[0]


async def test_a_pending_stop_starts_no_grace_once_the_watch_is_finished(monkeypatch):
    watch, killed = _watch_with_fakes(monkeypatch, _caller_ps)
    await watch.interrupting(0.05)
    await watch.finish(kill=False, settle_s=0.0)
    await asyncio.to_thread(watch.started, _ENGINE)
    await watch.interrupting(0.05)
    await asyncio.sleep(0.3)
    assert killed == []


def test_a_restarted_engine_keeps_the_first_engines_tree(monkeypatch):
    second = 90006

    def listing():
        me, group = os.getpid(), os.getpgrp()
        return _caller_ps() + f"{second} {me} {group} Sun Nov 15 15:11:42 2026 engine-2\n"

    watch, _killed = _watch_with_fakes(monkeypatch, listing)
    watch.started(_ENGINE)
    watch.snapshot()
    watch.started(second)
    assert {p.pid for p in watch.recorded()} == {_ENGINE, _CHILD, _GRANDCHILD, _SHIM, second}
