"""Live safety traps for the Codex engine (docs/components/06-workspace-safety.md §6).

On demand only: gated by `TRADEWIND_RUN_SAFETY=1`, marked `integration`,
never run by `check.sh` or CI. It uses the machine's ChatGPT subscription
login and a real `codex app-server`. The owner starts it, time-boxed:

    TRADEWIND_RUN_SAFETY=1 timeout 1500 uv run pytest tests/safety -v -m integration

Add `-rP` to see the printed engine versions of passed tests.

Each test checks a fact that an engine upgrade can change. A test that
cannot show its effect fails with "inconclusive: <why>"; it never passes
without positive evidence.

The two broker tests came from `tests/integration/test_codex_governance_live.py`
(PR #17, 05 §7). They prove what the unit tests cannot: a
`fileChange/requestApproval` finds its item's changes from `item/started`
(same item id, start before request), so the broker gets a `file_write`
action with the changed path, not a `missing_critical_field` floor deny.
The sandbox is `read-only`, so every write must ask first.

Workspaces, markers and project config are in the test's temp folder only.
The tests never edit `~/.codex/config.toml` and never change Codex trust.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import anyio
import pytest

from tradewind.adapters.codex_backend import CodexBackend
from tradewind.application.client import Session, Tradewind
from tradewind.application.config import TradewindConfig
from tradewind.domain.events import (
    Event,
    ItemCompleted,
    PermissionRequested,
    TurnCompleted,
    TurnFailed,
)
from tradewind.domain.models import (
    CustomizationDecision,
    ModelSpec,
    Profile,
    SessionOptions,
    SubscriptionAuth,
    TurnResult,
)

pytestmark = pytest.mark.integration

if not os.environ.get("TRADEWIND_RUN_SAFETY"):
    pytest.skip(
        "set TRADEWIND_RUN_SAFETY=1 to run the live safety suite against a real "
        "codex app-server (ChatGPT subscription login, real model calls)",
        allow_module_level=True,
    )

_MODEL = "gpt-5.6-sol"
_CAP_S = 180
# A control turn that hangs is not evidence; it ends early as "inconclusive".
_CONTROL_CAP_S = 90
_PONG = "Reply with exactly one word: pong"
_CODEWORD = "KUMQUAT"
_AUTH_VARS = ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN")
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


@dataclass
class _RecordingBroker:
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    async def decide(self, tool_name: str, tool_input: dict[str, object]) -> str:
        self.calls.append((tool_name, tool_input))
        return "allow"

    def actions(self, kind: str) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        for _name, args in self.calls:
            shared = args.get("tradewind")
            if isinstance(shared, dict) and shared.get("kind") == kind:
                found.append(shared)
        return found


def _tradewind(tmp_path: Path, *, sandbox: str | None = None) -> Tradewind:
    profile = Profile(
        backend="codex",
        auth=SubscriptionAuth(),
        models={"standard": ModelSpec(model=_MODEL)},
        backend_options={"sandbox": sandbox} if sandbox is not None else {},
    )
    return Tradewind(
        TradewindConfig(
            profiles={"default": profile},
            default_profile="default",
            store=tmp_path / "sessions.db",
        )
    )


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return workspace


async def _create(
    tradewind: Tradewind, workspace: Path, broker: _RecordingBroker | None = None
) -> Session:
    return await tradewind.create(
        str(uuid.uuid4()), SessionOptions(cwd=workspace, permission_broker=broker)
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
            print(f"codex engine_version: {result.engine_version}")
        elif isinstance(event, TurnFailed):
            error = event.error
    return _Outcome(events=events, result=result, error=error)


def _describe(outcome: _Outcome, broker: _RecordingBroker | None = None) -> str:
    lines = [f"broker calls: {broker.calls!r}"] if broker is not None else []
    lines += [f"error: {outcome.error!r}"]
    lines += [
        repr(e)[:400]
        for e in outcome.events
        if isinstance(e, PermissionRequested | TurnFailed | TurnCompleted | ItemCompleted)
    ]
    return "\n".join(lines)


def _redact(text: str, values: list[str]) -> str:
    """Never print the invalid values: drop each one and any masked form."""
    for value in values:
        text = text.replace(value, "<redacted>")
    return re.sub(r"\S*\*{3,}\S*", "<masked>", text)


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


def _no_auth_vars(_self: CodexBackend) -> frozenset[str]:
    return frozenset()


async def test_shell_reaches_broker(tmp_path: Path) -> None:
    """A shell command reaches the broker as a `shell` action."""
    with anyio.fail_after(_CAP_S):
        workspace = _workspace(tmp_path)
        broker = _RecordingBroker()
        session = await _create(_tradewind(tmp_path, sandbox="read-only"), workspace, broker)

        outcome = await _turn(
            session,
            "Run exactly this shell command once: touch marker.txt -- then reply with "
            "the word done. Do not run any other command.",
        )

        detail = _describe(outcome, broker)
        shells = broker.actions("shell")
        if not shells and not (workspace / "marker.txt").exists():
            pytest.fail(f"inconclusive: the model ran no command\n{detail}")
        assert shells, f"marker.txt exists, but no shell action reached the broker\n{detail}"
        assert any("touch" in str(s.get("fields")) for s in shells), detail
        assert outcome.result is not None, detail
        assert (workspace / "marker.txt").exists(), detail


async def test_file_write_reaches_broker_with_changes(tmp_path: Path) -> None:
    """A file write reaches the broker as a `file_write` action with its
    changes, not a `missing_critical_field` deny."""
    with anyio.fail_after(_CAP_S):
        workspace = _workspace(tmp_path)
        broker = _RecordingBroker()
        session = await _create(_tradewind(tmp_path, sandbox="read-only"), workspace, broker)

        outcome = await _turn(
            session,
            "Create a new file named hello.txt in the current directory whose whole "
            "content is the single line: hi. Use your apply_patch file-editing tool, "
            "not a shell command. Then reply with the word done.",
        )

        detail = _describe(outcome, broker)
        writes = broker.actions("file_write")
        if not writes and not (workspace / "hello.txt").exists():
            pytest.fail(f"inconclusive: the model wrote no file\n{detail}")
        assert writes, f"hello.txt exists, but no file_write reached the broker\n{detail}"
        assert any("hello.txt" in str(w.get("fields")) for w in writes), detail
        denied = [
            e for e in outcome.events if isinstance(e, PermissionRequested) and e.verdict == "deny"
        ]
        assert denied == [], detail
        assert outcome.result is not None, detail
        assert (workspace / "hello.txt").read_text().strip() == "hi"


async def test_env_scrub_keeps_subscription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With invalid Codex auth variables and no scrub, the turn fails on
    auth (control). With the scrub, the same environment gives a turn on
    the subscription. The values are never printed.

    Codex 0.147.0 (`login/src/auth/manager.rs` `load_auth`) reads
    `CODEX_ACCESS_TOKEN` before the stored ChatGPT login, so that variable
    is the one expected to break the control."""
    with anyio.fail_after(_CAP_S):
        invalid = {name: f"tradewind-safety-invalid-{secrets.token_hex(16)}" for name in _AUTH_VARS}
        for name, value in invalid.items():
            monkeypatch.setenv(name, value)
        values = list(invalid.values())
        workspace = _workspace(tmp_path)
        tradewind = _tradewind(tmp_path)

        control: _Outcome | None = None
        with monkeypatch.context() as no_scrub:
            # Test-only: the adapter builds its config without the scrub.
            no_scrub.setattr(CodexBackend, "auth_env_vars", _no_auth_vars)
            session = await _create(tradewind, workspace)
            with anyio.move_on_after(_CONTROL_CAP_S):
                control = await _turn(session, _PONG)
        if control is None:
            pytest.fail(f"inconclusive: the control turn did not end in {_CONTROL_CAP_S} s")
        if _auth_failure(control) is None:
            pytest.fail(
                "inconclusive: without the scrub, invalid Codex auth variables did not make "
                "the turn fail on auth\n" + _redact(_describe(control), values)
            )

        session = await _create(tradewind, workspace)
        protected = await _turn(session, _PONG)

        if protected.result is None or not protected.result.final_text:
            pytest.fail(
                "the scrubbed turn did not complete on the subscription\n"
                + _redact(_describe(protected), values)
            )


async def test_project_config_keys_honored(tmp_path: Path) -> None:
    """Whether a project `.codex/config.toml` key that runs code is honored.
    This decides the Codex pattern in 06 §1.

    The file sets two keys:
    - `developer_instructions` (harmless): the reply ends with a codeword
      when the project layer is loaded. This is the control.
    - `mcp_servers` (runs code, not on the 0.147.0 project-local denylist
      in `config/src/loader/mod.rs`): its command touches a marker.

    Codex loads the project layer only when the folder is trusted. A temp
    folder is trusted only if the owner's own trust settings already cover
    it; the test never changes them, so an untrusted folder is
    "inconclusive"."""
    with anyio.fail_after(_CAP_S):
        workspace = _workspace(tmp_path)
        marker = tmp_path / "project-mcp-server-started"
        config_path = workspace / ".codex" / "config.toml"
        config_path.parent.mkdir()
        touch = shutil.which("touch") or "touch"
        config_path.write_text(
            f'developer_instructions = "End every reply with the word {_CODEWORD}."\n'
            "\n"
            "[mcp_servers.tradewind_safety_trap]\n"
            f"command = {json.dumps(touch)}\n"
            f"args = [{json.dumps(str(marker))}]\n"
        )
        session = await _create(_tradewind(tmp_path), workspace)
        scan = session.scan_customizations()
        assert [f.path for f in scan.files] == [Path(".codex/config.toml")], scan
        decision = CustomizationDecision(action="proceed", fingerprint=scan.fingerprint)

        outcome = await _turn(session, "Reply with one short sentence that says hello.", decision)

        detail = _describe(outcome)
        if marker.exists():
            return  # a project-local key that runs code is honored: the flag is needed
        reply = (outcome.result.final_text or "") if outcome.result is not None else ""
        if _CODEWORD.lower() not in reply.lower():
            pytest.fail(
                "inconclusive: no sign that the project config layer loaded; the temp folder "
                "is probably not trusted by Codex (the test never changes trust)\n" + detail
            )
        pytest.fail(
            "the project config layer loaded (codeword seen), but its mcp_servers entry did "
            "not start: re-check which project-local keys this Codex honors (06 §1)\n" + detail
        )
