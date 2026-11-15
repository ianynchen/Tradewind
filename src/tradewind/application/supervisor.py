"""The work supervisor (05-turn-governance §3): ends a turn that stopped
making progress, replacing the FR-6.6 wall-clock watchdog.

Backends report work and model activity through `ctx.work`; the runner
starts `run()` as a task and wires the policy gate's approval count to
`approvals_changed`. The limit arithmetic itself is
`tradewind.domain.supervision.next_deadline`.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import cast

from tradewind.domain.supervision import (
    Limits,
    SupervisorState,
    WorkItem,
    WorkKind,
    next_deadline,
)

OnFire = Callable[[str, float, float], Awaitable[None]]


class WorkSupervisor:
    """Tracks one turn's open work and fires at most one limit.

    Time is measured on an *active* clock: `clock()` minus the time a
    broker question was open, so all limits pause while one is (§3.2).
    The clock starts when `run()` starts (time before it is not counted).

    Concurrency: all methods must be called from the event loop that runs
    `run()`; signals are plain method calls, never awaited.

    Args:
        limits: the turn's limits.
        reports_work: False when the backend sends no work or model
            signals (`supports_work_supervisor=False`); only the hard cap
            then applies.
        clock: monotonic seconds; injectable for tests.
        sleep: the wait used between checks; injectable for tests.
    """

    def __init__(
        self,
        limits: Limits,
        *,
        reports_work: bool = True,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.limits = limits
        self._reports_work = reports_work
        self._clock = clock
        self._sleep = sleep
        self._open: dict[str, WorkItem] = {}
        self._closed_after_done: list[WorkItem] = []
        self._engine_done = False
        self._pending = 0
        self._paused_s = 0.0
        self._paused_at: float | None = None
        self._started_at: float | None = None
        self._last_model_at = float("-inf")
        self._last_work_at = float("-inf")
        self._changed = asyncio.Event()

    def _active_now(self) -> float:
        now = self._clock()
        paused = self._paused_s + (now - self._paused_at if self._paused_at is not None else 0.0)
        return now - paused

    def _signal(self) -> None:
        self._changed.set()

    def started(
        self,
        id: str,  # noqa: A002 -- the port's signal vocabulary (05 §3.1)
        kind: WorkKind,
        label: str,
        has_end_signal: bool = True,
    ) -> None:
        """A work item opened (a tool call, a background task, a subagent)."""
        self._open[id] = WorkItem(id, kind, label, has_end_signal)
        self._last_work_at = self._active_now()
        self._signal()

    def active(self, id: str) -> None:  # noqa: A002, ARG002 -- 05 §3.1; any item's report counts
        """An open work item reported output, progress or a log write."""
        self._last_work_at = self._active_now()
        self._signal()

    def ended(self, id: str) -> None:  # noqa: A002 -- the port's signal vocabulary (05 §3.1)
        """A work item ended; an unknown id is ignored."""
        self._open.pop(id, None)
        self._last_work_at = self._active_now()
        self._signal()

    def model_active(self) -> None:
        """Text, reasoning or thinking arrived from the model."""
        self._last_model_at = self._active_now()
        self._signal()

    def engine_done(self) -> None:
        """The engine reported its turn done. The turn ends once no work
        is open; work without an end signal is closed after the
        work-silence limit counted from here (§3.3)."""
        self._engine_done = True
        self._last_work_at = self._active_now()
        self._signal()

    def approvals_changed(self, count: int) -> None:
        """The number of open broker questions changed (wired to
        `PolicyGate.on_approval_change`); a non-zero count pauses every
        limit."""
        now = self._clock()
        if count > 0 and self._paused_at is None:
            self._paused_at = now
        elif count == 0 and self._paused_at is not None:
            self._paused_s += now - self._paused_at
            self._paused_at = None
        self._pending = count
        self._signal()

    def open_items(self) -> list[WorkItem]:
        """The work items still open, in the order they started."""
        return list(self._open.values())

    def close_unsignalled_after_done(self) -> list[WorkItem]:
        """Close the open work that has no end signal, as a normal end,
        once the engine is done and no item with an end signal is open.

        Returns every item closed this way (for the process report);
        returns `[]` while the conditions do not hold. Idempotent.
        """
        if (
            self._engine_done
            and self._open
            and not any(item.has_end_signal for item in self._open.values())
        ):
            self._closed_after_done.extend(self._open.values())
            self._open.clear()
        return list(self._closed_after_done)

    def _state(self) -> SupervisorState:
        started_at = self._started_at if self._started_at is not None else self._active_now()
        return SupervisorState(
            started_at=started_at,
            last_model_at=max(self._last_model_at, started_at),
            last_work_at=max(self._last_work_at, started_at),
            has_open_work=bool(self._open),
            reports_work=self._reports_work,
            pending_approvals=self._pending,
        )

    async def _wait(self, delay: float | None) -> None:
        """Wait for a signal, or for `delay` seconds when it is set."""
        waiters: list[asyncio.Future[object]] = [asyncio.ensure_future(self._changed.wait())]
        if delay is not None:
            waiters.append(asyncio.ensure_future(self._sleep(delay)))
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
            for waiter in waiters:
                with contextlib.suppress(asyncio.CancelledError):
                    await waiter

    async def run(self, on_fire: OnFire) -> None:
        """Supervise until the turn ends normally or a limit fires.

        Returns when the engine is done and no work is open (unsignalled
        work closing after done counts), or after calling
        `on_fire(limit, value, elapsed)` once: `limit` is the limit's
        name, `value` its setting and `elapsed` the active seconds since
        `run()` started. The caller cancels the task when the turn's
        stream ends first.
        """
        self._started_at = self._active_now()
        while True:
            self._changed.clear()
            if self._engine_done and not self._open:
                return
            now = self._active_now()
            deadline, name = next_deadline(now, self._state(), self.limits)
            if deadline is None or name is None:
                await self._wait(None)
                continue
            if deadline > now:
                await self._wait(deadline - now)
                continue
            if name == "work_silence" and self.close_unsignalled_after_done():
                return
            if name == "hard_cap":
                value = cast(float, self.limits.hard_cap_s)
            elif name == "work_silence":
                value = self.limits.work_silence_s
            else:
                value = self.limits.model_silence_s
            await on_fire(name, value, now - self._started_at)
            return
