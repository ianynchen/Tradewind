"""Preflight checks (05-turn-governance §4): `run_checks` and the
`SessionOptions.on_preflight` callback."""

from __future__ import annotations

from typing import cast

import pytest

from tradewind.application.ports import TurnContext
from tradewind.application.preflight import Ask, Check, Ok, Stop, run_checks
from tradewind.domain.errors import ConfigError
from tradewind.domain.models import OnPreflight, SessionOptions


def _ctx(on_preflight: OnPreflight | None = None) -> TurnContext:
    """`run_checks` reads only `ctx.on_preflight` and writes only
    `ctx.preflight`."""

    class _Ctx:
        def __init__(self) -> None:
            self.preflight: dict[str, str] = {}
            self.on_preflight = on_preflight

    return cast(TurnContext, _Ctx())


def _ask(default_choice: str = "keep") -> Check:
    async def check(_ctx: TurnContext) -> Ok | Ask | Stop:
        return Ask(
            question="Delete stale files?",
            details={"count": 2},
            choices=["keep", "delete"],
            default_choice=default_choice,
        )

    return check


async def _ok(_ctx: TurnContext) -> Ok | Ask | Stop:
    return Ok()


async def test_ask_without_callback_uses_default() -> None:
    ctx = _ctx()
    await run_checks(ctx, [("ok", _ok), ("stale", _ask())])
    assert ctx.preflight == {"stale": "keep"}


async def test_ask_answer_from_callback_is_stored() -> None:
    seen: list[tuple[str, dict[str, object], list[str]]] = []

    async def ask(question: str, details: dict[str, object], choices: list[str]) -> str:
        seen.append((question, details, choices))
        return "delete"

    ctx = _ctx(ask)
    await run_checks(ctx, [("stale", _ask())])
    assert ctx.preflight == {"stale": "delete"}
    assert seen == [("Delete stale files?", {"count": 2}, ["keep", "delete"])]


async def test_callback_error_uses_default() -> None:
    async def ask(_q: str, _d: dict[str, object], _c: list[str]) -> str:
        raise RuntimeError("boom")

    ctx = _ctx(ask)
    await run_checks(ctx, [("stale", _ask())])
    assert ctx.preflight == {"stale": "keep"}


async def test_answer_not_in_choices_uses_default() -> None:
    async def ask(_q: str, _d: dict[str, object], _c: list[str]) -> str:
        return "shred"

    ctx = _ctx(ask)
    await run_checks(ctx, [("stale", _ask())])
    assert ctx.preflight == {"stale": "keep"}


async def test_stop_raises_config_error() -> None:
    async def stop(_ctx: TurnContext) -> Ok | Ask | Stop:
        return Stop(reason="workspace is locked")

    ran: list[str] = []

    async def later(_ctx: TurnContext) -> Ok | Ask | Stop:
        ran.append("later")
        return Ok()

    with pytest.raises(ConfigError, match="workspace is locked"):
        await run_checks(_ctx(), [("lock", stop), ("later", later)])
    assert ran == []


def test_on_preflight_not_in_snapshot() -> None:
    async def ask(_q: str, _d: dict[str, object], _c: list[str]) -> str:
        return "keep"

    options = SessionOptions(on_preflight=ask)
    assert options.on_preflight is ask
    assert "on_preflight" not in options.snapshot()
