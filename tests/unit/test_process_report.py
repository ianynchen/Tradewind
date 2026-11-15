"""Process listing and the process report (05-turn-governance §3.4)."""

from __future__ import annotations

import logging
import signal
import subprocess
import sys

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
    kill_tree([_p(7), _p(8)], grace_s=0.0, sleep=lambda _s: None)
    assert (7, signal.SIGTERM) in sent
    assert (8, signal.SIGTERM) in sent
    # 7 never died in the fake, so it is killed after the grace.
    assert (7, signal.SIGKILL) in sent


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
