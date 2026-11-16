"""The auth environment scrub and the engine version per turn
(docs/components/06-workspace-safety.md §4, §5).

A subscription turn must never be billed to an API key or another
provider (anti-goal), so the engine's child environment blanks every
variable that selects one; an API-key profile keeps them (D6). Each turn
records the engine version that ran it, logged at turn start so a failed
turn has it too.
"""

from __future__ import annotations

import importlib.metadata
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import claude_agent_sdk._cli_version
import pytest
from fake_governed_backend import FakeGovernedBackend, Say
from pydantic import SecretStr

from tradewind import Tradewind
from tradewind.adapters.claude_backend import ClaudeBackend
from tradewind.adapters.codex_backend import CodexBackend
from tradewind.adapters.cursor_backend import CursorBackend
from tradewind.adapters.langchain_backend import LangchainBackend
from tradewind.application.config import NativeStoreConfig, TradewindConfig
from tradewind.application.engine_env import engine_env
from tradewind.application.ports import Backend, TurnContext
from tradewind.domain.errors import Unsupported
from tradewind.domain.events import Event
from tradewind.domain.models import (
    ApiKeyAuth,
    BackendName,
    ModelSpec,
    Profile,
    SessionOptions,
    SubscriptionAuth,
)

_ID = "77777777-7777-7777-7777-777777777777"
_SECRET = "sk-secret-value-never-logged"

# Exactly the lists in 06 §4 (explicit names, no prefix rule).
_CLAUDE_VARS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_ANTHROPIC_AWS",
        "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
        "CLAUDE_CODE_USE_MANTLE",
        "CLAUDE_CODE_USE_GATEWAY",
    }
)
_CODEX_VARS = frozenset({"OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"})


def _profile(backend: BackendName, *, subscription: bool = True) -> Profile:
    return Profile(
        backend=backend,
        auth=SubscriptionAuth() if subscription else ApiKeyAuth(api_key=SecretStr(_SECRET)),
        models={"standard": ModelSpec(model="model-standard")},
    )


@pytest.mark.parametrize(
    ("backend_cls", "backend_name", "declared"),
    [(ClaudeBackend, "claude", _CLAUDE_VARS), (CodexBackend, "codex", _CODEX_VARS)],
)
def test_subscription_profile_removes_listed_vars(
    backend_cls: type[Backend], backend_name: BackendName, declared: frozenset[str]
) -> None:
    backend = backend_cls(_profile(backend_name), NativeStoreConfig())
    assert backend.auth_env_vars() == declared
    present = sorted(declared)[:2]
    base = {
        **dict.fromkeys(present, _SECRET),
        "PATH": "/usr/bin",
        "CLAUDE_CODE_USE_POWERSHELL_TOOL": "1",  # a feature switch, not auth
    }

    env = engine_env(backend.profile.auth, backend.auth_env_vars(), base)

    # Only what the caller has set is removed; the child gets "" for it,
    # which both engines read as unset (task-4 report, Step 0).
    assert env.removed == tuple(present)
    assert env.overrides == dict.fromkeys(present, "")


def test_api_key_profile_unchanged() -> None:
    base = {"ANTHROPIC_API_KEY": _SECRET, "PATH": "/usr/bin"}

    env = engine_env(ApiKeyAuth(api_key=SecretStr(_SECRET)), _CLAUDE_VARS, base)

    assert env.removed == ()
    assert env.overrides == {}


def test_values_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="tradewind.application.engine_env")
    base = {"ANTHROPIC_API_KEY": _SECRET, "ANTHROPIC_AUTH_TOKEN": _SECRET}

    engine_env(SubscriptionAuth(), _CLAUDE_VARS, base)

    # Each removal is logged at DEBUG, by name.
    debug = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert sorted(r.__dict__["env_var"] for r in debug) == [
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
    ]
    for record in caplog.records:
        assert _SECRET not in record.getMessage()
        assert _SECRET not in repr(record.__dict__)


def test_os_environ_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", _SECRET)
    before = dict(os.environ)

    env = engine_env(SubscriptionAuth(), _CODEX_VARS, os.environ)

    assert env.removed == ("OPENAI_API_KEY",)
    assert dict(os.environ) == before


class _VersionedFake(FakeGovernedBackend):
    """Reports an engine version that changes between calls, so a cached
    value would show."""

    versions: list[str]

    def engine_version(self) -> str | None:
        return self.versions.pop(0)


class _VersionedFailsBeforeStart(_VersionedFake):
    async def run(self, ctx: TurnContext) -> AsyncIterator[Event]:  # noqa: ARG002
        raise Unsupported("fails before TurnStarted")
        yield  # an async generator, like every real backend


class _VersionedStopsInPrepare(_VersionedFake):
    """The caller's `stop()` lands during `prepare_turn`, so `run()` is never
    called and the runner synthesizes the interrupted result (05 §4)."""

    stop: Callable[[], Awaitable[None]]

    async def prepare_turn(self, ctx: TurnContext) -> None:
        await super().prepare_turn(ctx)
        await self.stop()


def _tradewind(tmp_path: Path, backend_cls: type[_VersionedFake]) -> Tradewind:
    tmp_path.mkdir(exist_ok=True)
    profile = _profile("codex", subscription=False)
    tw = Tradewind(
        TradewindConfig(
            profiles={"default": profile}, default_profile="default", store=tmp_path / "s.db"
        )
    )
    fake = backend_cls(profile, NativeStoreConfig(), [Say("done")])
    fake.versions = ["1.0.0", "1.0.1"]
    tw._backends["default"] = fake
    return tw


def _version_logs(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [
        {k: r.__dict__[k] for k in ("turn_id", "session_id", "backend", "engine_version")}
        for r in caplog.records
        if r.name == "tradewind.application.turn_runner"
        and r.levelno == logging.INFO
        and "engine_version" in r.__dict__
    ]


async def test_version_on_turn_result_and_logged_at_turn_start(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="tradewind.application.turn_runner")
    tw = _tradewind(tmp_path, _VersionedFake)
    session = await tw.create(_ID, SessionOptions())

    first = await session.run("go")
    second = await session.run("again")

    # Read on each turn (no cache): an engine upgraded between turns shows.
    assert (first.engine_version, second.engine_version) == ("1.0.0", "1.0.1")
    assert _version_logs(caplog) == [
        {
            "turn_id": first.turn_id,
            "session_id": _ID,
            "backend": "codex",
            "engine_version": "1.0.0",
        },
        {
            "turn_id": second.turn_id,
            "session_id": _ID,
            "backend": "codex",
            "engine_version": "1.0.1",
        },
    ]

    # A turn that fails before `TurnStarted` still logged its version.
    caplog.clear()
    failing = _tradewind(tmp_path / "failing", _VersionedFailsBeforeStart)
    failing_session = await failing.create(_ID, SessionOptions())
    with pytest.raises(Unsupported):
        await failing_session.run("go")
    [log] = _version_logs(caplog)
    assert log["engine_version"] == "1.0.0"


async def test_version_on_synthesized_interrupted_result(tmp_path: Path) -> None:
    tw = _tradewind(tmp_path, _VersionedStopsInPrepare)
    fake = tw._backends["default"]
    assert isinstance(fake, _VersionedStopsInPrepare)
    session = await tw.create(_ID, SessionOptions())
    fake.stop = session.stop

    result = await session.run("go")

    # The runner's own result, not the backend's, still records the engine.
    assert "run" not in fake.calls
    assert (result.status, result.end_reason) == ("interrupted", "interrupted")
    assert result.engine_version == "1.0.0"


def test_claude_and_codex_report_their_bundled_engine_versions() -> None:
    claude = ClaudeBackend(_profile("claude"), NativeStoreConfig())
    codex = CodexBackend(_profile("codex"), NativeStoreConfig())

    assert claude.engine_version() == claude_agent_sdk._cli_version.__cli_version__
    assert codex.engine_version() == importlib.metadata.version("openai-codex-cli-bin")


def test_langchain_cursor_have_no_vars_and_no_version() -> None:
    for backend in (
        LangchainBackend(_profile("langchain"), NativeStoreConfig()),
        CursorBackend(_profile("cursor"), NativeStoreConfig()),
    ):
        # D8: no engine parts. An API key is LangChain's intended auth.
        assert backend.auth_env_vars() == frozenset()
        assert backend.engine_version() is None
