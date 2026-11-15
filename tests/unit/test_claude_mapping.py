"""Tests for `tradewind.adapters.claude_backend`'s pure event-mapping
functions (task-10 brief): fixture `claude_agent_sdk` message dataclasses
constructed directly, no CLI subprocess, no network -- the SDK boundary
itself is exercised live only by `tests/conformance/test_claude.py`
(`@pytest.mark.integration`).
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SdkMcpTool,
    SessionMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk import _build_input_schema as sdk_build_input_schema

from tradewind.adapters.claude_backend import (
    _normalize_input_schema,
    assistant_message_items,
    is_aborted_result,
    is_max_turns_result,
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


def test_assistant_items_carry_the_message_uuid_as_native_id() -> None:
    # Task-11 fix: without this, `store.last_native_id()` never advances
    # past a live-streamed turn and `ResumePlanner.reconcile()`'s next
    # `read_native_transcript(after=None)` re-imports that turn's content
    # from the transcript file as brand-new (task-11 report).  One message
    # with several blocks -- thinking + tool_use, a common real combination
    # (task-11 live run) -- must stamp the SAME uuid on every item it
    # produces (matches `native_transcript_items`'s own per-entry
    # convention), not just the first.
    message = _assistant(
        [
            ThinkingBlock(thinking="thinking", signature="sig"),
            ToolUseBlock(id="toolu_3", name="a_tool", input={}),
        ],
        uuid="msg-uuid-1",
    )

    items = assistant_message_items(message)

    assert [item.native_id for item in items] == ["msg-uuid-1", "msg-uuid-1"]


def test_assistant_items_native_id_is_none_when_message_uuid_is_none() -> None:
    message = _assistant([TextBlock(text="hi")])

    items = assistant_message_items(message)

    assert items[0].native_id is None


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


def test_user_tool_result_items_carry_the_message_uuid_as_native_id() -> None:
    # Same task-11 fix as assistant_message_items -- see its own test.
    message = UserMessage(
        content=[ToolResultBlock(tool_use_id="toolu_1", content="ok", is_error=False)],
        uuid="msg-uuid-2",
    )

    items = user_message_items(message)

    assert items[0].native_id == "msg-uuid-2"


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


# --- _normalize_input_schema ---
#
# Regression coverage for fix round 1: `create_sdk_mcp_server`'s own
# `_build_input_schema` (imported directly from the installed SDK below, not
# reimplemented) only takes a `Tool.input_schema` dict literally when it has
# both `"type"` (a str) and `"properties"`; missing `"properties"`, it
# reinterprets the dict's own top-level keys as a `{param_name: python_type}`
# shorthand instead -- silently corrupting a schema like `{"type": "object"}`
# (see `_normalize_input_schema`'s docstring for the exact mechanism). These
# tests assert against `_build_input_schema` itself, not a description of
# it, so they break if a future SDK version changes that guard.


async def _noop_handler(_args: dict[str, Any]) -> dict[str, Any]:
    return {"content": []}


def _built_schema(input_schema: dict[str, Any]) -> dict[str, Any]:
    tool = SdkMcpTool(name="t", description="d", input_schema=input_schema, handler=_noop_handler)
    return sdk_build_input_schema(tool)


def test_normalize_input_schema_adds_empty_properties_when_missing() -> None:
    normalized = _normalize_input_schema({"type": "object"})

    assert normalized == {"type": "object", "properties": {}}


def test_normalize_input_schema_object_without_properties_reaches_sdk_literal_path() -> None:
    # The actual regression: unnormalized, the SDK's own `_build_input_schema`
    # reinterprets `{"type": "object"}`'s `"type"` entry as a bogus parameter
    # named `type` (confirmed empirically against claude-agent-sdk 0.2.151,
    # task-10 fix-round-1 report) -- `sdk_build_input_schema({"type":
    # "object"})` alone would return `{"type": "object", "properties":
    # {"type": {"type": "string"}}, "required": ["type"]}`, not a schema with
    # no declared parameters.
    normalized = _normalize_input_schema({"type": "object"})

    assert _built_schema(normalized) == {"type": "object", "properties": {}}


def test_normalize_input_schema_with_properties_is_unchanged() -> None:
    schema = {
        "type": "object",
        "properties": {"x": {"type": "integer"}},
        "required": ["x"],
    }

    normalized = _normalize_input_schema(schema)

    assert normalized == schema
    assert normalized is schema  # untouched, not a defensive copy
    assert _built_schema(normalized) == schema


def test_normalize_input_schema_without_type_key_is_left_to_sdk_shorthand() -> None:
    # No `"type"` key at all is the SDK's own, intentional shorthand form
    # (`{param_name: python_type}`) -- not a schema this adapter's callers
    # would confuse for JSON Schema, so it is left alone rather than forced
    # through the literal path.
    schema = {"text": str}

    assert _normalize_input_schema(schema) is schema


# --- end_reason (FR-6.5) and is_max_turns_result ---


def test_turn_result_end_reason_defaults_to_end_turn() -> None:
    # Includes stop_reason=None (older CLIs report none): a clean-finish
    # ResultMessage with no contrary signal ended the turn itself.
    assert turn_result_from_result_message(_result(), turn_id="turn-1").end_reason == "end_turn"


def test_turn_result_end_reason_max_tokens_from_stop_reason() -> None:
    # Truncation must never be reported as a clean end_turn -- the lie
    # FR-6.5 exists to prevent.
    result = turn_result_from_result_message(_result(stop_reason="max_tokens"), turn_id="turn-1")
    assert result.end_reason == "max_tokens"


def test_turn_result_end_reason_max_tool_rounds_for_a_max_turns_result() -> None:
    # The CLI reports its max_turns stop as an error result; tradewind only
    # ever sets max_turns from the caller's own cap, so this is the cap
    # doing its job -- an honest partial completion.
    result = turn_result_from_result_message(
        _result(subtype="error_max_turns", is_error=True, terminal_reason="max_turns"),
        turn_id="turn-1",
    )
    assert result.status == "completed"
    assert result.end_reason == "max_tool_rounds"


def test_is_max_turns_result_true_for_terminal_reason_max_turns() -> None:
    assert is_max_turns_result(_result(terminal_reason="max_turns")) is True


def test_is_max_turns_result_true_for_error_max_turns_subtype() -> None:
    # Older CLIs may report the subtype without a terminal_reason.
    assert is_max_turns_result(_result(subtype="error_max_turns", is_error=True)) is True


def test_is_max_turns_result_false_for_a_plain_success() -> None:
    assert is_max_turns_result(_result()) is False


def test_is_max_turns_result_false_for_an_ordinary_error() -> None:
    # A genuine failure must keep failing -- only the caller's own cap is
    # reclassified as completion.
    assert is_max_turns_result(_result(subtype="error_during_execution", is_error=True)) is False


# --- turn governance: work signals (05-turn-governance §7, Claude row) ---


class _RecordingWork:
    """Records every work signal in call order."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def started(
        self,
        id: str,  # noqa: A002
        kind: str,
        label: str,
        has_end_signal: bool = True,  # noqa: ARG002 -- signature parity only
    ) -> None:
        self.calls.append(("started", id, kind, label))

    def active(self, id: str) -> None:  # noqa: A002
        self.calls.append(("active", id))

    def ended(self, id: str) -> None:  # noqa: A002
        self.calls.append(("ended", id))

    def model_active(self) -> None:
        self.calls.append(("model_active",))

    def engine_done(self) -> None:
        self.calls.append(("engine_done",))


def _signals(messages: list[object]) -> list[tuple[object, ...]]:
    from tradewind.adapters.claude_backend import report_work

    work = _RecordingWork()
    for message in messages:
        report_work(work, message)  # type: ignore[arg-type]
    return work.calls


def test_task_messages_drive_work_signals() -> None:
    """Background tasks hold the turn open (§3.3): each lifecycle message
    maps to exactly one signal, and a terminal status from EITHER the
    notification or the update closes the task (the SDK's own lifecycle
    note: a killed task may report only through `task_updated`)."""
    from claude_agent_sdk import (
        TaskNotificationMessage,
        TaskProgressMessage,
        TaskStartedMessage,
        TaskUpdatedMessage,
    )

    usage: Any = {"total_tokens": 1, "tool_uses": 0, "duration_ms": 1}
    calls = _signals(
        [
            TaskStartedMessage(
                subtype="task_started",
                data={},
                task_id="bg1",
                description="build",
                uuid="u1",
                session_id="s",
            ),
            TaskProgressMessage(
                subtype="task_progress",
                data={},
                task_id="bg1",
                description="build",
                usage=usage,
                uuid="u2",
                session_id="s",
            ),
            TaskUpdatedMessage(
                subtype="task_updated", data={}, task_id="bg1", patch={"status": "running"}
            ),
            TaskNotificationMessage(
                subtype="task_notification",
                data={},
                task_id="bg1",
                status="completed",
                output_file="/tmp/o",
                summary="ok",
                uuid="u3",
                session_id="s",
            ),
            TaskStartedMessage(
                subtype="task_started",
                data={},
                task_id="bg2",
                description="watch",
                uuid="u4",
                session_id="s",
            ),
            TaskUpdatedMessage(
                subtype="task_updated", data={}, task_id="bg2", patch={"status": "killed"}
            ),
        ]
    )

    assert calls == [
        ("started", "bg1", "background", "build"),
        ("active", "bg1"),
        ("ended", "bg1"),
        ("started", "bg2", "background", "watch"),
        ("ended", "bg2"),
    ]


def test_tool_blocks_open_and_close_tool_work() -> None:
    calls = _signals(
        [
            _assistant(
                [
                    TextBlock(text="let me check"),
                    ToolUseBlock(id="t1", name="mcp__tradewind__x", input={}),
                ]
            ),
            UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="ok")]),
            UserMessage(content="plain text echo"),
        ]
    )

    assert calls == [("started", "t1", "tool", "mcp__tradewind__x"), ("ended", "t1")]


def test_subagent_messages_report_activity_of_their_parent_tool() -> None:
    """A subagent's own tool traffic arrives with `parent_tool_use_id`; it is
    activity of the spawning call, so work silence does not fire while the
    subagent is busy."""
    calls = _signals(
        [
            _assistant(
                [ToolUseBlock(id="sub-t", name="Read", input={})], parent_tool_use_id="agent-1"
            ),
            UserMessage(
                content=[ToolResultBlock(tool_use_id="sub-t", content="x")],
                parent_tool_use_id="agent-1",
            ),
        ]
    )

    assert calls == [
        ("started", "sub-t", "tool", "Read"),
        ("active", "agent-1"),
        ("ended", "sub-t"),
        ("active", "agent-1"),
    ]


def test_stream_events_are_model_activity_only_and_result_is_engine_done() -> None:
    from claude_agent_sdk import StreamEvent

    calls = _signals(
        [
            StreamEvent(uuid="e1", session_id="s", event={"type": "content_block_delta"}),
            ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="s",
            ),
        ]
    )

    assert calls == [("model_active",), ("engine_done",)]


def test_slash_prompt_is_message() -> None:
    """05 §5 (owner M11): a prompt is always a message to the model. The
    bundled CLI's `--disable-slash-commands` still intercepts `/cost` (live
    check, CLI 2.1.258), so the prefix rule applies."""
    from tradewind.adapters.claude_backend import message_prompt

    assert message_prompt("/clear") == "Message: /clear"
    assert message_prompt("  /compact now") == "Message:   /compact now"
    assert message_prompt("explain a/b") == "explain a/b"
    assert message_prompt("") == ""
