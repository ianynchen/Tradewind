"""Tests for `tradewind.adapters.claude_backend`'s pure event-mapping
functions (task-10 brief): fixture `claude_agent_sdk` message dataclasses
constructed directly, no CLI subprocess, no network -- the SDK boundary
itself is exercised live only by `tests/conformance/test_claude.py`
(`@pytest.mark.integration`).
"""

from __future__ import annotations

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SessionMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from tradewind.adapters.claude_backend import (
    assistant_message_items,
    is_aborted_result,
    native_transcript_items,
    turn_result_from_result_message,
    user_message_items,
)

# --- assistant_message_items ---


def _assistant(content: list[object], **kwargs: object) -> AssistantMessage:
    return AssistantMessage(content=content, model="claude-sonnet-4-5", **kwargs)  # type: ignore[arg-type]


def test_assistant_text_block_maps_to_text_item() -> None:
    message = _assistant([TextBlock(text="hello there")])

    items = assistant_message_items(message)

    assert len(items) == 1
    assert items[0].role == "assistant"
    assert items[0].kind == "text"
    assert items[0].content == {"text": "hello there"}


def test_assistant_empty_text_block_is_dropped() -> None:
    # A streamed-but-empty text block carries no information worth
    # persisting -- matches LangchainBackend's `_completed_items` precedent.
    message = _assistant([TextBlock(text="")])

    assert assistant_message_items(message) == []


def test_assistant_thinking_block_carries_signature_in_raw() -> None:
    message = _assistant([ThinkingBlock(thinking="pondering...", signature="sig-abc")])

    items = assistant_message_items(message)

    assert len(items) == 1
    assert items[0].kind == "thinking"
    assert items[0].content == {"text": "pondering..."}
    assert items[0].raw == {"signature": "sig-abc"}


def test_assistant_tool_use_block_maps_to_tool_use_item() -> None:
    message = _assistant([ToolUseBlock(id="toolu_1", name="allowed_tool", input={"x": 1})])

    items = assistant_message_items(message)

    assert len(items) == 1
    assert items[0].kind == "tool_use"
    assert items[0].content == {"id": "toolu_1", "name": "allowed_tool", "input": {"x": 1}}


def test_assistant_multiple_blocks_preserve_order() -> None:
    message = _assistant(
        [
            ThinkingBlock(thinking="thinking first", signature="sig"),
            TextBlock(text="then text"),
            ToolUseBlock(id="toolu_2", name="a_tool", input={}),
        ]
    )

    items = assistant_message_items(message)

    assert [item.kind for item in items] == ["thinking", "text", "tool_use"]


# --- user_message_items ---


def test_user_tool_result_block_maps_to_tool_result_item() -> None:
    message = UserMessage(
        content=[ToolResultBlock(tool_use_id="toolu_1", content="ok:{'x': 1}", is_error=None)]
    )

    items = user_message_items(message)

    assert len(items) == 1
    assert items[0].role == "tool"
    assert items[0].kind == "tool_result"
    assert items[0].content == {
        "tool_use_id": "toolu_1",
        "content": "ok:{'x': 1}",
        "is_error": False,
    }


def test_user_tool_result_error_flag_preserved() -> None:
    message = UserMessage(
        content=[ToolResultBlock(tool_use_id="toolu_1", content="boom", is_error=True)]
    )

    items = user_message_items(message)

    assert items[0].content["is_error"] is True


def test_user_tool_result_list_content_joins_text_parts() -> None:
    # A tool result whose content is a list of content-part dicts (rather
    # than a plain string) -- the SDK's own `ToolResultBlock.content` type
    # allows both.
    message = UserMessage(
        content=[
            ToolResultBlock(
                tool_use_id="toolu_1",
                content=[
                    {"type": "text", "text": "part one"},
                    {"type": "text", "text": "part two"},
                ],
                is_error=False,
            )
        ]
    )

    items = user_message_items(message)

    assert items[0].content["content"] == "part onepart two"


def test_user_message_plain_text_content_yields_nothing() -> None:
    # A string-content UserMessage is either this adapter's own submitted
    # prompt echoed back, or CLI-synthesized text (e.g. an interrupt
    # notice) -- neither is something this adapter mirrors as an item.
    message = UserMessage(content="hi")

    assert user_message_items(message) == []


def test_user_message_text_block_yields_nothing() -> None:
    # Only ToolResultBlock is in the task-10 brief's UserMessage mapping
    # scope; a TextBlock inside a UserMessage (e.g. "[Request interrupted
    # by user]") is not persisted by this adapter.
    message = UserMessage(content=[TextBlock(text="[Request interrupted by user]")])

    assert user_message_items(message) == []


# --- is_aborted_result ---


def _result(**overrides: object) -> ResultMessage:
    base: dict[str, object] = {
        "subtype": "success",
        "duration_ms": 100,
        "duration_api_ms": 90,
        "is_error": False,
        "num_turns": 1,
        "session_id": "sess-1",
    }
    base.update(overrides)
    return ResultMessage(**base)  # type: ignore[arg-type]


def test_is_aborted_result_true_for_aborted_streaming() -> None:
    assert is_aborted_result(_result(terminal_reason="aborted_streaming")) is True


def test_is_aborted_result_true_for_aborted_tools() -> None:
    assert is_aborted_result(_result(terminal_reason="aborted_tools")) is True


def test_is_aborted_result_false_for_completed() -> None:
    assert is_aborted_result(_result(terminal_reason="completed")) is False


def test_is_aborted_result_false_when_absent() -> None:
    assert is_aborted_result(_result()) is False


# --- turn_result_from_result_message ---


def test_turn_result_maps_final_text_usage_and_cost() -> None:
    result = _result(
        result="pong",
        total_cost_usd=0.0123,
        usage={"input_tokens": 10, "output_tokens": 5, "service_tier": "standard"},
    )

    turn_result = turn_result_from_result_message(result, turn_id="turn-1")

    assert turn_result.turn_id == "turn-1"
    assert turn_result.status == "completed"
    assert turn_result.final_text == "pong"
    assert turn_result.cost_usd == 0.0123
    # Non-int usage values (e.g. "service_tier": "standard") are dropped --
    # `TurnResult.usage` is typed `dict[str, int]`.
    assert turn_result.usage == {"input_tokens": 10, "output_tokens": 5}


def test_turn_result_usage_none_becomes_empty_dict() -> None:
    turn_result = turn_result_from_result_message(_result(usage=None), turn_id="turn-1")

    assert turn_result.usage == {}


# --- native_transcript_items ---


def _session_message(entry_type: str, uuid: str, message: dict[str, object]) -> SessionMessage:
    return SessionMessage(type=entry_type, uuid=uuid, session_id="sess-1", message=message)  # type: ignore[arg-type]


def test_native_transcript_maps_user_text_entry() -> None:
    entries = [_session_message("user", "u-1", {"role": "user", "content": "hi"})]

    items = native_transcript_items(entries, after_native_id=None)

    assert len(items) == 1
    assert items[0].role == "user"
    assert items[0].kind == "text"
    assert items[0].content == {"text": "hi"}
    assert items[0].native_id == "u-1"


def test_native_transcript_maps_assistant_blocks() -> None:
    entries = [
        _session_message(
            "assistant",
            "a-1",
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hmm", "signature": "sig-xyz"},
                    {"type": "text", "text": "hello there"},
                    {"type": "tool_use", "id": "toolu_1", "name": "a_tool", "input": {"x": 1}},
                ],
            },
        )
    ]

    items = native_transcript_items(entries, after_native_id=None)

    assert [item.kind for item in items] == ["thinking", "text", "tool_use"]
    assert all(item.native_id == "a-1" for item in items)
    assert items[0].raw == {"signature": "sig-xyz"}
    assert items[2].content == {"id": "toolu_1", "name": "a_tool", "input": {"x": 1}}


def test_native_transcript_maps_tool_result_block_to_tool_role() -> None:
    entries = [
        _session_message(
            "user",
            "u-2",
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "ok",
                        "is_error": False,
                    }
                ],
            },
        )
    ]

    items = native_transcript_items(entries, after_native_id=None)

    assert len(items) == 1
    assert items[0].role == "tool"
    assert items[0].kind == "tool_result"
    assert items[0].content == {"tool_use_id": "toolu_1", "content": "ok", "is_error": False}


def test_native_transcript_after_native_id_drops_up_to_and_including_it() -> None:
    entries = [
        _session_message("user", "u-1", {"role": "user", "content": "first"}),
        _session_message(
            "assistant",
            "a-1",
            {"role": "assistant", "content": [{"type": "text", "text": "first reply"}]},
        ),
        _session_message("user", "u-2", {"role": "user", "content": "second"}),
    ]

    items = native_transcript_items(entries, after_native_id="a-1")

    assert [item.content.get("text") for item in items] == ["second"]


def test_native_transcript_after_native_id_not_found_returns_everything() -> None:
    entries = [_session_message("user", "u-1", {"role": "user", "content": "hi"})]

    items = native_transcript_items(entries, after_native_id="does-not-exist")

    assert len(items) == 1


def test_native_transcript_empty_content_string_yields_nothing() -> None:
    entries = [_session_message("user", "u-1", {"role": "user", "content": ""})]

    assert native_transcript_items(entries, after_native_id=None) == []
