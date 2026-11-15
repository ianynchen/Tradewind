"""Engine process listing, ending and the process report
(05-turn-governance §3.4).

Listing uses `ps` (no new dependency). A process is identified by its PID
and its start time, so a reused PID is never taken for the recorded one.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from tradewind.domain.events import ItemCompleted
from tradewind.domain.models import process_report_notice
from tradewind.domain.supervision import WorkItem

_logger = logging.getLogger(__name__)

_PS_ARGS = ["ps", "-A", "-o", "pid=,ppid=,pgid=,lstart=,command="]
_LSTART_TOKENS = 5  # e.g. "Sun Nov 15 15:11:42 2026"


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    pgid: int
    start: str
    command: str


def _ps() -> str:
    return subprocess.run(_PS_ARGS, capture_output=True, text=True, check=True).stdout


def list_processes(run_ps: Callable[[], str] = _ps) -> list[ProcInfo]:
    """Parse `ps -A -o pid=,ppid=,pgid=,lstart=,command=`; malformed lines
    are skipped."""
    procs: list[ProcInfo] = []
    for line in run_ps().splitlines():
        parts = line.split(None, 3 + _LSTART_TOKENS)
        if len(parts) < 3 + _LSTART_TOKENS:
            continue
        try:
            pid, ppid, pgid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        start = " ".join(parts[3 : 3 + _LSTART_TOKENS])
        command = parts[3 + _LSTART_TOKENS] if len(parts) > 3 + _LSTART_TOKENS else ""
        procs.append(ProcInfo(pid, ppid, pgid, start, command))
    return procs


def descendants(root_pid: int, root_pgid: int, procs: list[ProcInfo]) -> list[ProcInfo]:
    """Members of the process group plus the parent-tree walk from the
    root; the root itself is not included."""
    found: dict[int, ProcInfo] = {
        p.pid: p for p in procs if p.pgid == root_pgid and p.pid != root_pid
    }
    seen = {root_pid}
    frontier = {root_pid}
    while frontier:
        children = [p for p in procs if p.ppid in frontier and p.pid not in seen]
        seen.update(p.pid for p in children)
        found.update((p.pid, p) for p in children)
        frontier = {p.pid for p in children}
    return list(found.values())


def still_alive(
    recorded: list[ProcInfo],
    now: list[ProcInfo],
    exclude: Callable[[ProcInfo], bool],
) -> list[ProcInfo]:
    """The recorded processes still running: same PID **and** same start
    time (a reused PID is a different process), minus the excluded ones
    (the shim and the engine's own helpers)."""
    running = {(p.pid, p.start) for p in now}
    return [p for p in recorded if (p.pid, p.start) in running and not exclude(p)]


def kill_tree(
    procs: list[ProcInfo],
    *,
    grace_s: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
    list_fn: Callable[[], list[ProcInfo]] = list_processes,
) -> None:
    """SIGTERM every process, then SIGKILL after `grace_s`. Before each
    phase the processes are listed again and only those whose PID and
    start time still match are signalled, so a reused PID is never hit.
    When none is left, the wait and the SIGKILL are skipped."""
    live = _matching(procs, list_fn)
    if not live:
        return
    _signal_all(live, signal.SIGTERM)
    sleep(grace_s)
    _signal_all(_matching(live, list_fn), signal.SIGKILL)


def _matching(procs: Sequence[ProcInfo], list_fn: Callable[[], list[ProcInfo]]) -> list[ProcInfo]:
    running = {(p.pid, p.start) for p in list_fn()}
    return [p for p in procs if (p.pid, p.start) in running]


def _signal_all(procs: Sequence[ProcInfo], sig: signal.Signals) -> None:
    for p in procs:
        try:
            os.kill(p.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            _logger.warning("cannot send %s to pid %d: permission denied", sig.name, p.pid)


def _name(command: str) -> str:
    first = command.split(None, 1)[0] if command.strip() else ""
    return os.path.basename(first)


@dataclass(frozen=True)
class _Fields:
    turn_id: str
    session_id: str
    backend: str
    limit: dict[str, float | str] | None
    open_items: list[dict[str, str]]
    processes: list[dict[str, str | int]]
    last_action: str | None


def _fields(
    *,
    turn_id: str,
    session_id: str,
    backend: str,
    timeout_reason: Mapping[str, float | str] | None,
    open_items: Sequence[WorkItem],
    alive: Sequence[ProcInfo],
    last_action: str | None,
) -> _Fields:
    return _Fields(
        turn_id=turn_id,
        session_id=session_id,
        backend=backend,
        limit=dict(timeout_reason) if timeout_reason else None,
        open_items=[{"kind": i.kind, "label": i.label} for i in open_items],
        processes=[
            {"name": _name(p.command), "pid": p.pid, "command": p.command, "start": p.start}
            for p in alive
        ],
        last_action=last_action,
    )


def report_event(
    *,
    turn_id: str,
    session_id: str,
    backend: str,
    timeout_reason: Mapping[str, float | str] | None,
    open_items: Sequence[WorkItem],
    alive: Sequence[ProcInfo],
    last_action: str | None,
) -> ItemCompleted:
    """The process report as the `ItemCompleted(kind="event")` emitted
    before the terminal event."""
    f = _fields(
        turn_id=turn_id,
        session_id=session_id,
        backend=backend,
        timeout_reason=timeout_reason,
        open_items=open_items,
        alive=alive,
        last_action=last_action,
    )
    return ItemCompleted(
        process_report_notice(
            turn_id=f.turn_id,
            session_id=f.session_id,
            backend=f.backend,
            limit=f.limit,
            open_items=f.open_items,
            processes=f.processes,
            last_action=f.last_action,
        )
    )


def log_report(
    *,
    turn_id: str,
    session_id: str,
    backend: str,
    timeout_reason: Mapping[str, float | str] | None,
    open_items: Sequence[WorkItem],
    alive: Sequence[ProcInfo],
    last_action: str | None,
) -> None:
    """One WARNING log entry carrying the same fields as the event."""
    f = _fields(
        turn_id=turn_id,
        session_id=session_id,
        backend=backend,
        timeout_reason=timeout_reason,
        open_items=open_items,
        alive=alive,
        last_action=last_action,
    )
    _logger.warning(
        "process report: turn=%s session=%s backend=%s limit=%s open_items=%s "
        "processes=%s last_action=%s",
        f.turn_id,
        f.session_id,
        f.backend,
        f.limit,
        f.open_items,
        f.processes,
        f.last_action,
    )
