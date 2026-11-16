"""The engine child's real environment (docs/components/06-workspace-safety.md
§4, Acceptance): the adapter's own option building, spawned by the SDK's
own launch code, against a fake engine binary that writes its environment
to a file and exits.

An integration test under GUIDELINES §10 (it spawns a process), but not
gated: no network, no engine CLI, no credentials. Neither SDK can delete a
variable from the child environment, so the adapters pass "" for each
auth variable the caller has set; both engines read "" as unset (task-4
report, Step 0). This test proves the value reaches the child as "" on a
subscription profile, and is kept on an API-key profile (D6).
"""

from __future__ import annotations

import dataclasses
import json
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
from openai_codex.client import CodexClient
from pydantic import SecretStr

from tradewind.adapters.claude_backend import ClaudeBackend
from tradewind.adapters.codex_backend import CodexBackend
from tradewind.application.config import NativeStoreConfig
from tradewind.application.ports import TurnContext
from tradewind.application.tool_host import ToolHost
from tradewind.domain.models import (
    ApiKeyAuth,
    BackendName,
    ModelSpec,
    Profile,
    SessionRow,
    SubscriptionAuth,
    Verdict,
)

pytestmark = pytest.mark.integration

_SECRET = "sk-fake-not-a-real-key"
_UNRELATED = "TRADEWIND_TEST_UNRELATED"

_FAKE_ENGINE = """\
#!{python}
import json, os, sys
if sys.argv[1:] == ["-v"]:  # the Claude SDK's version probe
    print("2.1.258")
    sys.exit(0)
out = os.environ["TRADEWIND_TEST_ENV_OUT"]
with open(out + ".tmp", "w") as f:
    json.dump(dict(os.environ), f)
os.replace(out + ".tmp", out)
"""


def _fake_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A fake engine binary, and the file it writes its environment to."""
    binary = tmp_path / "fake-engine"
    binary.write_text(_FAKE_ENGINE.format(python=sys.executable))
    binary.chmod(0o700)
    out = tmp_path / "child-env.json"
    monkeypatch.setenv("TRADEWIND_TEST_ENV_OUT", str(out))
    monkeypatch.setenv(_UNRELATED, "kept")
    return binary, out


def _child_env(out: Path) -> dict[str, str]:
    """The fake engine's environment, once it has written it (it writes
    the file whole, by rename, then exits)."""
    deadline = time.monotonic() + 10
    while not out.exists():
        assert time.monotonic() < deadline, "the fake engine never wrote its environment"
        time.sleep(0.01)
    return cast(dict[str, str], json.loads(out.read_text()))


def _profile(backend: BackendName, *, subscription: bool) -> Profile:
    return Profile(
        backend=backend,
        auth=SubscriptionAuth() if subscription else ApiKeyAuth(api_key=SecretStr(_SECRET)),
        models={"standard": ModelSpec(model="model-standard")},
    )


class _NoBroker:
    async def decide(self, tool_name: str, _tool_input: dict[str, object]) -> Verdict:
        raise AssertionError(f"broker.decide should not be called (got {tool_name!r})")


async def _no_history() -> list[Any]:
    return []


def _claude_ctx() -> TurnContext:
    return TurnContext(
        session=SessionRow(
            session_id="s1", backend="claude", profile="default", options_snapshot={}
        ),
        turn_id="turn-1",
        prompt="hello",
        model_spec=ModelSpec(model="model-standard"),
        system_prompt=None,
        output_schema=None,
        tools=ToolHost([], [], lambda ref: ref),
        broker=cast(Any, _NoBroker()),
        load_history=_no_history,
    )


@pytest.mark.parametrize("subscription", [True, False], ids=["subscription", "api_key"])
async def test_child_env_has_no_auth_vars_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, subscription: bool
) -> None:
    binary, out = _fake_engine(tmp_path, monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", _SECRET)
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    backend = ClaudeBackend(_profile("claude", subscription=subscription), NativeStoreConfig())
    options = backend._build_options(_claude_ctx(), [], [False], [None])
    transport = SubprocessCLITransport(
        prompt="", options=dataclasses.replace(options, cli_path=binary)
    )

    await transport.connect()
    try:
        child = _child_env(out)
    finally:
        await transport.close()

    if subscription:
        assert child["ANTHROPIC_API_KEY"] == ""
        assert child["CLAUDE_CODE_USE_BEDROCK"] == ""
    else:
        assert child["ANTHROPIC_API_KEY"] == _SECRET
        assert child["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert child["PATH"]
    assert child[_UNRELATED] == "kept"


@pytest.mark.parametrize("subscription", [True, False], ids=["subscription", "api_key"])
def test_child_env_has_no_auth_vars_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, subscription: bool
) -> None:
    binary, out = _fake_engine(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", _SECRET)
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", _SECRET)
    # The CODEX_HOME isolation (DR-3) still applies alongside the scrub.
    codex_home = tmp_path / "codex-home"
    backend = CodexBackend(
        _profile("codex", subscription=subscription),
        NativeStoreConfig(isolation_mode=True, codex_home=codex_home),
    )
    config = dataclasses.replace(backend._engine_config(), codex_bin=str(binary))
    client = CodexClient(config=config)

    client.start()
    try:
        child = _child_env(out)
    finally:
        client.close()

    if subscription:
        assert child["OPENAI_API_KEY"] == ""
        assert child["CODEX_ACCESS_TOKEN"] == ""
    else:
        assert child["OPENAI_API_KEY"] == _SECRET
        assert child["CODEX_ACCESS_TOKEN"] == _SECRET
    assert child["CODEX_HOME"] == str(codex_home)
    assert child["PATH"]
    assert child[_UNRELATED] == "kept"
