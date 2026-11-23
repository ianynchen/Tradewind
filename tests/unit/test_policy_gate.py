"""Unit tests for PolicyGate: gate, observe, broker compatibility."""

from __future__ import annotations

import anyio
import pytest

from tradewind.application.policy_gate import (
    Observation,
    PolicyGate,
    broker_args_with_action,
    caller_tool_action,
    caller_tool_source,
)
from tradewind.domain.events import Event, ItemCompleted
from tradewind.domain.models import Denial, SessionOptions
from tradewind.domain.policy import Action, Matcher, Rule, TurnVars, floor_tradewind


def _turn(mode="broker") -> TurnVars:
    return TurnVars(
        mode=mode, engine_mode=None, shim_server=None, workspace="/ws", result_tool=None
    )


def _action(name="merge_pull_request", args=None, kind="caller_tool") -> Action:
    args = {"pr": 7} if args is None else args
    return Action(
        kind=kind,
        fields={"name": name, "args": args},
        backend="claude",
        native_name=name,
        native_args=args,
        summary=f"{name} call",
    )


class _Broker:
    def __init__(self, answer="allow"):
        self.answer = answer
        self.calls: list[tuple[str, dict]] = []

    async def decide(self, tool_name, tool_input):
        self.calls.append((tool_name, tool_input))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _gate(broker=None, *, caller=(), defaults=(), floor=(), mode="broker", has_broker=True):
    events: list[Event] = []
    gate = PolicyGate(
        broker=broker or _Broker(),
        backend="claude",
        floor=floor,
        caller=caller,
        defaults=defaults,
        turn=_turn(mode),
        has_broker=has_broker,
        emit=events.append,
    )
    return gate, events


async def test_existing_broker_receives_todays_name_and_args():
    broker = _Broker()
    gate, _ = _gate(broker)
    args = {"pr": 7}
    decision = await gate.gate(
        _action(args=args), broker_name="merge_pull_request", broker_args=args
    )
    assert decision.outcome == "allow"
    name, seen = broker.calls[0]
    assert name == "merge_pull_request"
    assert seen["pr"] == 7
    assert seen["tradewind"]["kind"] == "caller_tool"
    assert args == {"pr": 7}


def test_broker_copy_holds_only_the_shared_fields():
    """05 §1.1: `{kind, fields, backend, native_name, summary}` -- never
    `native_args` or the internal flags."""
    out = broker_args_with_action({"pr": 7}, _action())
    assert out["tradewind"] == {
        "kind": "caller_tool",
        "fields": {"name": "merge_pull_request", "args": {"pr": 7}},
        "backend": "claude",
        "native_name": "merge_pull_request",
        "summary": "merge_pull_request call",
    }


def test_caller_tool_source_from_name():
    assert caller_tool_source("merge") == "local"
    assert caller_tool_source("mcp__gh__merge") == "mcp:gh"
    assert caller_tool_source("mcp__my_srv__t") == "mcp:my_srv"


def test_tradewind_key_collision_uses_tradewind_action():
    args = {"tradewind": "mine", "x": 1}
    out = broker_args_with_action(args, _action())
    assert out["tradewind"] == "mine"
    assert out["tradewind_action"]["kind"] == "caller_tool"
    assert args == {"tradewind": "mine", "x": 1}


async def test_pass_through_never_asks():
    broker = _Broker("deny")
    gate, _ = _gate(broker, caller=[Rule("d", "any", {}, "deny")])
    decision = await gate.gate(_action(), broker_name="n", broker_args={}, pass_through=True)
    assert decision.outcome == "allow"
    assert broker.calls == []


def test_observe_default_allow_and_deny_becomes_stop():
    gate, _ = _gate(mode="engine_auto", has_broker=False)
    assert gate.observe(_action()) == Observation("allow", None)
    gate2, _ = _gate(
        mode="engine_auto",
        caller=[
            Rule("d", "caller_tool", {"name": Matcher("equals", "x")}, "deny", reason="no x"),
            Rule("q", "caller_tool", {"name": Matcher("equals", "z")}, "ask"),
        ],
    )
    # A stop carries the matching rule's reason (None when it has none).
    assert gate2.observe(_action(name="x")) == Observation("stop", "no x")
    assert gate2.observe(_action(name="z")) == Observation("stop", None)
    # 05 §2.3: the observe default is `allow` in every mode -- a gate's
    # `ask` default never stops an observed action; only a matching rule does.
    assert gate2.observe(_action(name="y")) == Observation("allow", None)
    gate3, _ = _gate(mode="broker")
    assert gate3.observe(_action()) == Observation("allow", None)


async def test_engine_auto_without_broker_allows_without_asking():
    broker = _Broker("deny")
    gate, _ = _gate(broker, mode="engine_auto", has_broker=False)
    decision = await gate.gate(_action(), broker_name="n", broker_args={})
    assert decision.outcome == "allow"
    assert broker.calls == []


async def test_broker_exception_denies():
    gate, _ = _gate(_Broker(RuntimeError("boom")))
    decision = await gate.gate(_action(), broker_name="n", broker_args={})
    assert decision.outcome == "deny"
    assert "boom" in (decision.reason or "")
    assert gate.pending_approvals == 0


async def test_denial_terminate_sets_terminate():
    gate, _ = _gate(_Broker(Denial("no", terminate=True)))
    decision = await gate.gate(_action(), broker_name="n", broker_args={})
    assert decision.outcome == "deny"
    assert decision.reason == "no"
    assert decision.terminate is True


async def test_rule_deny_and_stop_skip_broker():
    broker = _Broker()
    gate, _ = _gate(broker, caller=[Rule("s", "any", {}, "stop", reason="halt")])
    decision = await gate.gate(_action(), broker_name="n", broker_args={})
    assert decision.outcome == "stop"
    assert decision.reason == "halt"
    assert broker.calls == []


async def test_pending_counter_released_on_cancel():
    started = anyio.Event()

    class _Blocking:
        async def decide(self, _tool_name, _tool_input):
            started.set()
            await anyio.sleep_forever()

    gate, _ = _gate(_Blocking())
    counts: list[int] = []
    gate.on_approval_change = counts.append
    async with anyio.create_task_group() as tg:

        async def run():
            await gate.gate(_action(), broker_name="n", broker_args={})

        tg.start_soon(run)
        await started.wait()
        assert gate.pending_approvals == 1
        tg.cancel_scope.cancel()
    assert gate.pending_approvals == 0
    assert counts == [1, 0]


async def test_record_emits_one_notice():
    rule = Rule("r", "caller_tool", {}, "allow", record=True)
    gate, events = _gate(caller=[rule])
    await gate.gate(_action(), broker_name="n", broker_args={})
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, ItemCompleted)
    assert ev.message.kind == "event"
    assert ev.message.content["action_kind"] == "caller_tool"
    assert ev.message.content["rule_ids"] == ["r"]


async def test_no_notice_on_plain_turn():
    gate, events = _gate()
    await gate.gate(_action(), broker_name="n", broker_args={})
    gate.observe(_action())
    assert events == []


async def test_as_broker_maps_outcomes():
    gate, _ = _gate(
        caller=[
            Rule("d", "caller_tool", {"name": Matcher("equals", "bad")}, "deny", reason="nope"),
            Rule("s", "caller_tool", {"name": Matcher("equals", "ugly")}, "stop", reason="halt"),
            Rule("a", "caller_tool", {"name": Matcher("equals", "good")}, "allow"),
        ]
    )
    b = gate.as_broker()
    assert getattr(b, "is_default_allow_all", False) is False
    assert await b.decide("good", {}) == "allow"
    assert await b.decide("bad", {}) == Denial("nope", False)
    assert await b.decide("ugly", {}) == Denial("halt", True)


async def test_as_broker_builds_caller_tool_action():
    seen: list[Action] = []
    gate, _ = _gate(
        caller=[
            Rule("a", "caller_tool", {"source": Matcher("equals", "local")}, "allow"),
            Rule("m", "caller_tool", {"source": Matcher("equals", "mcp:gh")}, "allow"),
        ]
    )
    orig = gate.gate

    async def spy(action, **kw):
        seen.append(action)
        return await orig(action, **kw)

    gate.gate = spy  # type: ignore[method-assign]
    assert await gate.as_broker().decide("t", {"a": 1}) == "allow"
    assert await gate.as_broker().decide("mcp__gh__t", {}) == "allow"
    assert seen[0].fields == {"name": "t", "args": {"a": 1}, "source": "local"}
    assert seen[1].fields["source"] == "mcp:gh"
    assert seen[0].backend == "claude"


async def test_pending_counter_paired_when_approval_hook_raises():
    """The +1 is inside the `try`, so the `finally` always pairs it."""
    gate, _ = _gate(_Broker())
    counts: list[int] = []

    def hook(count: int) -> None:
        counts.append(count)
        if count == 1:
            raise RuntimeError("hook")

    gate.on_approval_change = hook
    decision = await gate.gate(_action(), broker_name="n", broker_args={})
    assert decision.outcome == "deny"
    assert gate.pending_approvals == 0
    assert counts == [1, 0]


def test_snapshot_includes_permission_mode_and_rules():
    rule = Rule("r", "shell", {"cmd": Matcher("prefix", "rm")}, "deny", reason="x")
    opts = SessionOptions(permission_mode="engine_auto", policy_rules=[rule])
    snap = opts.snapshot()
    assert snap["permission_mode"] == "engine_auto"
    assert snap["policy_rules"] == [rule.to_dict()]
    assert SessionOptions().snapshot()["permission_mode"] is None
    assert SessionOptions().snapshot()["policy_rules"] == []


def test_a_caller_tool_maps_as_today():
    a = caller_tool_action("lookup", {"q": 1}, backend="claude")
    assert (a.kind, a.fields["name"], a.fields["source"], a.summary) == (
        "caller_tool",
        "lookup",
        "local",
        "lookup",
    )


def test_the_fetch_tool_maps_to_fetch_url():
    a = caller_tool_action("tradewind_fetch_url", {"url": "https://example.com"}, backend="codex")
    assert (a.kind, a.fields, a.missing_critical_field) == (
        "fetch_url",
        {"url": "https://example.com"},
        False,
    )


@pytest.mark.parametrize("args", [{}, {"url": 7}, {"url": None}])
def test_a_fetch_call_with_no_usable_url_is_missing_a_field(args):
    assert caller_tool_action(
        "tradewind_fetch_url", args, backend="langchain"
    ).missing_critical_field


async def test_broker_denies_a_fetch_call_without_a_url_by_the_floor():
    gate, _ = _gate(floor=floor_tradewind())
    assert isinstance(await gate.as_broker().decide("tradewind_fetch_url", {}), Denial)


async def test_broker_evaluates_a_fetch_call_as_fetch_url():
    rule = Rule("c", "fetch_url", {"url": Matcher("equals", "https://example.com")}, "allow")
    broker = _Broker("deny")
    gate, _ = _gate(broker, floor=floor_tradewind(), caller=[rule])
    out = await gate.as_broker().decide("tradewind_fetch_url", {"url": "https://example.com"})
    assert out == "allow"
    assert broker.calls == []
