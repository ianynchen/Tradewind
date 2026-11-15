"""Turn-governance policy gate (docs/components/05-turn-governance.md §2).

Runs the pure rule evaluation for one action and, on `ask`, consults the
caller's `PermissionBroker` with today's name and args plus the normalized
action under a `tradewind` key. Nothing here talks to an engine.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from typing import Literal

from tradewind.domain.events import Event, ItemCompleted
from tradewind.domain.models import (
    Denial,
    PermissionBroker,
    policy_record_notice,
)
from tradewind.domain.models import normalize_decision as _normalize
from tradewind.domain.policy import Action, Outcome, PolicyResult, Rule, TurnVars, evaluate


@dataclass(frozen=True)
class GateDecision:
    outcome: Literal["allow", "deny", "stop"]
    reason: str | None
    terminate: bool
    record: bool
    rule_ids: tuple[str, ...]


def broker_args_with_action(args: dict[str, object], action: Action) -> dict[str, object]:
    """Copy of `args` with the action added under `tradewind`, or under
    `tradewind_action` when the tool already has a `tradewind` argument."""
    key = "tradewind_action" if "tradewind" in args else "tradewind"
    return {**args, key: asdict(action)}


class _GateBroker:
    """A `PermissionBroker` view of a gate; never the default allow-all."""

    is_default_allow_all = False

    def __init__(self, gate: PolicyGate, source: str) -> None:
        self._gate = gate
        self._source = source

    async def decide(self, tool_name: str, tool_input: dict[str, object]) -> str | Denial:
        action = Action(
            kind="caller_tool",
            fields={"name": tool_name, "args": tool_input, "source": self._source},
            backend=self._gate._backend,
            native_name=tool_name,
            native_args=tool_input,
        )
        d = await self._gate.gate(action, broker_name=tool_name, broker_args=tool_input)
        if d.outcome == "allow":
            return "allow"
        return Denial(d.reason, d.terminate or d.outcome == "stop")


class PolicyGate:
    def __init__(
        self,
        *,
        broker: PermissionBroker,
        backend: str,
        floor: Sequence[Rule],
        caller: Sequence[Rule],
        defaults: Sequence[Rule],
        turn: TurnVars,
        has_broker: bool,
        emit: Callable[[Event], None],
    ) -> None:
        self._broker = broker
        self._backend = backend
        self._floor = floor
        self._caller = caller
        self._defaults = defaults
        self._turn = turn
        self.has_broker = has_broker
        self._emit = emit
        self._pending = 0
        self.on_approval_change: Callable[[int], None] | None = None

    @property
    def pending_approvals(self) -> int:
        return self._pending

    def _mode_default(self) -> Outcome:
        if self._turn.mode == "engine_auto" and not self.has_broker:
            return "allow"
        return "ask"

    def _evaluate(self, action: Action, mode_default: Outcome) -> PolicyResult:
        result = evaluate(
            action,
            floor=self._floor,
            caller=self._caller,
            defaults=self._defaults,
            mode_default=mode_default,
            turn=self._turn,
        )
        if result.record:
            self._emit(
                ItemCompleted(
                    message=policy_record_notice(
                        action_kind=action.kind,
                        summary=action.summary,
                        outcome=result.outcome,
                        rule_ids=list(result.rule_ids),
                    )
                )
            )
        return result

    def observe(self, action: Action) -> Literal["allow", "stop"]:
        """Decide an action the engine already runs: `deny` and `ask` stop.
        The mode default is `allow` in every mode (05 §2.3), so only a
        matching rule can stop the turn."""
        return "allow" if self._evaluate(action, "allow").outcome == "allow" else "stop"

    async def gate(
        self,
        action: Action,
        *,
        broker_name: str,
        broker_args: dict[str, object],
        pass_through: bool = False,
    ) -> GateDecision:
        if pass_through:
            return GateDecision("allow", None, False, False, ())
        result = self._evaluate(action, self._mode_default())
        ids = result.rule_ids
        if result.outcome == "allow":
            return GateDecision("allow", None, False, result.record, ids)
        if result.outcome in ("deny", "stop"):
            return GateDecision(result.outcome, result.reason or None, False, result.record, ids)
        self._set_pending(self._pending + 1)
        try:
            raw = await self._broker.decide(
                broker_name, broker_args_with_action(broker_args, action)
            )
            verdict, reason, terminate = _normalize(raw)
        except Exception as exc:
            return GateDecision(
                "deny", f"permission broker failed: {exc}", False, result.record, ids
            )
        finally:
            self._set_pending(self._pending - 1)
        if verdict == "allow":
            return GateDecision("allow", None, False, result.record, ids)
        return GateDecision("deny", reason, terminate, result.record, ids)

    def _set_pending(self, count: int) -> None:
        self._pending = count
        if self.on_approval_change is not None:
            self.on_approval_change(count)

    def as_broker(self, source: str) -> PermissionBroker:
        """A broker whose decisions go through this gate (for `ToolHost`)."""
        return _GateBroker(self, source)  # type: ignore[return-value]
