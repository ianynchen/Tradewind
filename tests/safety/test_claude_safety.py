"""Live safety traps for the Claude engine (docs/components/06-workspace-safety.md §6).

On demand only: gated by `TRADEWIND_RUN_SAFETY=1`, marked `integration`,
never run by `check.sh` or CI. It uses the machine's Claude subscription
login and the SDK's bundled CLI. The owner starts it, time-boxed:

    TRADEWIND_RUN_SAFETY=1 timeout 1500 uv run pytest tests/safety -v -m integration

Add `-rP` to see the printed engine versions of passed tests.

Each test checks a fact that an engine upgrade can change. Each trap has a
control run that shows the effect without the protection. When the control
does not show the effect, the test fails with "inconclusive: <why>"; it
never passes without positive evidence.

Workspaces, markers and backups are in the test's temp folder only. The
tests never edit `~/.claude` (the CLI still writes its own session
transcripts there, as on every Claude turn).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shlex
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import anyio
import pytest

from tradewind.adapters.claude_backend import _MCP_SERVER_NAME, ClaudeBackend
from tradewind.application.client import Session, Tradewind
from tradewind.application.config import TradewindConfig
from tradewind.domain.events import Event, ItemCompleted, TurnCompleted, TurnFailed
from tradewind.domain.models import (
    CustomizationDecision,
    ModelSpec,
    Profile,
    SessionOptions,
    SubscriptionAuth,
    Tool,
    TurnResult,
)

pytestmark = pytest.mark.integration

if not os.environ.get("TRADEWIND_RUN_SAFETY"):
    pytest.skip(
        "set TRADEWIND_RUN_SAFETY=1 to run the live safety suite against a real "
        "Claude Code CLI (subscription login, real model calls)",
        allow_module_level=True,
    )

_MODEL = "haiku"
_CAP_S = 180
_PONG = "Reply with exactly one word: pong"
_PROBE = "probe_tool"
_PROBE_FULL_NAME = f"mcp__{_MCP_SERVER_NAME}__{_PROBE}"
_CALL_PROBE = (
    f"Call the tool {_PROBE_FULL_NAME} exactly once with the input "
    '{"x": "a"}. Then reply with the word done.'
)
# Words that show a turn failed on authentication, not for another reason.
_AUTH_MARKERS = (
    "api key",
    "api_key",
    "apikey",
    "401",
    "auth",
    "unauthorized",
    "credential",
    "token",
    "login",
    "log in",
    "invalid",
)


@dataclass
class _Outcome:
    events: list[Event]
    result: TurnResult | None
    error: str | None

    def describe(self) -> str:
        lines = [f"result: {self.result!r}", f"error: {self.error!r}"]
        lines += [repr(e)[:400] for e in self.events if isinstance(e, ItemCompleted)]
        return "\n".join(lines)


@dataclass
class _RecordingBroker:
    calls: list[str] = field(default_factory=list)

    async def decide(self, tool_name: str, _tool_input: dict[str, object]) -> str:
        self.calls.append(tool_name)
        return "allow"


@dataclass
class _Probe:
    """A harmless caller tool that records each call."""

    calls: list[dict[str, object]] = field(default_factory=list)

    async def handler(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        return "probe ok"

    def tool(self) -> Tool:
        return Tool(
            name=_PROBE,
            description="A harmless probe. Returns the text 'probe ok'.",
            input_schema={
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            },
            handler=self.handler,
        )


def _tradewind(tmp_path: Path) -> Tradewind:
    profile = Profile(
        backend="claude",
        auth=SubscriptionAuth(),
        models={"standard": ModelSpec(model=_MODEL)},
    )
    return Tradewind(
        TradewindConfig(
            profiles={"default": profile},
            default_profile="default",
            store=tmp_path / "sessions.db",
            customization_backup_dir=tmp_path / "backups",
        )
    )


def _workspace(tmp_path: Path, name: str = "ws") -> Path:
    workspace = tmp_path / name
    workspace.mkdir()
    return workspace


def _write_settings(workspace: Path, settings: dict[str, object]) -> Path:
    path = workspace / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings))
    return path


async def _create(
    tradewind: Tradewind,
    workspace: Path,
    *,
    tools: list[Tool] | None = None,
    broker: _RecordingBroker | None = None,
) -> Session:
    return await tradewind.create(
        str(uuid.uuid4()),
        SessionOptions(cwd=workspace, tools=tools or [], permission_broker=broker),
    )


async def _turn(
    session: Session, prompt: str, customizations: CustomizationDecision | None = None
) -> _Outcome:
    """One streamed turn. A failure is recorded, not raised, so a control
    run can inspect it."""
    events: list[Event] = []
    error: str | None = None
    try:
        # Closed at once, also on a cancel, so the engine process ends.
        async with contextlib.aclosing(
            session.stream(prompt, customizations=customizations)
        ) as stream:
            async for event in stream:
                events.append(event)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    result: TurnResult | None = None
    for event in events:
        if isinstance(event, TurnCompleted):
            result = event.result
            print(f"claude engine_version: {result.engine_version}")
        elif isinstance(event, TurnFailed):
            error = event.error
    return _Outcome(events=events, result=result, error=error)


def _decide(
    session: Session, action: Literal["delete", "proceed"], expected: list[Path]
) -> CustomizationDecision:
    scan = session.scan_customizations()
    found = sorted(f.path for f in scan.files)
    assert found == sorted(expected), f"scan found {found}, expected {expected}"
    return CustomizationDecision(action=action, fingerprint=scan.fingerprint)


def _redact(text: str, secret: str) -> str:
    """Never print the invalid key: drop the value and any masked form."""
    return re.sub(r"\S*\*{3,}\S*", "<masked>", text.replace(secret, "<redacted>"))


def _auth_failure(outcome: _Outcome) -> str | None:
    """The text that shows the turn failed on auth, or None."""
    texts: list[str] = []
    if outcome.error:
        texts.append(outcome.error)
    if outcome.result is not None and outcome.result.status != "completed":
        texts.append(outcome.result.final_text or "")
    for text in texts:
        if any(marker in text.lower() for marker in _AUTH_MARKERS):
            return text
    return None


def _no_auth_vars(_self: ClaudeBackend) -> frozenset[str]:
    return frozenset()


async def test_project_hook_runs_on_proceed_not_after_delete(tmp_path: Path) -> None:
    """A project `SessionStart` hook runs on a `proceed` turn (control), and
    not after a `delete` turn. Proves the flagged settings file is loaded
    and that `delete` keeps its hook from running."""
    with anyio.fail_after(_CAP_S):
        marker = tmp_path / "hook-ran"
        workspace = _workspace(tmp_path)
        hook = {"type": "command", "command": f"touch {shlex.quote(str(marker))}"}
        settings_path = _write_settings(workspace, {"hooks": {"SessionStart": [{"hooks": [hook]}]}})
        tradewind = _tradewind(tmp_path)
        expected = [Path(".claude/settings.json")]

        session = await _create(tradewind, workspace)
        control = await _turn(session, _PONG, _decide(session, "proceed", expected))
        if control.result is None:
            pytest.fail(f"inconclusive: the proceed turn did not complete\n{control.describe()}")
        if not marker.exists():
            pytest.fail(
                "inconclusive: the project SessionStart hook did not run on a proceed turn, "
                f"so this CLI does not show the effect\n{control.describe()}"
            )

        marker.unlink()
        session = await _create(tradewind, workspace)
        trap = await _turn(session, _PONG, _decide(session, "delete", expected))

        assert trap.result is not None, trap.describe()
        assert not settings_path.exists(), "delete left the settings file"
        backups = list((tmp_path / "backups").rglob("settings.json"))
        assert backups, "delete made no backup of the settings file"
        assert not marker.exists(), f"the hook ran after delete\n{trap.describe()}"


async def test_project_permission_allow_skips_can_use_tool(tmp_path: Path) -> None:
    """Without project settings, a caller MCP tool asks `can_use_tool`, so
    the broker is called (control). With a project `permissions.allow` rule
    for it and `proceed`, the tool runs and the broker is not called. Proves
    that the settings file must be flagged."""
    with anyio.fail_after(_CAP_S):
        tradewind = _tradewind(tmp_path)

        plain = _workspace(tmp_path, "plain")
        probe, broker = _Probe(), _RecordingBroker()
        session = await _create(tradewind, plain, tools=[probe.tool()], broker=broker)
        control = await _turn(session, _CALL_PROBE)
        detail = f"broker calls: {broker.calls}; tool calls: {probe.calls}\n{control.describe()}"
        if not probe.calls:
            pytest.fail(f"inconclusive: the model did not call the probe tool\n{detail}")
        if _PROBE not in broker.calls:
            pytest.fail(
                "inconclusive: the probe tool ran without a broker question even with no "
                f"project settings (a user-level allow rule can cause this)\n{detail}"
            )

        allowed = _workspace(tmp_path, "allowed")
        _write_settings(allowed, {"permissions": {"allow": [_PROBE_FULL_NAME]}})
        probe, broker = _Probe(), _RecordingBroker()
        session = await _create(tradewind, allowed, tools=[probe.tool()], broker=broker)
        decision = _decide(session, "proceed", [Path(".claude/settings.json")])
        trap = await _turn(session, _CALL_PROBE, decision)
        detail = f"broker calls: {broker.calls}; tool calls: {probe.calls}\n{trap.describe()}"
        if not probe.calls:
            pytest.fail(f"inconclusive: the model did not call the probe tool\n{detail}")

        assert _PROBE not in broker.calls, (
            "the project allow rule did not skip can_use_tool: the flag on "
            f".claude/settings.json may no longer be needed for this rule\n{detail}"
        )


async def test_env_scrub_keeps_subscription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With an invalid `ANTHROPIC_API_KEY` and no scrub, the turn fails on
    auth (control). With the scrub, the same environment gives a turn on
    the subscription. The key value is never printed."""
    with anyio.fail_after(_CAP_S):
        invalid_key = f"sk-ant-api03-tradewind-safety-invalid-{secrets.token_hex(16)}"
        monkeypatch.setenv("ANTHROPIC_API_KEY", invalid_key)
        workspace = _workspace(tmp_path)
        tradewind = _tradewind(tmp_path)

        with monkeypatch.context() as no_scrub:
            # Test-only: the adapter builds its options without the scrub.
            no_scrub.setattr(ClaudeBackend, "auth_env_vars", _no_auth_vars)
            session = await _create(tradewind, workspace)
            control = await _turn(session, _PONG)
        if _auth_failure(control) is None:
            pytest.fail(
                "inconclusive: without the scrub, an invalid ANTHROPIC_API_KEY did not make "
                "the turn fail on auth\n" + _redact(control.describe(), invalid_key)
            )

        session = await _create(tradewind, workspace)
        protected = await _turn(session, _PONG)

        if protected.result is None or not protected.result.final_text:
            pytest.fail(
                "the scrubbed turn did not complete on the subscription\n"
                + _redact(protected.describe(), invalid_key)
            )


async def test_caller_mcp_tool_reaches_broker(tmp_path: Path) -> None:
    """A caller MCP tool call reaches the broker through `can_use_tool`."""
    with anyio.fail_after(_CAP_S):
        workspace = _workspace(tmp_path)
        probe, broker = _Probe(), _RecordingBroker()
        session = await _create(
            _tradewind(tmp_path), workspace, tools=[probe.tool()], broker=broker
        )

        outcome = await _turn(session, _CALL_PROBE)

        detail = f"broker calls: {broker.calls}; tool calls: {probe.calls}\n{outcome.describe()}"
        if not probe.calls and not broker.calls:
            pytest.fail(f"inconclusive: the model did not call the probe tool\n{detail}")
        assert _PROBE in broker.calls, f"the probe tool ran without the broker\n{detail}"
        assert probe.calls, f"the broker allowed the call, but the tool did not run\n{detail}"
        assert outcome.result is not None, detail
