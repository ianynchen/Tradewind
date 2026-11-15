"""Unit tests for the turn-governance action model and rule evaluation."""

from __future__ import annotations

import json

import pytest

from tradewind.domain.policy import (
    Action,
    Matcher,
    PolicyResult,
    Rule,
    TurnVars,
    evaluate,
    floor_tradewind,
)

TURN = TurnVars(
    mode="broker",
    engine_mode="default",
    shim_server=None,
    workspace="/ws",
    result_tool=None,
)


def _action(kind="shell", **fields) -> Action:
    return Action(
        kind=kind,
        fields=fields,
        backend="claude",
        native_name="x",
        native_args={},
    )


def _run(action, *, floor=(), caller=(), defaults=(), mode_default="ask", turn=TURN):
    return evaluate(
        action,
        floor=floor,
        caller=caller,
        defaults=defaults,
        mode_default=mode_default,
        turn=turn,
    )


def test_floor_deny_beats_caller_allow():
    floor = [Rule("f1", "shell", {"sandboxed": Matcher("is_false")}, "deny")]
    caller = [Rule("c1", "shell", {}, "allow")]
    res = _run(_action(sandboxed=False), floor=floor, caller=caller)
    assert res.outcome == "deny"
    assert "f1" in res.rule_ids


def test_caller_can_tighten_default():
    defaults = [Rule("d1", "web_search", {}, "allow")]
    caller = [Rule("c1", "web_search", {}, "stop")]
    res = _run(_action("web_search"), caller=caller, defaults=defaults)
    assert res.outcome == "stop"


def test_first_match_caller_then_defaults_then_mode_default():
    defaults = [Rule("d1", "shell", {}, "allow")]
    caller = [Rule("c1", "fetch_url", {}, "deny")]
    assert _run(_action(), caller=caller, defaults=defaults).outcome == "allow"
    assert (
        _run(_action("mcp"), caller=caller, defaults=defaults, mode_default="ask").outcome == "ask"
    )


def test_record_is_floor_or_chosen_rule():
    floor = [Rule("f1", "shell", {}, "deny", record=False)]
    caller = [Rule("c1", "shell", {}, "ask", record=True)]
    defaults = [Rule("d1", "shell", {}, "allow", record=True)]
    assert _run(_action(), floor=floor, caller=caller, defaults=defaults).record is True
    caller = [Rule("c1", "shell", {}, "ask", record=False)]
    assert _run(_action(), floor=floor, caller=caller, defaults=defaults).record is False
    floor = [Rule("f1", "shell", {}, "deny", record=True)]
    assert _run(_action(), floor=floor, caller=caller, defaults=defaults).record is True


def test_modes_filter():
    caller = [Rule("c1", "shell", {}, "deny", modes=frozenset({"broker"}))]
    engine_turn = TurnVars("engine_auto", None, None, None, None)
    assert _run(_action(), caller=caller, turn=engine_turn, mode_default="allow").outcome == "allow"
    assert _run(_action(), caller=caller).outcome == "deny"


def test_turn_variables():
    caller = [
        Rule(
            "c1",
            "mode_change",
            {"to": Matcher("ne", "$turn.engine_mode")},
            "deny",
        )
    ]
    assert (
        _run(_action("mode_change", to="auto"), caller=caller, mode_default="allow").outcome
        == "deny"
    )
    assert (
        _run(_action("mode_change", to="default"), caller=caller, mode_default="allow").outcome
        == "allow"
    )


def test_missing_critical_field_denied_by_tradewind_floor():
    action = Action("shell", {}, "claude", "Bash", {}, missing_critical_field=True)
    res = _run(action, floor=floor_tradewind(), caller=[Rule("c", "any", {}, "allow")])
    assert res.outcome == "deny"
    assert res.record is True


@pytest.mark.parametrize("outcome", ["allow", "ask"])
def test_floor_rules_reject_allow_and_ask(outcome):
    with pytest.raises(ValueError):
        _run(_action(), floor=[Rule("f", "shell", {}, outcome)])


def test_matchers():
    def m(op, value, field):
        rule = Rule("r", "any", {"f": Matcher(op, value)}, "deny")
        fields = {} if field is None else {"f": field}
        return _run(_action(**fields), caller=[rule], mode_default="allow").outcome == "deny"

    assert m("equals", "a", "a") and not m("equals", "a", "b")
    assert m("prefix", "git ", "git push") and not m("prefix", "git ", "ls")
    assert m("regex", r"rm\s+-rf", "sudo rm  -rf /") and not m("regex", r"^rm", "ls")
    assert m("glob", "/ws/*.py", "/ws/a.py") and not m("glob", "/ws/*.py", "/etc/a.py")
    assert m("is_true", None, True) and not m("is_true", None, False)
    assert m("is_false", None, False) and not m("is_false", None, True)
    # A missing field never matches, except `ne`.
    assert not m("equals", "a", None) and not m("is_false", None, None)
    assert m("ne", "a", None)
    assert m("ne", "a", "b") and not m("ne", "a", "a")


def test_turn_workspace_variable_in_glob():
    rule = Rule("r", "file_write", {"path": Matcher("ne", "$turn.workspace")}, "deny")
    assert (
        _run(_action("file_write", path="/o"), caller=[rule], mode_default="allow").outcome
        == "deny"
    )


def test_result_is_policy_result():
    assert isinstance(_run(_action()), PolicyResult)


def test_rule_round_trip_json_safe():
    rule = Rule(
        "r1",
        "shell",
        {"cmd": Matcher("prefix", "git "), "sandboxed": Matcher("is_false")},
        "ask",
        record=True,
        modes=frozenset({"broker", "engine_auto"}),
        reason="why",
    )
    d = rule.to_dict()
    assert d["modes"] == ["broker", "engine_auto"]
    assert Rule.from_dict(json.loads(json.dumps(d))) == rule
    m = Matcher("regex", "a.*")
    assert Matcher.from_dict(json.loads(json.dumps(m.to_dict()))) == m


def test_tradewind_floor_does_not_deny_normal_action():
    res = _run(_action(), floor=floor_tradewind(), mode_default="allow")
    assert res.outcome == "allow"
    assert res.rule_ids == ()


def test_rule_can_match_unknown_tool_flag():
    rule = Rule("c", "any", {"unknown_tool": Matcher("is_true")}, "deny")
    flagged = Action("native", {}, "claude", "Mystery", {}, unknown_tool=True)
    assert _run(flagged, caller=[rule], mode_default="allow").outcome == "deny"
    assert _run(_action(), caller=[rule], mode_default="allow").outcome == "allow"
