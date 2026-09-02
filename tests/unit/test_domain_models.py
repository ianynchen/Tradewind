"""Tests for tradewind.domain.models: SessionOptions.snapshot(), Tool, Capabilities.

Snapshot rule under test (controller ruling, binding over the brief's self-
correcting prose): McpServerDef.env/headers values are kept verbatim in the
snapshot ONLY when already "ref:"-prefixed; any other value is replaced with
the literal "ref:missing" and the snapshot is flagged has_unrefed_secrets=True.
This is I-2: secrets never land in the store.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tradewind.domain.models import (
    Capabilities,
    McpServerDef,
    SessionOptions,
    Tool,
)


def _capabilities_kwargs(**overrides: bool) -> dict[str, bool]:
    base: dict[str, bool] = {
        "supports_system_prompt": True,
        "supports_structured_output": True,
        "supports_interactive_permissions": True,
        "supports_in_process_tools": True,
        "supports_native_resume": True,
        "supports_fork": True,
        "supports_transcript_read": True,
    }
    base.update(overrides)
    return base


async def _handler(**_: Any) -> dict[str, Any]:
    return {"ok": True}


def _tool(name: str = "search") -> Tool:
    return Tool(
        name=name,
        description="search the web",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        handler=_handler,
    )


def _mcp_server(
    env: dict[str, str] | None = None, headers: dict[str, str] | None = None
) -> McpServerDef:
    return McpServerDef(
        name="fs",
        transport="stdio",
        command=["mcp-server-fs"],
        env=env or {},
        headers=headers or {},
    )


class _DummyBroker:
    async def decide(self, _tool_name: str, _tool_input: dict[str, Any]) -> str:
        return "allow"


# --- Capabilities: missing flag fails (Step 1) ---


def test_capabilities_missing_flag_fails_validation() -> None:
    kwargs = _capabilities_kwargs()
    del kwargs["supports_fork"]
    with pytest.raises(ValidationError):
        Capabilities(**kwargs)


def test_capabilities_with_all_flags_constructs() -> None:
    caps = Capabilities(**_capabilities_kwargs())
    assert caps.supports_fork is True


# --- Tool: accepts async handler (Step 1) ---


async def test_tool_accepts_and_invokes_async_handler() -> None:
    tool = _tool()
    result = await tool.handler(query="x")
    assert result == {"ok": True}


# --- SessionOptions.snapshot(): excludes live objects (Step 1) ---


def test_snapshot_excludes_tool_handlers() -> None:
    options = SessionOptions(tools=[_tool()])
    snap = options.snapshot()
    assert "handler" not in snap["tools"][0]


def test_snapshot_excludes_permission_broker() -> None:
    options = SessionOptions(permission_broker=_DummyBroker())
    snap = options.snapshot()
    assert "permission_broker" not in snap


# --- SessionOptions.snapshot(): includes tool names + schemas (Step 1) ---


def test_snapshot_includes_tool_names_and_schemas() -> None:
    tool = _tool(name="search")
    options = SessionOptions(tools=[tool])
    snap = options.snapshot()
    assert snap["tools"] == [
        {
            "name": "search",
            "description": "search the web",
            "input_schema": tool.input_schema,
        }
    ]


# --- SessionOptions.snapshot(): MCP redaction rule (Step 1, controller ruling) ---


def test_snapshot_keeps_ref_prefixed_mcp_env_verbatim() -> None:
    server = _mcp_server(env={"API_KEY": "ref:openai-key"})
    options = SessionOptions(mcp_servers=[server])
    snap = options.snapshot()
    assert snap["mcp_servers"][0]["env"] == {"API_KEY": "ref:openai-key"}
    assert snap["has_unrefed_secrets"] is False


def test_snapshot_keeps_ref_prefixed_mcp_headers_verbatim() -> None:
    server = _mcp_server(headers={"Authorization": "ref:auth-token"})
    options = SessionOptions(mcp_servers=[server])
    snap = options.snapshot()
    assert snap["mcp_servers"][0]["headers"] == {"Authorization": "ref:auth-token"}
    assert snap["has_unrefed_secrets"] is False


def test_snapshot_redacts_non_ref_mcp_env_and_flags_it() -> None:
    server = _mcp_server(env={"API_KEY": "sk-live-secret"})
    options = SessionOptions(mcp_servers=[server])
    snap = options.snapshot()
    assert snap["mcp_servers"][0]["env"] == {"API_KEY": "ref:missing"}
    assert snap["has_unrefed_secrets"] is True


def test_snapshot_redacts_non_ref_mcp_headers_and_flags_it() -> None:
    server = _mcp_server(headers={"Authorization": "Bearer sk-live"})
    options = SessionOptions(mcp_servers=[server])
    snap = options.snapshot()
    assert snap["mcp_servers"][0]["headers"] == {"Authorization": "ref:missing"}
    assert snap["has_unrefed_secrets"] is True


def test_snapshot_has_unrefed_secrets_false_when_no_mcp_servers() -> None:
    options = SessionOptions()
    snap = options.snapshot()
    assert snap["has_unrefed_secrets"] is False


# --- SessionOptions.snapshot(): other declarative parts (sensible coverage) ---


def test_snapshot_includes_declarative_scalar_fields() -> None:
    options = SessionOptions(
        profile="default",
        system_prompt="be helpful",
        tier="fast",
        output_schema={"type": "object"},
        cwd=Path("/tmp/work"),
    )
    snap = options.snapshot()
    assert snap["profile"] == "default"
    assert snap["system_prompt"] == "be helpful"
    assert snap["tier"] == "fast"
    assert snap["output_schema"] == {"type": "object"}
    assert snap["cwd"] == "/tmp/work"


def test_snapshot_cwd_none_stays_none() -> None:
    options = SessionOptions()
    snap = options.snapshot()
    assert snap["cwd"] is None
