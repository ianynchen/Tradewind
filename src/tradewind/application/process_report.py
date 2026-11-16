"""Engine process listing, ending and the process report
(05-turn-governance §3.4).

Listing uses `ps` (no new dependency). A process is identified by its PID
and its start time, so a reused PID is never taken for the recorded one.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import threading
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


def descendants(root_pid: int, root_pgid: int | None, procs: list[ProcInfo]) -> list[ProcInfo]:
    """Members of the root's process group plus the parent-tree walk from
    the root; the root itself is not included.

    The group is used only when `root_pgid` is set and is not the
    caller's own group: an engine the SDK spawned without a new session
    shares the caller's group, whose other members are not the engine's.
    The caller and its ancestors are never included."""
    protected = _caller_and_ancestors(procs)
    if root_pid in protected:
        return []
    use_group = root_pgid is not None and root_pgid != os.getpgrp()
    found: dict[int, ProcInfo] = {
        p.pid: p for p in procs if use_group and p.pgid == root_pgid and p.pid != root_pid
    }
    seen = {root_pid}
    frontier = {root_pid}
    while frontier:
        children = [p for p in procs if p.ppid in frontier and p.pid not in seen]
        seen.update(p.pid for p in children)
        found.update((p.pid, p) for p in children)
        frontier = {p.pid for p in children}
    return [p for p in found.values() if p.pid not in protected]


def _caller_and_ancestors(procs: Sequence[ProcInfo]) -> set[int]:
    """This process and every ancestor of it in `procs` (its parent always,
    listed or not): never signalled, never reported."""
    parents = {p.pid: p.ppid for p in procs}
    protected = {os.getpid(), os.getppid()}
    pid = os.getppid()
    while pid in parents and parents[pid] not in protected and parents[pid] > 0:
        pid = parents[pid]
        protected.add(pid)
    return protected


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
    The caller and its ancestors are never signalled. When none is left,
    the wait and the SIGKILL are skipped."""
    live = _matching(procs, list_fn)
    if not live:
        return
    _signal_all(live, signal.SIGTERM)
    sleep(grace_s)
    _signal_all(_matching(live, list_fn), signal.SIGKILL)


def _matching(procs: Sequence[ProcInfo], list_fn: Callable[[], list[ProcInfo]]) -> list[ProcInfo]:
    now = list_fn()
    running = {(p.pid, p.start) for p in now}
    protected = _caller_and_ancestors(now)
    return [p for p in procs if (p.pid, p.start) in running and p.pid not in protected]


def _signal_all(procs: Sequence[ProcInfo], sig: signal.Signals) -> None:
    for p in procs:
        try:
            os.kill(p.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            _logger.warning("cannot send %s to pid %d: permission denied", sig.name, p.pid)


class EngineWatch:
    """One turn's engine process and the processes under it (05 §3.4).

    The adapter reports the engine's PID (`started`) and asks for a
    snapshot just before it closes the engine (`snapshot`); the runner
    asks for one before every interrupt (`interrupting`), because the
    engine's descendants re-parent to PID 1 once it exits and the tree
    can then no longer be walked. `finish` ends the watch: it kills the
    recorded tree when told to (a fired limit) or when an interrupt was
    not answered within its grace, then returns the recorded processes
    still alive, minus the excluded ones.

    `started` and `snapshot` block on `ps`: call them from a worker
    thread (the Codex adapter's own thread, or `asyncio.to_thread`).
    They are thread-safe. `interrupting` and `finish` run on the loop.
    Listing and killing go through this module's `list_processes` and
    `kill_tree`, looked up at call time.

    Args:
        exclude: processes never reported (the Tradewind shim).
    """

    def __init__(self, exclude: Callable[[ProcInfo], bool]) -> None:
        self._exclude = exclude
        self._lock = threading.Lock()
        self._engine: ProcInfo | None = None
        self._tree: dict[tuple[int, str], ProcInfo] = {}
        self._watchdog: asyncio.Task[None] | None = None
        self.unanswered = False
        self.finished = False

    @property
    def tracked(self) -> bool:
        """True once the adapter reported an engine PID that was found."""
        return self._engine is not None

    def started(self, pid: int) -> None:
        """Record the engine by PID and start time; a PID that is not
        listed, or is this process or an ancestor of it, is ignored. A
        failed listing is logged: the turn goes on without a report."""
        try:
            procs = list_processes()
        except Exception:
            _logger.exception("process report unavailable: listing processes failed")
            return
        engine = next((p for p in procs if p.pid == pid), None)
        if engine is None or pid in _caller_and_ancestors(procs):
            _logger.warning("process report unavailable: engine pid %d not usable", pid)
            return
        with self._lock:
            self._engine = engine
            self._tree = {(engine.pid, engine.start): engine}

    def snapshot(self) -> None:
        """Add the engine's current descendants to the record. Nothing is
        added once the engine is gone (its tree is no longer walkable)."""
        engine = self._engine
        if engine is None:
            return
        try:
            procs = list_processes()
        except Exception:
            _logger.exception("process snapshot failed: listing processes failed")
            return
        if not any((p.pid, p.start) == (engine.pid, engine.start) for p in procs):
            return
        # Engines spawned without a new session share the caller's group;
        # `descendants` then walks the tree only.
        pgid = engine.pgid if engine.pgid != os.getpgrp() else None
        found = descendants(engine.pid, pgid, procs)
        with self._lock:
            self._tree.update(((p.pid, p.start), p) for p in found)

    def recorded(self) -> list[ProcInfo]:
        """The engine and every descendant seen in a snapshot."""
        with self._lock:
            return list(self._tree.values())

    async def interrupting(self, grace_s: float) -> None:
        """Before an interrupt: snapshot the tree, then start the grace.
        When the turn has not ended (`finish`) within `grace_s`, the
        interrupt counts as unanswered and the tree is killed."""
        if not self.tracked:
            return
        await asyncio.to_thread(self.snapshot)
        if self._watchdog is None:
            self._watchdog = asyncio.get_running_loop().create_task(self._kill_after(grace_s))

    async def _kill_after(self, grace_s: float) -> None:
        await asyncio.sleep(grace_s)
        self.unanswered = True
        _logger.warning("interrupt not answered within %.1f s; ending the engine tree", grace_s)
        await asyncio.to_thread(self._kill)

    def _kill(self) -> None:
        kill_tree(self.recorded(), list_fn=lambda: list_processes())

    async def finish(self, *, kill: bool, settle_s: float) -> list[ProcInfo]:
        """End the watch and return the recorded processes still alive.

        Kills the tree first when `kill` is set or an interrupt went
        unanswered. Otherwise waits up to `settle_s` for the processes
        to exit on their own (a helper ends shortly after its engine)."""
        self.finished = True
        if self._watchdog is not None:
            self._watchdog.cancel()
            try:
                await self._watchdog
            except asyncio.CancelledError:
                pass
            except Exception:
                _logger.exception("ending the engine tree after an unanswered interrupt failed")
        if not self.tracked:
            return []
        if kill or self.unanswered:
            await asyncio.to_thread(self._kill)
            settle_s = 0.0
        return await asyncio.to_thread(self._alive, settle_s)

    def _alive(self, settle_s: float) -> list[ProcInfo]:
        deadline = time.monotonic() + settle_s
        while True:
            alive = still_alive(self.recorded(), list_processes(), self._exclude)
            if not alive or time.monotonic() >= deadline:
                return alive
            time.sleep(0.1)


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
