"""Live safety traps for the Claude engine (docs/components/06-workspace-safety.md §6).

On demand only: gated by `TRADEWIND_RUN_SAFETY=1`, marked `integration`,
never run by `check.sh` or CI. It uses the machine's Claude subscription
login and the SDK's bundled CLI. The owner starts it, time-boxed:

    TRADEWIND_RUN_SAFETY=1 timeout 1500 uv run pytest tests/safety -v -m integration

Add `-rP` to see the printed engine versions and evidence of passed tests.

Each test checks a fact that an engine upgrade can change. Each trap has a
control run that shows the effect without the protection. When the control
does not show the effect, the test fails with "inconclusive: <why>"; it
never passes without positive evidence.

Workspaces, markers and backups are in the test's temp folder. The tests
edit no config outside it. The engine itself still writes its own state
outside it, as on every Claude turn: per-project state in `~/.claude.json`
and session transcripts under `~/.claude/projects/`.
"""

from __future__ import annotations

import json
import os
import secrets
import shlex
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import anyio
import pytest

from tests.safety._live import auth_failure, redact, turn
from tradewind.adapters.claude_backend import _MCP_SERVER_NAME, ClaudeBackend
from tradewind.application.client import Session, Tradewind
from tradewind.application.config import NativeStoreConfig, TradewindConfig
from tradewind.domain.models import (
    CustomizationDecision,
    ModelSpec,
    Profile,
    SessionOptions,
    SubscriptionAuth,
    Tool,
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
# The Claude auth and provider variables other than ANTHROPIC_API_KEY
# (06 §4). The scrub control removes them, so it can never use a real
# credential from the owner's shell.
_OTHER_AUTH_VARS = (
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_GATEWAY",
)


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


def _profile() -> Profile:
    return Profile(
        backend="claude",
        auth=SubscriptionAuth(),
        models={"standard": ModelSpec(model=_MODEL)},
    )


def _print_engine_version() -> None:
    """Printed before any turn, so a failed test shows it too."""
    version = ClaudeBackend(_profile(), NativeStoreConfig()).engine_version()
    print(f"claude engine_version: {version}")


def _tradewind(tmp_path: Path) -> Tradewind:
    return Tradewind(
        TradewindConfig(
            profiles={"default": _profile()},
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


def _decide(
    session: Session, action: Literal["delete", "proceed"], expected: list[Path]
) -> CustomizationDecision:
    scan = session.scan_customizations()
    found = sorted(f.path for f in scan.files)
    assert found == sorted(expected), f"scan found {found}, expected {expected}"
    return CustomizationDecision(action=action, fingerprint=scan.fingerprint)


def _no_auth_vars(_self: ClaudeBackend) -> frozenset[str]:
    return frozenset()


async def test_project_hook_runs_on_proceed_not_after_delete(tmp_path: Path) -> None:
    """A project `SessionStart` hook runs on a `proceed` turn (control), and
    not after a `delete` turn. Proves the flagged settings file is loaded
    and that `delete` keeps its hook from running."""
    _print_engine_version()
    with anyio.fail_after(_CAP_S):
        marker = tmp_path / "hook-ran"
        workspace = _workspace(tmp_path)
        hook = {"type": "command", "command": f"touch {shlex.quote(str(marker))}"}
        settings_path = _write_settings(workspace, {"hooks": {"SessionStart": [{"hooks": [hook]}]}})
        tradewind = _tradewind(tmp_path)
        expected = [Path(".claude/settings.json")]

        session = await _create(tradewind, workspace)
        control = await turn(session, _PONG, _decide(session, "proceed", expected))
        if control.result is None:
            pytest.fail(f"inconclusive: the proceed turn did not complete\n{control.describe()}")
        if not marker.exists():
            pytest.fail(
                "inconclusive: the project SessionStart hook did not run on a proceed turn, "
                f"so this CLI does not show the effect\n{control.describe()}"
            )

        marker.unlink()
        session = await _create(tradewind, workspace)
        trap = await turn(session, _PONG, _decide(session, "delete", expected))

        assert trap.result is not None, trap.describe()
        assert not settings_path.exists(), "delete left the settings file"
        backups = list((tmp_path / "backups").rglob("settings.json"))
        assert backups, "delete made no backup of the settings file"
        assert not marker.exists(), f"the hook ran after delete\n{trap.describe()}"


async def test_project_allow_rule_does_not_bypass_can_use_tool(tmp_path: Path) -> None:
    """Engine-regression guard: a project `permissions.allow` rule for a
    caller MCP tool does not skip `can_use_tool`, so the broker is still
    asked.

    Live finding (CLI 2.1.258, 2026-11-15): with `.claude/settings.json` =
    `{"permissions": {"allow": ["mcp__tradewind__probe_tool"]}}` and
    `proceed`, the broker was still asked (broker calls: ['probe_tool']).
    The transcript's tool_use name was exactly `mcp__tradewind__probe_tool`,
    so the rule matched the tool. Today the `.claude/settings.json` flag is
    needed for its hooks, not for this permission rule.

    Pass (positive evidence): with the rule present, the broker got the
    probe tool, and the tool ran. Fail: the tool ran with no broker
    question; a future CLI then skips `can_use_tool` for project allow
    rules. "Inconclusive": the model did not call the tool."""
    _print_engine_version()
    with anyio.fail_after(_CAP_S):
        workspace = _workspace(tmp_path)
        _write_settings(workspace, {"permissions": {"allow": [_PROBE_FULL_NAME]}})
        probe, broker = _Probe(), _RecordingBroker()
        session = await _create(
            _tradewind(tmp_path), workspace, tools=[probe.tool()], broker=broker
        )
        decision = _decide(session, "proceed", [Path(".claude/settings.json")])

        outcome = await turn(session, _CALL_PROBE, decision)

        detail = f"broker calls: {broker.calls}; tool calls: {probe.calls}\n{outcome.describe()}"
        if not probe.calls and _PROBE not in broker.calls:
            pytest.fail(f"inconclusive: the model did not call the probe tool\n{detail}")
        assert _PROBE in broker.calls, (
            "a project permissions.allow rule now skips can_use_tool: the probe tool ran "
            "with no broker question. The .claude/settings.json flag is then needed for "
            f"permission rules too, not only for hooks (06 §1)\n{detail}"
        )
        assert probe.calls, f"the broker allowed the call, but the tool did not run\n{detail}"


async def test_env_scrub_keeps_subscription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With an invalid `ANTHROPIC_API_KEY` and no scrub, the turn fails on
    auth (control). With the scrub, the same environment gives a completed
    turn on the subscription. The key value is never printed."""
    _print_engine_version()
    with anyio.fail_after(_CAP_S):
        invalid_key = f"sk-ant-api03-tradewind-safety-invalid-{secrets.token_hex(16)}"
        for name in _OTHER_AUTH_VARS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", invalid_key)
        workspace = _workspace(tmp_path)
        tradewind = _tradewind(tmp_path)

        with monkeypatch.context() as no_scrub:
            # Test-only: the adapter builds its options without the scrub.
            no_scrub.setattr(ClaudeBackend, "auth_env_vars", _no_auth_vars)
            session = await _create(tradewind, workspace)
            control = await turn(session, _PONG)
        evidence = auth_failure(control)
        if evidence is None:
            pytest.fail(
                "inconclusive: without the scrub, an invalid ANTHROPIC_API_KEY did not make "
                "the turn fail on auth\n" + redact(control.describe(), [invalid_key])
            )

        session = await _create(tradewind, workspace)
        protected = await turn(session, _PONG)

        status = protected.result.status if protected.result is not None else None
        if status != "completed" or auth_failure(protected) is not None:
            pytest.fail(
                "the scrubbed turn did not complete on the subscription\n"
                + redact(protected.describe(), [invalid_key])
            )
        print("control auth failure (evidence): " + redact(evidence, [invalid_key])[:400])


async def test_caller_mcp_tool_reaches_broker(tmp_path: Path) -> None:
    """A caller MCP tool call reaches the broker through `can_use_tool`."""
    _print_engine_version()
    with anyio.fail_after(_CAP_S):
        workspace = _workspace(tmp_path)
        probe, broker = _Probe(), _RecordingBroker()
        session = await _create(
            _tradewind(tmp_path), workspace, tools=[probe.tool()], broker=broker
        )

        outcome = await turn(session, _CALL_PROBE)

        detail = f"broker calls: {broker.calls}; tool calls: {probe.calls}\n{outcome.describe()}"
        if not probe.calls and not broker.calls:
            pytest.fail(f"inconclusive: the model did not call the probe tool\n{detail}")
        assert _PROBE in broker.calls, f"the probe tool ran without the broker\n{detail}"
        assert probe.calls, f"the broker allowed the call, but the tool did not run\n{detail}"
        assert outcome.result is not None, detail
