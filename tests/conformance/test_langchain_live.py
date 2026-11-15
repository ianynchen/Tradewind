"""Live structured-output checks for `LangchainBackend` against the real
Anthropic API (FR-1.3) -- the fake-model conformance run
(`tests/conformance/test_langchain.py`) cannot prove what only the real API
decides: that the forced schema-tool call (caller tools still bound, plus
the un-stored user instruction ahead of it) is ACCEPTED.

Opt-in, same shape as `test_claude.py`: every test is
`@pytest.mark.integration` AND the module skips unless
`TRADEWIND_RUN_LANGCHAIN_INTEGRATION` is set, so `scripts/check.sh` (a plain
`uv run pytest`) never makes these billed calls. The API key is read ONLY
from the `ANTHROPIC_API_KEY` environment variable into the profile's
`ApiKeyAuth` (a `SecretStr`); it is never logged, printed, persisted, or
read from any file.

Cheap by construction: `claude-haiku-4-5`, `max_tokens=256` (set through
the `chat_model_factory` seam -- `ModelSpec` has no token limit), an
ephemeral in-memory store, and at most 3 model calls per test (2 + 3).
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel

from tradewind.adapters.langchain_backend import _STRUCTURED_OUTPUT_TOOL, LangchainBackend
from tradewind.application.client import Tradewind
from tradewind.application.config import NativeStoreConfig, TradewindConfig
from tradewind.domain.events import Event, ItemCompleted, TextDelta, TurnCompleted
from tradewind.domain.models import ApiKeyAuth, ModelSpec, Profile, SessionOptions, Tool

pytestmark = pytest.mark.integration

if not os.environ.get("TRADEWIND_RUN_LANGCHAIN_INTEGRATION"):
    pytest.skip(
        "set TRADEWIND_RUN_LANGCHAIN_INTEGRATION=1 (and ANTHROPIC_API_KEY) to run these "
        "against the real Anthropic API (billed API calls)",
        allow_module_level=True,
    )

_MODEL = "claude-haiku-4-5"
_MAX_TOKENS = 256


def _tradewind() -> Tradewind:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.fail("TRADEWIND_RUN_LANGCHAIN_INTEGRATION is set but ANTHROPIC_API_KEY is not")
    auth = ApiKeyAuth(api_key=os.environ["ANTHROPIC_API_KEY"])  # type: ignore[arg-type]
    profile = Profile(backend="langchain", auth=auth, models={"standard": ModelSpec(model=_MODEL)})

    def chat_model_factory(spec: ModelSpec) -> BaseChatModel:
        # The default factory, plus a small output cap for cost.
        return ChatAnthropic(  # type: ignore[call-arg]
            model_name=spec.model, api_key=auth.api_key, max_tokens=_MAX_TOKENS
        )

    tw = Tradewind(TradewindConfig(profiles={"default": profile}, default_profile="default"))
    tw._backends["default"] = LangchainBackend(
        profile, NativeStoreConfig(), chat_model_factory=chat_model_factory
    )
    return tw


def _content_items(events: list[Event]) -> list[Any]:
    return [
        e.message
        for e in events
        if isinstance(e, ItemCompleted) and e.message.kind in ("text", "tool_use", "tool_result")
    ]


def _assert_plain_turn_content(events: list[Event], final_text: str | None) -> None:
    """The schema turn's content is a plain turn's: no schema-tool item, and
    `final_text` is the model's own last text item (FR-1.3, additive)."""
    items = _content_items(events)
    assert not any(
        m.kind == "tool_use" and str(m.content.get("name")) == _STRUCTURED_OUTPUT_TOOL
        for m in items
    )
    texts = [m.content["text"] for m in items if m.kind == "text"]
    assert final_text is not None
    assert texts and texts[-1] in final_text  # final_text joins the last reply's blocks
    streamed = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert final_text in streamed  # deltas are the natural reply only


async def test_structured_output_live() -> None:
    """2 model calls: the natural reply, then the forced schema-tool call."""
    tw = _tradewind()
    session = await tw.create(
        str(uuid.uuid4()),
        SessionOptions(
            output_schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            }
        ),
    )

    events = [e async for e in session.stream('Return {"ok": true}.', max_tool_rounds=2)]

    result = next(e for e in events if isinstance(e, TurnCompleted)).result
    assert result.end_reason == "end_turn"
    assert result.structured_output == {"ok": True}
    _assert_plain_turn_content(events, result.final_text)


async def test_structured_output_after_a_tool_round_live() -> None:
    """3 model calls: the tool call, the natural reply, then the forced
    call -- which the API must accept with the caller's tool still bound
    and the un-stored user instruction ahead of it."""
    calls: list[dict[str, object]] = []

    async def get_answer(**kwargs: object) -> str:
        calls.append(kwargs)
        return "42"

    tool = Tool(
        name="get_answer",
        description="Returns the answer.",
        input_schema={"type": "object", "properties": {}},
        handler=get_answer,
    )
    tw = _tradewind()
    session = await tw.create(
        str(uuid.uuid4()),
        SessionOptions(
            tools=[tool],
            output_schema={
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
        ),
    )

    events = [
        e
        async for e in session.stream(
            "Call the get_answer tool once, then tell me the answer it gave.",
            max_tool_rounds=1,
        )
    ]

    assert len(calls) == 1
    result = next(e for e in events if isinstance(e, TurnCompleted)).result
    assert result.end_reason == "end_turn"
    assert result.structured_output is not None
    assert isinstance(result.structured_output.get("answer"), str)
    _assert_plain_turn_content(events, result.final_text)
