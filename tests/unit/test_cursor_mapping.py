"""Pure mapping tests for `tradewind.adapters.cursor_backend`: `SDKMessage`
-> `NormalizedMessage` (task-15 brief). SDK dataclasses are constructed
directly (no live `cursor-sdk-bridge` subprocess -- none exists, no Cursor
subscription on this machine) -- a real session is
`tests/conformance/test_cursor.py`, permanently skipped until P-5 is
resolved (see that module's own docstring).
"""

from __future__ import annotations

import pytest
from cursor_sdk import (
    SDKAssistantMessage,
    SDKAssistantMessageContent,
    SDKStatusMessage,
    SDKThinkingMessage,
    SDKToolUseMessage,
    SDKUsageMessage,
    SDKUserMessageContent,
    SDKUserMessageEvent,
    TextBlock,
    TokenUsage,
    ToolUseBlock,
)

from tradewind.adapters.cursor_backend import (
    _agent_options,
    _launch_local_options,
    _tool_result_text,
    _usage_dict,
    sdk_message_items,
)
from tradewind.application.config import NativeStoreConfig
from tradewind.domain.errors import ConfigError

# --- sdk_message_items ----------------------------------------------------


def test_assistant_text_block_maps_to_text() -> None:
    message = SDKAssistantMessage(
        type="assistant",
        agent_id="agent-1",
        run_id="run-1",
        message=SDKAssistantMessageContent(
            role="assistant", content=(TextBlock(type="text", text="hello there"),)
        ),
    )

    items = sdk_message_items(message)

    assert len(items) == 1
    assert items[0].role == "assistant"
    assert items[0].kind == "text"
    assert items[0].content == {"text": "hello there"}
    assert items[0].native_id is None


def test_assistant_empty_text_block_produces_nothing() -> None:
    message = SDKAssistantMessage(
        type="assistant",
        agent_id="agent-1",
        run_id="run-1",
        message=SDKAssistantMessageContent(
            role="assistant", content=(TextBlock(type="text", text=""),)
        ),
    )

    assert sdk_message_items(message) == []


def test_assistant_tool_use_block_is_skipped() -> None:
    # The authoritative call+result row comes from SDKToolUseMessage, not
    # this content block (module docstring: no status/result to pair
    # against here, would duplicate under a different id scheme).
    message = SDKAssistantMessage(
        type="assistant",
        agent_id="agent-1",
        run_id="run-1",
        message=SDKAssistantMessageContent(
            role="assistant",
            content=(ToolUseBlock(type="tool_use", id="call-1", name="my_tool", input={"x": 1}),),
        ),
    )

    assert sdk_message_items(message) == []


def test_thinking_maps_to_thinking() -> None:
    message = SDKThinkingMessage(
        type="thinking", agent_id="agent-1", run_id="run-1", text="pondering..."
    )

    items = sdk_message_items(message)

    assert len(items) == 1
    assert items[0].kind == "thinking"
    assert items[0].content == {"text": "pondering..."}


def test_thinking_with_empty_text_produces_nothing() -> None:
    message = SDKThinkingMessage(type="thinking", agent_id="agent-1", run_id="run-1", text="")

    assert sdk_message_items(message) == []


def test_user_message_event_is_skipped() -> None:
    # The turn runner already mirrors ctx.prompt itself; re-emitting it here
    # would duplicate it (module docstring).
    message = SDKUserMessageEvent(
        type="user",
        agent_id="agent-1",
        run_id="run-1",
        message=SDKUserMessageContent(role="user", content=()),
    )

    assert sdk_message_items(message) == []


def test_completed_tool_call_maps_to_tool_use_and_tool_result() -> None:
    message = SDKToolUseMessage(
        type="tool_call",
        agent_id="agent-1",
        run_id="run-1",
        call_id="call-1",
        name="my_tool",
        status="completed",
        args={"x": "a"},
        result={"content": [{"type": "text", "text": "ok"}]},
    )

    items = sdk_message_items(message)

    assert len(items) == 2
    tool_use, tool_result = items
    assert tool_use.kind == "tool_use"
    assert tool_use.content == {"id": "call-1", "name": "my_tool", "input": {"x": "a"}}
    assert tool_use.native_id == "call-1"
    assert tool_result.kind == "tool_result"
    assert tool_result.content == {"tool_use_id": "call-1", "content": "ok", "is_error": False}
    assert tool_result.native_id == "call-1"


def test_errored_tool_call_maps_is_error_true() -> None:
    message = SDKToolUseMessage(
        type="tool_call",
        agent_id="agent-1",
        run_id="run-1",
        call_id="call-2",
        name="my_tool",
        status="error",
        args={},
        result="boom",
    )

    items = sdk_message_items(message)

    tool_result = items[1]
    assert tool_result.content == {"tool_use_id": "call-2", "content": "boom", "is_error": True}


def test_running_tool_call_produces_nothing() -> None:
    # A status update mid-execution, not yet a completed item -- store
    # completed items, not deltas (research doc §4.2).
    message = SDKToolUseMessage(
        type="tool_call",
        agent_id="agent-1",
        run_id="run-1",
        call_id="call-3",
        name="my_tool",
        status="running",
        args={},
        result=None,
    )

    assert sdk_message_items(message) == []


def test_tool_call_with_no_args_maps_to_empty_input() -> None:
    message = SDKToolUseMessage(
        type="tool_call",
        agent_id="agent-1",
        run_id="run-1",
        call_id="call-4",
        name="my_tool",
        status="completed",
        args=None,
        result="ok",
    )

    tool_use = sdk_message_items(message)[0]
    assert tool_use.content == {"id": "call-4", "name": "my_tool", "input": {}}


def test_unknown_message_kind_maps_to_event_with_raw() -> None:
    message = SDKStatusMessage(
        type="status", agent_id="agent-1", run_id="run-1", status="thinking", message="hi"
    )

    items = sdk_message_items(message)

    assert len(items) == 1
    assert items[0].kind == "event"
    assert items[0].content == {"type": "status"}
    assert items[0].raw is not None
    assert items[0].raw.get("status") == "thinking"


def test_usage_message_maps_to_event_with_raw_usage() -> None:
    message = SDKUsageMessage(
        type="usage",
        agent_id="agent-1",
        run_id="run-1",
        usage=TokenUsage(
            input_tokens=1,
            output_tokens=2,
            cache_read_tokens=0,
            cache_write_tokens=0,
            total_tokens=3,
        ),
    )

    items = sdk_message_items(message)

    assert items[0].kind == "event"
    assert items[0].content == {"type": "usage"}
    assert items[0].raw is not None
    assert items[0].raw["usage"]["total_tokens"] == 3


def test_mapping_dict_fallback_maps_to_event_with_raw() -> None:
    # `SDKMessage` itself includes a bare `Mapping[str, Any]` fallback for
    # anything the SDK's own `sdk_message_from_json` doesn't recognize.
    message = {"type": "some_future_kind", "foo": "bar"}

    items = sdk_message_items(message)

    assert items[0].kind == "event"
    assert items[0].content == {"type": "some_future_kind"}
    assert items[0].raw == {"type": "some_future_kind", "foo": "bar"}


# --- _tool_result_text -----------------------------------------------------


def test_tool_result_text_reads_mcp_shaped_content() -> None:
    assert _tool_result_text({"content": [{"type": "text", "text": "hi"}]}) == "hi"


def test_tool_result_text_of_none_is_empty_string() -> None:
    assert _tool_result_text(None) == ""


def test_tool_result_text_of_plain_string_is_itself() -> None:
    assert _tool_result_text("already text") == "already text"


def test_tool_result_text_falls_back_to_json_for_unrecognized_shapes() -> None:
    assert _tool_result_text({"weird": 1}) == '{"weird": 1}'


# --- _usage_dict ------------------------------------------------------------


def test_usage_dict_reads_all_fields() -> None:
    usage = TokenUsage(
        input_tokens=1,
        output_tokens=2,
        cache_read_tokens=3,
        cache_write_tokens=4,
        total_tokens=10,
        reasoning_tokens=5,
    )

    assert _usage_dict(usage) == {
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_read_tokens": 3,
        "cache_write_tokens": 4,
        "total_tokens": 10,
        "reasoning_tokens": 5,
    }


def test_usage_dict_omits_reasoning_tokens_when_absent() -> None:
    usage = TokenUsage(
        input_tokens=1,
        output_tokens=2,
        cache_read_tokens=0,
        cache_write_tokens=0,
        total_tokens=3,
    )

    assert "reasoning_tokens" not in _usage_dict(usage)


# --- _agent_options ---------------------------------------------------------


def test_agent_options_disables_builtin_tools_and_carries_custom_tools() -> None:
    options = _agent_options("gpt-cursor", "/workdir", {})

    payload = options.to_json()

    assert payload["model"] == {"id": "gpt-cursor"}
    assert payload["tools"] == {"names": []}
    assert payload["local"]["cwd"] == ["/workdir"]


# --- _launch_local_options: DR-3 isolation gate -----------------------------


def test_launch_local_options_is_none_when_isolation_mode_is_off() -> None:
    assert _launch_local_options(NativeStoreConfig(isolation_mode=False)) is None


def test_launch_local_options_carries_cursor_store_when_isolation_mode_is_on() -> None:
    store = {"type": "sqlite", "rootDir": "/isolated"}

    options = _launch_local_options(NativeStoreConfig(isolation_mode=True, cursor_store=store))

    assert options is not None
    assert options.store == store


def test_launch_local_options_raises_when_isolation_mode_is_on_without_a_store() -> None:
    with pytest.raises(ConfigError, match="cursor_store"):
        _launch_local_options(NativeStoreConfig(isolation_mode=True, cursor_store=None))
