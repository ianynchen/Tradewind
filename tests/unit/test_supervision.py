"""Unit tests for the work supervisor (05-turn-governance §3): the pure
limit arithmetic in `domain.supervision` and the `WorkSupervisor` loop.

A fake clock and a fake sleep drive every test: the fake sleep advances
the clock by the requested delay and returns at once, so "nothing happened
for N seconds" costs no real time (GUIDELINES §10: no sleeps).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from tradewind.application.supervisor import WorkSupervisor
from tradewind.domain.supervision import Limits, SupervisorState, next_deadline

_LIMITS = Limits(model_silence_s=300.0, work_silence_s=1800.0, hard_cap_s=None)


class _Clock:
    """Fake monotonic clock. `sleep` jumps to the wake-up time, or to the
    first callback scheduled with `at` before it, and runs that callback
    -- a signal arriving while the supervisor waits."""

    def __init__(self) -> None:
        self.now = 0.0
        self._scheduled: list[tuple[float, Callable[[], None]]] = []

    def __call__(self) -> float:
        return self.now

    def at(self, when: float, callback: Callable[[], None]) -> None:
        self._scheduled.append((when, callback))
        self._scheduled.sort(key=lambda entry: entry[0])

    async def sleep(self, delay: float) -> None:
        wake = self.now + delay
        if self._scheduled and self._scheduled[0][0] <= wake:
            when, callback = self._scheduled.pop(0)
            self.now = when
            callback()
            return
        self.now = wake


class _Fired:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float, float]] = []

    async def __call__(self, limit: str, value: float, elapsed: float) -> None:
        self.calls.append((limit, value, elapsed))


def _supervisor(
    clock: _Clock, limits: Limits = _LIMITS, *, reports_work: bool = True
) -> WorkSupervisor:
    return WorkSupervisor(limits, reports_work=reports_work, clock=clock, sleep=clock.sleep)


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def _state(**changes: object) -> SupervisorState:
    base: dict[str, object] = {
        "started_at": 0.0,
        "last_model_at": 0.0,
        "last_work_at": 0.0,
        "has_open_work": False,
        "reports_work": True,
        "pending_approvals": 0,
    }
    base.update(changes)
    return SupervisorState(**base)  # type: ignore[arg-type]


# --- domain arithmetic ---


def test_next_deadline_model_silence_without_open_work() -> None:
    assert next_deadline(10.0, _state(last_model_at=50.0), _LIMITS) == (350.0, "model_silence")


def test_next_deadline_work_silence_with_open_work() -> None:
    state = _state(has_open_work=True, last_model_at=900.0, last_work_at=100.0)
    # Model activity does not keep stuck work alive: only the work's own
    # reports count while it is open.
    assert next_deadline(10.0, state, _LIMITS) == (1900.0, "work_silence")


def test_next_deadline_none_while_an_approval_is_pending() -> None:
    limits = Limits(model_silence_s=300.0, work_silence_s=1800.0, hard_cap_s=5.0)
    assert next_deadline(10.0, _state(pending_approvals=1), limits) == (None, None)


def test_next_deadline_hard_cap_wins_when_earlier() -> None:
    limits = Limits(model_silence_s=300.0, work_silence_s=1800.0, hard_cap_s=60.0)
    assert next_deadline(10.0, _state(), limits) == (60.0, "hard_cap")


def test_next_deadline_without_work_reports_only_the_hard_cap_applies() -> None:
    limits = Limits(model_silence_s=300.0, work_silence_s=1800.0, hard_cap_s=600.0)
    state = _state(reports_work=False)
    assert next_deadline(10.0, state, limits) == (600.0, "hard_cap")
    unbounded = Limits(model_silence_s=300.0, work_silence_s=1800.0, hard_cap_s=None)
    assert next_deadline(10.0, state, unbounded) == (None, None)


def test_next_deadline_already_passed_fires_now() -> None:
    assert next_deadline(400.0, _state(), _LIMITS) == (400.0, "model_silence")


# --- WorkSupervisor ---


async def test_model_silence_fires_without_open_work() -> None:
    clock = _Clock()
    fired = _Fired()

    await _supervisor(clock).run(fired)

    assert fired.calls == [("model_silence", 300.0, 300.0)]
    assert clock.now == 300.0


async def test_model_active_resets_model_silence() -> None:
    clock = _Clock()
    fired = _Fired()
    sup = _supervisor(clock)
    clock.at(200.0, sup.model_active)

    await sup.run(fired)

    # Measured from the last model signal (200), not from turn start.
    assert fired.calls == [("model_silence", 300.0, 500.0)]


async def test_open_work_uses_work_silence() -> None:
    clock = _Clock()
    fired = _Fired()
    sup = _supervisor(clock)
    sup.started("t1", "tool", "bash: make test")

    await sup.run(fired)

    # Not model silence (300 s): a tool is running.
    assert fired.calls == [("work_silence", 1800.0, 1800.0)]
    assert [item.id for item in sup.open_items()] == ["t1"]


async def test_active_resets_work_silence() -> None:
    clock = _Clock()
    fired = _Fired()
    sup = _supervisor(clock)
    sup.started("t1", "background", "server")
    clock.at(1000.0, lambda: sup.active("t1"))

    await sup.run(fired)

    assert fired.calls == [("work_silence", 1800.0, 2800.0)]


async def test_ended_work_returns_to_model_silence() -> None:
    clock = _Clock()
    fired = _Fired()
    sup = _supervisor(clock)
    sup.started("t1", "tool", "read")
    clock.at(50.0, lambda: sup.ended("t1"))

    await sup.run(fired)

    assert sup.open_items() == []
    # Model silence counts from the start; the work's end is not model
    # activity.
    assert fired.calls == [("model_silence", 300.0, 300.0)]


async def test_pending_approval_pauses_all_limits() -> None:
    clock = _Clock()
    fired = _Fired()
    limits = Limits(model_silence_s=300.0, work_silence_s=1800.0, hard_cap_s=400.0)
    sup = _supervisor(clock, limits)
    sup.approvals_changed(1)
    task = asyncio.ensure_future(sup.run(fired))
    await _settle()

    # A human takes an hour to answer: nothing may fire meanwhile.
    clock.now = 3600.0
    await _settle()
    assert fired.calls == []
    assert not task.done()

    sup.approvals_changed(0)
    await task

    # The hour does not count: model silence fires 300 s of active time
    # later, before the 400 s hard cap.
    assert fired.calls == [("model_silence", 300.0, 300.0)]
    assert clock.now == 3900.0


async def test_hard_cap_none_means_no_cap() -> None:
    clock = _Clock()
    fired = _Fired()
    sup = _supervisor(clock)
    sup.started("t1", "tool", "long build")
    for step in range(1, 36):
        # Ten hours of work reporting every 1000 s: no limit applies.
        clock.at(step * 1000.0, lambda: sup.active("t1"))

    def finish() -> None:
        sup.ended("t1")
        sup.engine_done()

    clock.at(36000.0, finish)

    await sup.run(fired)

    assert fired.calls == []
    assert clock.now == 36000.0


async def test_hard_cap_fires_on_total_time() -> None:
    clock = _Clock()
    fired = _Fired()
    limits = Limits(model_silence_s=300.0, work_silence_s=1800.0, hard_cap_s=100.0)

    await _supervisor(clock, limits).run(fired)

    assert fired.calls == [("hard_cap", 100.0, 100.0)]


async def test_without_work_reports_silence_limits_never_fire() -> None:
    clock = _Clock()
    fired = _Fired()
    limits = Limits(model_silence_s=300.0, work_silence_s=1800.0, hard_cap_s=600.0)

    await _supervisor(clock, limits, reports_work=False).run(fired)

    # A backend that reports no activity would trip model silence at
    # 300 s on every turn; only the hard cap applies (Cursor).
    assert fired.calls == [("hard_cap", 600.0, 600.0)]


async def test_engine_done_with_no_open_work_ends_the_run() -> None:
    clock = _Clock()
    fired = _Fired()
    sup = _supervisor(clock)
    sup.engine_done()

    await sup.run(fired)

    assert fired.calls == []
    assert clock.now == 0.0


async def test_unsignalled_work_after_engine_done_is_normal_end() -> None:
    clock = _Clock()
    fired = _Fired()
    sup = _supervisor(clock)
    sup.started("sub1", "subagent", "agy subagent", has_end_signal=False)
    clock.at(100.0, sup.engine_done)

    await sup.run(fired)

    # Closed after the work-silence limit, as a normal end: no fire.
    assert fired.calls == []
    assert clock.now == 1900.0
    assert sup.open_items() == []
    assert [item.id for item in sup.closed_after_done()] == ["sub1"]
    assert await sup.settled() is True


async def test_unsignalled_work_is_not_closed_before_engine_done() -> None:
    clock = _Clock()
    fired = _Fired()
    sup = _supervisor(clock)
    sup.started("sub1", "subagent", "agy subagent", has_end_signal=False)

    await sup.run(fired)

    # The engine never said done: quiet work is stuck work, a timeout.
    assert fired.calls == [("work_silence", 1800.0, 1800.0)]
    assert sup.closed_after_done() == []
    assert [item.id for item in sup.open_items()] == ["sub1"]


async def test_signalled_open_work_after_done_times_out_on_work_silence() -> None:
    clock = _Clock()
    fired = _Fired()
    sup = _supervisor(clock)
    sup.started("sub1", "subagent", "agy subagent", has_end_signal=False)
    sup.started("t1", "tool", "bash")
    sup.engine_done()

    await sup.run(fired)

    # An item that can say "ended" never did: the whole turn times out.
    assert fired.calls == [("work_silence", 1800.0, 1800.0)]
    assert {item.id for item in sup.open_items()} == {"sub1", "t1"}
    assert sup.closed_after_done() == []


# --- settled(): what an adapter awaits before its terminal event (§3.3) ---


async def test_settled_is_true_when_the_engine_is_done_and_no_work_is_open() -> None:
    clock = _Clock()
    fired = _Fired()
    sup = _supervisor(clock)
    sup.started("t1", "tool", "bash")
    settled = asyncio.ensure_future(sup.settled())

    def finish() -> None:
        # The adapter kept reading the engine: the tool ends, then done.
        assert not settled.done()
        sup.ended("t1")
        sup.engine_done()

    clock.at(10.0, finish)

    await sup.run(fired)

    assert await settled is True
    assert fired.calls == []


async def test_settled_is_true_after_unsignalled_work_closes() -> None:
    clock = _Clock()
    sup = _supervisor(clock)
    sup.started("sub1", "subagent", "agy subagent", has_end_signal=False)
    sup.engine_done()
    settled = asyncio.ensure_future(sup.settled())

    await sup.run(_Fired())

    assert await settled is True
    assert [item.id for item in sup.closed_after_done()] == ["sub1"]


async def test_settled_is_false_when_a_limit_fired() -> None:
    clock = _Clock()
    fired = _Fired()
    sup = _supervisor(clock)
    settled = asyncio.ensure_future(sup.settled())

    await sup.run(fired)

    assert fired.calls == [("model_silence", 300.0, 300.0)]
    assert await settled is False


async def test_settled_is_false_when_run_is_cancelled() -> None:
    # The runner cancels the supervisor at the turn's end; an adapter
    # still waiting must not hang.
    sup = _supervisor(_Clock())
    sup.approvals_changed(1)
    run = asyncio.ensure_future(sup.run(_Fired()))
    settled = asyncio.ensure_future(sup.settled())
    await _settle()

    run.cancel()

    assert await settled is False
