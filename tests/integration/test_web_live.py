"""Live tests for web access (07-web-access §4, §4a) against real Claude Code
and Codex engines, through `Tradewind`. Real, billed calls with web access:
opt in with `TRADEWIND_RUN_WEB_INTEGRATION=1`. Each test depends on the model
choosing to use the web tool it is told to use; there are no retries.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from tradewind.application.client import Tradewind
from tradewind.application.config import TradewindConfig
from tradewind.domain.errors import TurnExecutionFailed
from tradewind.domain.events import Event, ItemCompleted
from tradewind.domain.models import (
    ModelSpec,
    NormalizedMessage,
    Profile,
    SessionOptions,
    SubscriptionAuth,
    WebAccess,
)
from tradewind.domain.policy import Matcher, Rule

pytestmark = pytest.mark.integration

if not os.environ.get("TRADEWIND_RUN_WEB_INTEGRATION"):
    pytest.skip(
        "set TRADEWIND_RUN_WEB_INTEGRATION=1 to run these against real Claude Code "
        "and Codex sessions with web access (billed API calls, requires local auth)",
        allow_module_level=True,
    )

# Same models as the conformance tests.
_CLAUDE_MODEL = "claude-haiku-4-5"
_CODEX_MODEL = "gpt-5.6-sol"


def _tradewind(
    backend: str, model: str, tmp_path: Path
) -> tuple[Tradewind, list[NormalizedMessage]]:
    """A client on `backend` and the list that receives every item its turns record."""
    items: list[NormalizedMessage] = []

    def on_event(event: Event) -> None:
        if isinstance(event, ItemCompleted):
            items.append(event.message)

    profile = Profile(
        backend=backend,  # type: ignore[arg-type]
        auth=SubscriptionAuth(),
        models={"standard": ModelSpec(model=model)},
    )
    config = TradewindConfig(
        profiles={"default": profile},
        default_profile="default",
        store=tmp_path / "sessions.db",
        on_event=on_event,
    )
    return Tradewind(config), items


def _recorded(items: list[NormalizedMessage], action_kind: str, outcome: str) -> list[str]:
    """Summaries of the `record=True` decisions with this action kind and outcome."""
    return [
        str(m.content["summary"])
        for m in items
        if m.content.get("type") == "policy_recorded"
        and m.content["action_kind"] == action_kind
        and m.content["outcome"] == outcome
    ]


async def test_claude_gated_search_runs_the_approved_query(tmp_path: Path) -> None:
    tw, items = _tradewind("claude", _CLAUDE_MODEL, tmp_path)
    session = await tw.create(
        str(uuid.uuid4()),
        SessionOptions(
            web=WebAccess(search=True, fetch=False),
            policy_rules=[
                # The model rewrites its query, so match a word, not the whole text.
                Rule(
                    "allow-python",
                    "web_search",
                    {"query": Matcher("regex", "(?i)python")},
                    "allow",
                    record=True,
                ),
                Rule("deny-other", "web_search", {}, "deny", record=True),
            ],
        ),
    )
    result = await session.run(
        "Use exactly one web search to find the latest stable Python release. "
        "Then reply with its version number."
    )
    assert result.status == "completed"
    assert _recorded(items, "web_search", "allow")


async def test_claude_gated_search_denied(tmp_path: Path) -> None:
    tw, items = _tradewind("claude", _CLAUDE_MODEL, tmp_path)
    session = await tw.create(
        str(uuid.uuid4()),
        SessionOptions(
            web=WebAccess(search=True, fetch=False),
            policy_rules=[Rule("deny-search", "web_search", {}, "deny", record=True)],
        ),
    )
    result = await session.run(
        "Use the WebSearch tool once to find the latest stable Python release. "
        "If the search is denied, say so and stop."
    )
    assert result.status == "completed"
    assert _recorded(items, "web_search", "deny")
    assert not _recorded(items, "web_search", "allow")
    # A denied search must give the model no result.
    search_ids = {
        m.content["id"] for m in items if m.kind == "tool_use" and m.content["name"] == "WebSearch"
    }
    assert all(
        m.content["is_error"]
        for m in items
        if m.kind == "tool_result" and m.content["tool_use_id"] in search_ids
    )


async def test_claude_fetch_tool(tmp_path: Path) -> None:
    tw, items = _tradewind("claude", _CLAUDE_MODEL, tmp_path)
    session = await tw.create(
        str(uuid.uuid4()),
        SessionOptions(
            web=WebAccess(search=False, fetch=True),
            policy_rules=[
                Rule(
                    "allow-example",
                    "fetch_url",
                    {"url": Matcher("equals", "https://example.com")},
                    "allow",
                    record=True,
                )
            ],
        ),
    )
    result = await session.run(
        "Call the tool tradewind_fetch_url with the url exactly https://example.com "
        "and tell me the title of the page."
    )
    assert result.status == "completed"
    assert _recorded(items, "fetch_url", "allow")
    assert any(
        m.kind == "tool_result" and "Example Domain" in str(m.content["content"]) for m in items
    )


async def test_codex_observed_search_is_recorded(tmp_path: Path) -> None:
    tw, items = _tradewind("codex", _CODEX_MODEL, tmp_path)
    session = await tw.create(
        str(uuid.uuid4()),
        SessionOptions(web=WebAccess(search=True, fetch=True, observed=True)),
    )
    result = await session.run(
        "Use a web search to find the latest stable Python release, then reply with its version."
    )
    assert result.status == "completed"
    assert any(m.kind == "web_search" and m.content.get("query") for m in items)


async def test_codex_observed_search_can_be_stopped(tmp_path: Path) -> None:
    tw, _ = _tradewind("codex", _CODEX_MODEL, tmp_path)
    session = await tw.create(
        str(uuid.uuid4()),
        SessionOptions(
            web=WebAccess(search=True, fetch=True, observed=True),
            policy_rules=[
                Rule("stop-search", "web_search", {}, "stop", reason="web search stopped by test")
            ],
        ),
    )
    # A policy stop ends the turn as `TurnFailed(reason)`, which `run` raises.
    with pytest.raises(TurnExecutionFailed, match="web search stopped by test"):
        await session.run(
            "Use a web search to find the latest stable Python release, then reply with its version."
        )
