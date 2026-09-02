"""Deferred integration tests for `LangchainBackend` against real model
APIs (task-8 brief; plan note "Available test credentials 2026-09-02").

No Anthropic API key and no Groq key exist yet, so every test here is
skip-by-default and none has been run against a real API — the fake-model
unit tests in `tests/unit/test_langchain_adapter.py` are this task's actual
gate. When a key is later supplied:

- `ANTHROPIC_API_KEY` set: exercises the default `chat_model_factory`
  (`ChatAnthropic`) end-to-end.
- `GROQ_API_KEY` set: exercises the `chat_model_factory` injection seam
  with a real `ChatGroq(model="openai/gpt-oss-120b")` — free-tier,
  per the plan note. `langchain-groq` is intentionally NOT a dependency
  yet (deferred alongside the key, same plan note); this test imports it
  lazily inside its own body so its absence only matters once the key
  arrives and this test actually runs.
"""

from __future__ import annotations

import os
import uuid

import pytest

from tradewind.adapters.langchain_backend import LangchainBackend
from tradewind.application.config import NativeStoreConfig
from tradewind.application.ports import TurnContext
from tradewind.application.tool_host import ToolHost
from tradewind.domain.events import TurnCompleted
from tradewind.domain.models import ApiKeyAuth, ModelSpec, Profile, SessionRow, Verdict

pytestmark = pytest.mark.integration


class _AllowAllBroker:
    async def decide(self, _tool_name: str, _tool_input: dict[str, object]) -> Verdict:
        return "allow"


def _session_row() -> SessionRow:
    return SessionRow(
        session_id=str(uuid.uuid4()),
        backend="langchain",
        profile="default",
        options_snapshot={},
    )


def _ctx(model_spec: ModelSpec, *, prompt: str) -> TurnContext:
    return TurnContext(
        session=_session_row(),
        turn_id=str(uuid.uuid4()),
        prompt=prompt,
        model_spec=model_spec,
        system_prompt=None,
        output_schema=None,
        tools=ToolHost([], [], lambda ref: ref),
        broker=_AllowAllBroker(),
        load_history=list,
    )


@pytest.mark.skipif(
    "ANTHROPIC_API_KEY" not in os.environ,
    reason="requires a real ANTHROPIC_API_KEY (deferred, none available yet)",
)
async def test_run_against_real_anthropic_api_completes_with_final_text() -> None:
    model_spec = ModelSpec(model="claude-3-5-haiku-20241022")
    profile = Profile(
        backend="langchain",
        auth=ApiKeyAuth(api_key=os.environ["ANTHROPIC_API_KEY"]),
        models={"standard": model_spec},
    )
    backend = LangchainBackend(profile, NativeStoreConfig())
    ctx = _ctx(model_spec, prompt="Reply with exactly one word: pong")

    events = [event async for event in backend.run(ctx)]

    assert isinstance(events[-1], TurnCompleted)
    assert events[-1].result.status == "completed"
    assert events[-1].result.final_text


@pytest.mark.skipif(
    "GROQ_API_KEY" not in os.environ,
    reason="requires a real GROQ_API_KEY and langchain-groq, both deferred "
    "(plan note: free-tier key pending; langchain-groq not added yet)",
)
async def test_run_against_real_groq_api_via_injected_chat_model() -> None:
    from langchain_groq import ChatGroq  # deferred dependency; see module docstring

    model_spec = ModelSpec(model="openai/gpt-oss-120b")
    profile = Profile(
        backend="langchain",
        # ChatGroq (injected below) reads GROQ_API_KEY itself; this profile's
        # auth is unused since `chat_model_factory` bypasses the default
        # ChatAnthropic factory entirely, but `Profile.auth` is required.
        auth=ApiKeyAuth(api_key="unused"),
        models={"standard": model_spec},
    )
    backend = LangchainBackend(
        profile,
        NativeStoreConfig(),
        chat_model_factory=lambda spec: ChatGroq(model=spec.model),
    )
    ctx = _ctx(model_spec, prompt="Reply with exactly one word: pong")

    events = [event async for event in backend.run(ctx)]

    assert isinstance(events[-1], TurnCompleted)
    assert events[-1].result.status == "completed"
    assert events[-1].result.final_text
