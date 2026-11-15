"""Turn-governance action model and pure rule evaluation.

Implements docs/components/05-turn-governance.md §1-§2. An `Action` is the
normalized thing a backend wants to do; `evaluate` decides `allow`, `ask`,
`deny` or `stop` from floor, caller and default rules plus a mode default.
No I/O happens here.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, get_args

ActionKind = Literal[
    "caller_tool",
    "shell",
    "file_read",
    "file_write",
    "mcp",
    "fetch_url",
    "web_search",
    "subagent",
    "input_to_running",
    "mode_change",
    "question",
    "native",
]
Outcome = Literal["allow", "ask", "deny", "stop"]
OUTCOME_RANK: dict[str, int] = {"allow": 0, "ask": 1, "deny": 2, "stop": 3}
Mode = Literal["broker", "engine_auto"]
MatchOp = Literal["equals", "ne", "prefix", "regex", "glob", "is_true", "is_false"]

_ALL_MODES: frozenset[Mode] = frozenset(get_args(Mode))
_MISSING = object()


@dataclass(frozen=True)
class Action:
    """One normalized action a backend wants to perform.

    `fields` holds the kind's matchable fields; `native_name` and
    `native_args` keep the engine's own call. `unknown_tool` and
    `missing_critical_field` mark actions that could not be mapped safely.
    """

    kind: ActionKind
    fields: dict[str, object]
    backend: str
    native_name: str
    native_args: dict[str, object]
    summary: str = ""
    unknown_tool: bool = False
    missing_critical_field: bool = False


@dataclass(frozen=True)
class TurnVars:
    """Per-turn values a matcher can name as `$turn.<attr>`."""

    mode: Mode
    engine_mode: str | None
    shim_server: str | None
    workspace: str | None
    result_tool: str | None


@dataclass(frozen=True)
class Matcher:
    """Test of one action field. A missing field never matches, except `ne`."""

    op: MatchOp
    value: object = None

    def matches(self, actual: object, turn: TurnVars) -> bool:
        expected = self.value
        if isinstance(expected, str) and expected.startswith("$turn."):
            expected = getattr(turn, expected[len("$turn.") :])
        if self.op == "ne":
            return actual is _MISSING or actual != expected
        if actual is _MISSING:
            return False
        if self.op == "equals":
            return actual == expected
        if self.op == "is_true":
            return actual is True
        if self.op == "is_false":
            return actual is False
        if not isinstance(actual, str) or not isinstance(expected, str):
            return False
        if self.op == "prefix":
            return actual.startswith(expected)
        if self.op == "regex":
            return re.search(expected, actual) is not None
        return fnmatch.fnmatchcase(actual, expected)  # glob

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op, "value": self.value}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Matcher:
        return cls(op=d["op"], value=d.get("value"))


@dataclass(frozen=True)
class Rule:
    """A policy rule: when `kind` and every `where` matcher match, `outcome` applies."""

    id: str
    kind: ActionKind | Literal["any"]
    where: dict[str, Matcher]
    outcome: Outcome
    record: bool = False
    modes: frozenset[Mode] = field(default_factory=lambda: _ALL_MODES)
    reason: str = ""

    def matches(self, action: Action, turn: TurnVars) -> bool:
        if turn.mode not in self.modes:
            return False
        if self.kind != "any" and self.kind != action.kind:
            return False
        return all(
            m.matches(action.fields.get(name, _MISSING), turn) for name, m in self.where.items()
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "where": {k: m.to_dict() for k, m in self.where.items()},
            "outcome": self.outcome,
            "record": self.record,
            "modes": sorted(self.modes),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Rule:
        return cls(
            id=d["id"],
            kind=d["kind"],
            where={k: Matcher.from_dict(m) for k, m in d["where"].items()},
            outcome=d["outcome"],
            record=d.get("record", False),
            modes=frozenset(d.get("modes", _ALL_MODES)),
            reason=d.get("reason", ""),
        )


@dataclass(frozen=True)
class PolicyResult:
    outcome: Outcome
    record: bool
    rule_ids: tuple[str, ...]
    reason: str


def floor_tradewind() -> list[Rule]:
    """Tradewind's own floor rules (§2.4)."""
    return [
        Rule(
            id="floor.missing_critical_field",
            kind="any",
            where={},
            outcome="deny",
            record=True,
            reason="action is missing a critical field",
        )
    ]


def _matches_flag(rule: Rule, action: Action, turn: TurnVars) -> bool:
    # `missing_critical_field` is a flag on the action, not a field; the
    # Tradewind floor rule is the one place it is consulted.
    if rule.id == "floor.missing_critical_field":
        return action.missing_critical_field and rule.matches(action, turn)
    return rule.matches(action, turn)


def evaluate(
    action: Action,
    *,
    floor: Sequence[Rule],
    caller: Sequence[Rule],
    defaults: Sequence[Rule],
    mode_default: Outcome,
    turn: TurnVars,
) -> PolicyResult:
    """Decide an action (§2.3).

    Final outcome is the stronger of the strongest matching floor rule and
    the chosen outcome (first caller match, else first default match, else
    `mode_default`). `record` ORs matching floor rules with the chosen rule.

    Raises:
        ValueError: a floor rule's outcome is `allow` or `ask`.
    """
    for rule in floor:
        if rule.outcome in ("allow", "ask"):
            raise ValueError(f"floor rule {rule.id!r} may only deny or stop")
    floor_hits = [r for r in floor if _matches_flag(r, action, turn)]
    chosen = next((r for r in (*caller, *defaults) if r.matches(action, turn)), None)
    outcome: Outcome = chosen.outcome if chosen else mode_default
    reason = chosen.reason if chosen else ""
    for r in floor_hits:
        if OUTCOME_RANK[r.outcome] > OUTCOME_RANK[outcome]:
            outcome, reason = r.outcome, r.reason
    ids = tuple([r.id for r in floor_hits] + ([chosen.id] if chosen else []))
    record = any(r.record for r in floor_hits) or (chosen.record if chosen else False)
    return PolicyResult(outcome=outcome, record=record, rule_ids=ids, reason=reason)
