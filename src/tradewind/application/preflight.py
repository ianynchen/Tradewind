"""Preflight checks (05-turn-governance §4): an ordered list of checks a
backend runs in `Backend.prepare_turn`, before the work supervisor starts
and before `TurnStarted`.

A check returns `Ok`, `Ask` (a question for the caller's `on_preflight`
callback) or `Stop` (end the turn with `ConfigError`). A check only
decides; anything the answer needs to change is done by `run()`, before
`TurnStarted` (§4).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from tradewind.application.ports import TurnContext
from tradewind.domain.errors import ConfigError

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Ok:
    """The check passed; nothing to ask."""


@dataclass(frozen=True)
class Ask:
    """A question for the caller.

    Args:
        question: the text shown to the caller.
        details: data that explains the question (e.g. the files found).
        choices: the answers the caller may give.
        default_choice: the answer used when there is no callback, the
            callback raises, or it returns a value not in `choices`;
            always the safe one.
    """

    question: str
    details: dict[str, object]
    choices: list[str]
    default_choice: str


@dataclass(frozen=True)
class Stop:
    """The turn must not run; `reason` becomes the `ConfigError` message."""

    reason: str


Check = Callable[[TurnContext], Awaitable[Ok | Ask | Stop]]
OnPreflight = Callable[[str, dict[str, object], list[str]], Awaitable[str]]


async def run_checks(
    ctx: TurnContext,
    checks: Sequence[tuple[str, Check]],
    ask: OnPreflight | None,
) -> None:
    """Run `checks` in order and store each `Ask` answer in
    `ctx.preflight[check_id]`.

    Args:
        ctx: the turn; only `ctx.preflight` is written.
        checks: `(check_id, check)` pairs, run one at a time in order.
        ask: the caller's `on_preflight` callback, or None.

    Failure modes:
        ConfigError: a check returned `Stop`; later checks do not run.
        A callback error, or an answer not in `choices`, is logged and the
        check's `default_choice` is used; it is not raised. Cancellation
        (e.g. `stop()` while a question waits) propagates.
    """
    for check_id, check in checks:
        result = await check(ctx)
        if isinstance(result, Stop):
            raise ConfigError(result.reason)
        if isinstance(result, Ask):
            ctx.preflight[check_id] = await _answer(check_id, result, ask)


async def _answer(check_id: str, question: Ask, ask: OnPreflight | None) -> str:
    """The caller's answer to `question`, or its `default_choice`."""
    if ask is None:
        return question.default_choice
    try:
        answer = await ask(question.question, question.details, question.choices)
    except Exception:
        _logger.exception(
            "on_preflight raised; using the default choice", extra={"check": check_id}
        )
        return question.default_choice
    if answer not in question.choices:
        _logger.warning(
            "on_preflight answer %r is not one of the choices; using the default choice",
            answer,
            extra={"check": check_id},
        )
        return question.default_choice
    return answer
