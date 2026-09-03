"""Runs the conformance matrix (`tests/conformance/matrix.py`) against
`LangchainBackend`, driven through the real `Tradewind`/`Session`/
`TurnRunner` stack -- unlike `tests/unit/test_langchain_adapter.py`, which
drives the adapter directly (task-9 brief).

The fake chat models below reuse task-8's patterns (`FakeMessagesListChat
Model` + a no-op `bind_tools`, a blocking `_agenerate` for the interrupt
scenario) but are queue-driven: each `LangchainHarness.script_*` call
enqueues one fake model, and the `chat_model_factory` given to
`LangchainBackend` pops the next one per turn. The backend itself is
pre-seeded into `Tradewind._backends` (bypassing the real
`ChatAnthropic`-building registry factory `tradewind/__init__.py`
registers) -- exactly the "adapters are built lazily per profile, cached
on the instance" cache the client wiring provides (component spec 01),
just pre-populated here instead of left to build itself.

Also carries the task-9 Step-3 embedding test: a plain-dict
`TradewindConfig`, `ensure` -> `run` -> `history`, asserting Tradewind
touched no path but the sqlite store's.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.messages.tool import tool_call as make_tool_call
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from pydantic import ConfigDict

from tests.conformance import matrix
from tradewind.adapters.langchain_backend import LangchainBackend
from tradewind.application.client import Tradewind
from tradewind.application.config import NativeStoreConfig, TradewindConfig
from tradewind.domain.models import ApiKeyAuth, Capabilities, ModelSpec, Profile, SessionOptions

# --- fake chat models (reuse of test_langchain_adapter.py's patterns, made
# queue-driven since a conformance scenario may run several turns) ---


class _ScriptedChatModel(FakeMessagesListChatModel):
    """`FakeMessagesListChatModel` plus a no-op `bind_tools` (the base
    class's default raises `NotImplementedError`)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def bind_tools(
        self, _tools: Sequence[object], *, _tool_choice: object = None, **_kwargs: object
    ) -> Runnable[Any, AIMessage]:
        return self


class _BlockingChatModel(BaseChatModel):
    """Blocks forever on the first request so a concurrent `stop()` can be
    delivered deterministically: `started` is set right before the block,
    so the scenario awaits it before interrupting (no sleep, GUIDELINES
    §10)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    started: anyio.Event

    def bind_tools(
        self, _tools: Sequence[object], *, _tool_choice: object = None, **_kwargs: object
    ) -> Runnable[Any, AIMessage]:
        return self

    def _generate(self, messages: list[BaseMessage], **kwargs: object) -> ChatResult:
        raise NotImplementedError("only the async path is exercised here")

    async def _agenerate(self, _messages: list[BaseMessage], **_kwargs: object) -> ChatResult:
        self.started.set()
        await anyio.sleep_forever()
        raise AssertionError("unreachable: interrupt should unwind before this returns")

    @property
    def _llm_type(self) -> str:
        return "blocking-fake"


class _EchoSystemPromptModel(BaseChatModel):
    """Answers with the `SystemMessage` content it was sent (or a fixed
    sentinel when there wasn't one), so `system_prompt_respected` can
    assert the prompt actually reached the backend's request."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def bind_tools(
        self, _tools: Sequence[object], *, _tool_choice: object = None, **_kwargs: object
    ) -> Runnable[Any, AIMessage]:
        return self

    def _generate(self, messages: list[BaseMessage], **kwargs: object) -> ChatResult:
        raise NotImplementedError("only the async path is exercised here")

    async def _agenerate(self, messages: list[BaseMessage], **_kwargs: object) -> ChatResult:
        system_texts = [str(m.content) for m in messages if isinstance(m, SystemMessage)]
        text = system_texts[0] if system_texts else "<no system prompt>"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

    @property
    def _llm_type(self) -> str:
        return "echo-system-prompt-fake"


# --- harness ---


@dataclass
class LangchainHarness:
    """`matrix.ConformanceHarness` for `LangchainBackend`."""

    tradewind: Tradewind
    capabilities: Capabilities
    _queue: list[BaseChatModel] = field(default_factory=list)

    def script_text_response(self, text: str) -> None:
        self._queue.append(_ScriptedChatModel(responses=[AIMessage(content=text)]))

    def script_tool_calls_then_text(
        self, tool_calls: list[matrix.ScriptedToolCall], final_text: str
    ) -> None:
        first_response = AIMessage(
            content="",
            tool_calls=[make_tool_call(name=c.name, args=c.args, id=c.id) for c in tool_calls],
        )
        self._queue.append(
            _ScriptedChatModel(responses=[first_response, AIMessage(content=final_text)])
        )

    def script_blocking(self) -> anyio.Event:
        started = anyio.Event()
        self._queue.append(_BlockingChatModel(started=started))
        return started

    def script_echo_system_prompt(self) -> None:
        self._queue.append(_EchoSystemPromptModel())


def _profile() -> Profile:
    return Profile(
        backend="langchain",
        auth=ApiKeyAuth(api_key=cast(Any, "sk-test")),
        models={"standard": ModelSpec(model="claude-sonnet-4-5")},
    )


def _wire_harness(
    tw: Tradewind, profile: Profile, profile_name: str = "default"
) -> LangchainHarness:
    """Pre-seed `tw`'s backend cache with a `LangchainBackend` whose
    `chat_model_factory` pops from a queue -- see module docstring."""
    queue: list[BaseChatModel] = []

    def chat_model_factory(_model_spec: ModelSpec) -> BaseChatModel:
        if not queue:
            raise AssertionError(
                "conformance scenario invoked the model without scripting a response"
            )
        return queue.pop(0)

    backend = LangchainBackend(profile, NativeStoreConfig(), chat_model_factory=chat_model_factory)
    tw._backends[profile_name] = backend
    return LangchainHarness(tradewind=tw, capabilities=backend.capabilities(), _queue=queue)


@pytest.fixture
def harness(tmp_path: Path) -> LangchainHarness:
    profile = _profile()
    config = TradewindConfig(
        profiles={"default": profile},
        default_profile="default",
        store=tmp_path / "sessions.db",
    )
    return _wire_harness(Tradewind(config), profile)


# --- the matrix, run against the harness above ---


async def test_single_turn_text(harness: LangchainHarness) -> None:
    await matrix.single_turn_text(harness)


async def test_tool_allow_deny(harness: LangchainHarness) -> None:
    await matrix.tool_allow_deny(harness)


async def test_interrupt_midturn(harness: LangchainHarness) -> None:
    await matrix.interrupt_midturn(harness)


async def test_resume_continues_context(harness: LangchainHarness) -> None:
    await matrix.resume_continues_context(harness)


async def test_history_flat_and_tree(harness: LangchainHarness) -> None:
    await matrix.history_flat_and_tree(harness)


async def test_structured_output(harness: LangchainHarness) -> None:
    # Expected to skip: `LangchainBackend.capabilities().supports_structured_
    # output` is False (task-8 fix round 1) -- this exercises the skip
    # machinery itself, not structured output (task-9 brief).
    await matrix.structured_output(harness)


async def test_system_prompt_respected(harness: LangchainHarness) -> None:
    await matrix.system_prompt_respected(harness)


# --- Step 3: meridian-shaped embedding test (component spec 01 Acceptance) ---


async def test_embedding_from_plain_dict_config_runs_a_turn_without_touching_extra_paths(
    tmp_path: Path,
) -> None:
    """A host builds `TradewindConfig` from its own plain dict, `ensure`s a
    session, `run`s a turn, and reads `history` back -- and Tradewind never
    opens any path but the one sqlite file the host gave it (no env reads,
    no config files -- FR-10.4)."""
    db_path = tmp_path / "app-sessions.db"
    plain_config: dict[str, object] = {
        "profiles": {
            "default": {
                "backend": "langchain",
                "auth": {"kind": "api_key", "api_key": "sk-test"},
                "models": {"standard": {"model": "claude-sonnet-4-5"}},
            }
        },
        "default_profile": "default",
        # A plain string path (ADR-0001's collapsed union field): the host's
        # dict stays JSON-shaped; pydantic coerces str -> Path.
        "store": str(db_path),
    }
    config = TradewindConfig.model_validate(plain_config)
    harness = _wire_harness(Tradewind(config), config.profiles["default"])
    harness.script_text_response("pong")

    session_id = str(uuid.uuid4())
    session = await harness.tradewind.ensure(session_id, SessionOptions())
    result = await session.run("ping")
    assert result.status == "completed"
    assert result.final_text == "pong"

    history = await harness.tradewind.history(session_id)
    assert [m.content.get("text") for m in history] == ["ping", "pong"]

    # Nothing under tmp_path but the sqlite store (and its WAL sidecars)
    # was ever created -- no config file, nothing else touched.
    touched_names = {p.name for p in tmp_path.iterdir()}
    sqlite_sidecars = {f"{db_path.name}{suffix}" for suffix in ("", "-wal", "-shm", "-journal")}
    assert touched_names <= sqlite_sidecars
    assert db_path.name in touched_names
