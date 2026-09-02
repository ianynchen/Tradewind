"""Pure mapping tests for `tradewind.adapters.codex_backend`: `ThreadItem` ->
`NormalizedMessage` (task-14 brief). SDK dataclasses are constructed directly
(no live `codex app-server`) -- a real session is
`tests/conformance/test_codex.py` (`@pytest.mark.integration`).
"""

from __future__ import annotations

import pytest
from openai_codex.generated.v2_all import (
    AbsolutePathBuf,
    AddPatchChangeKind,
    AgentMessageThreadItem,
    CommandAction,
    CommandExecutionStatus,
    CommandExecutionThreadItem,
    DynamicToolCallOutputContentItem,
    DynamicToolCallStatus,
    DynamicToolCallThreadItem,
    FileChangeThreadItem,
    FileUpdateChange,
    IdleThreadStatus,
    InputTextDynamicToolCallOutputContentItem,
    LegacyAppPathString,
    McpToolCallError,
    McpToolCallResult,
    McpToolCallStatus,
    McpToolCallThreadItem,
    MessagePhase,
    PatchApplyStatus,
    PatchChangeKind,
    PlanThreadItem,
    ReasoningThreadItem,
    SessionSource,
    SessionSourceValue,
    Thread,
    ThreadItem,
    ThreadReadResponse,
    ThreadStatus,
    ThreadTokenUsage,
    TokenUsageBreakdown,
    Turn,
    TurnStatus,
    UnknownCommandAction,
    UserMessageThreadItem,
    WebSearchThreadItem,
)

from tradewind.adapters.codex_backend import (
    _final_text_from_items,
    _usage_dict,
    mcp_server_config_overrides,
    thread_item_to_messages,
    thread_read_items,
)
from tradewind.domain.errors import ConfigError
from tradewind.domain.models import McpServerDef

# --- thread_item_to_messages -------------------------------------------


def _item(root: object) -> ThreadItem:
    return ThreadItem(root)  # type: ignore[arg-type]


def test_user_message_item_is_skipped() -> None:
    # The turn runner already mirrors ctx.prompt itself; re-emitting it here
    # would duplicate it (module docstring).
    item = _item(UserMessageThreadItem(id="item-1", content=[], type="userMessage"))

    assert thread_item_to_messages(item) == []


def test_agent_message_maps_to_text() -> None:
    item = _item(AgentMessageThreadItem(id="item-2", text="hello there", type="agentMessage"))

    messages = thread_item_to_messages(item)

    assert len(messages) == 1
    assert messages[0].role == "assistant"
    assert messages[0].kind == "text"
    assert messages[0].content == {"text": "hello there"}
    assert messages[0].native_id == "item-2"


def test_agent_message_with_empty_text_produces_nothing() -> None:
    item = _item(AgentMessageThreadItem(id="item-2b", text="", type="agentMessage"))

    assert thread_item_to_messages(item) == []


def test_reasoning_maps_to_thinking_using_summary() -> None:
    item = _item(
        ReasoningThreadItem(id="item-3", summary=["step one", "step two"], type="reasoning")
    )

    messages = thread_item_to_messages(item)

    assert len(messages) == 1
    assert messages[0].kind == "thinking"
    assert messages[0].content == {"text": "step one\nstep two"}
    assert messages[0].native_id == "item-3"


def test_reasoning_with_no_summary_or_content_produces_nothing() -> None:
    item = _item(ReasoningThreadItem(id="item-3b", summary=[], content=[], type="reasoning"))

    assert thread_item_to_messages(item) == []


def test_mcp_tool_call_completed_maps_to_tool_use_and_tool_result() -> None:
    item = _item(
        McpToolCallThreadItem(
            id="call-1",
            server="toolproxy",
            tool="my_tool",
            arguments={"x": "a"},
            status=McpToolCallStatus.completed,
            result=McpToolCallResult(content=[{"type": "text", "text": "ok"}]),
            type="mcpToolCall",
        )
    )

    messages = thread_item_to_messages(item)

    assert len(messages) == 2
    tool_use, tool_result = messages
    assert tool_use.kind == "tool_use"
    assert tool_use.content == {"id": "call-1", "name": "my_tool", "input": {"x": "a"}}
    assert tool_use.native_id == "call-1"
    assert tool_result.kind == "tool_result"
    assert tool_result.content == {"tool_use_id": "call-1", "content": "ok", "is_error": False}
    assert tool_result.native_id == "call-1"


def test_mcp_tool_call_failed_maps_error_message_into_tool_result() -> None:
    item = _item(
        McpToolCallThreadItem(
            id="call-2",
            server="toolproxy",
            tool="my_tool",
            arguments={},
            status=McpToolCallStatus.failed,
            error=McpToolCallError(message="user rejected MCP tool call"),
            type="mcpToolCall",
        )
    )

    messages = thread_item_to_messages(item)

    tool_result = messages[1]
    assert tool_result.content == {
        "tool_use_id": "call-2",
        "content": "user rejected MCP tool call",
        "is_error": True,
    }


def test_dynamic_tool_call_completed_maps_to_tool_use_and_tool_result() -> None:
    item = _item(
        DynamicToolCallThreadItem(
            id="dyn-1",
            tool="search",
            arguments={"q": "hi"},
            status=DynamicToolCallStatus.completed,
            content_items=[
                DynamicToolCallOutputContentItem(
                    InputTextDynamicToolCallOutputContentItem(text="found it", type="inputText")
                )
            ],
            type="dynamicToolCall",
        )
    )

    messages = thread_item_to_messages(item)

    tool_use, tool_result = messages
    assert tool_use.content == {"id": "dyn-1", "name": "search", "input": {"q": "hi"}}
    assert tool_result.content == {
        "tool_use_id": "dyn-1",
        "content": "found it",
        "is_error": False,
    }


def test_dynamic_tool_call_failed_is_error() -> None:
    item = _item(
        DynamicToolCallThreadItem(
            id="dyn-2",
            tool="search",
            arguments={},
            status=DynamicToolCallStatus.failed,
            content_items=None,
            type="dynamicToolCall",
        )
    )

    messages = thread_item_to_messages(item)

    tool_result = messages[1]
    assert tool_result.content == {"tool_use_id": "dyn-2", "content": "", "is_error": True}


def test_command_execution_maps_to_command_execution_kind() -> None:
    item = _item(
        CommandExecutionThreadItem(
            id="exec-1",
            command="touch foo.txt",
            command_actions=[
                CommandAction(UnknownCommandAction(command="touch foo.txt", type="unknown"))
            ],
            cwd=LegacyAppPathString("/workdir"),
            exit_code=0,
            aggregated_output="",
            status=CommandExecutionStatus.completed,
            type="commandExecution",
        )
    )

    messages = thread_item_to_messages(item)

    assert len(messages) == 1
    assert messages[0].kind == "command_execution"
    assert messages[0].content == {
        "command": "touch foo.txt",
        "cwd": "/workdir",
        "status": "completed",
        "exit_code": 0,
        "output": "",
    }
    assert messages[0].native_id == "exec-1"


def test_file_change_maps_to_file_change_kind() -> None:
    item = _item(
        FileChangeThreadItem(
            id="patch-1",
            changes=[
                FileUpdateChange(
                    path="foo.py", kind=PatchChangeKind(AddPatchChangeKind(type="add")), diff="+x"
                )
            ],
            status=PatchApplyStatus.completed,
            type="fileChange",
        )
    )

    messages = thread_item_to_messages(item)

    assert len(messages) == 1
    assert messages[0].kind == "file_change"
    assert messages[0].content == {
        "status": "completed",
        "changes": [{"path": "foo.py", "kind": "add", "diff": "+x"}],
    }
    assert messages[0].native_id == "patch-1"


def test_web_search_maps_to_web_search_kind() -> None:
    item = _item(WebSearchThreadItem(id="ws-1", query="weather today", type="webSearch"))

    messages = thread_item_to_messages(item)

    assert len(messages) == 1
    assert messages[0].kind == "web_search"
    assert messages[0].content == {"query": "weather today", "results": None}
    assert messages[0].native_id == "ws-1"


def test_unknown_item_kind_maps_to_event_with_raw() -> None:
    item = _item(PlanThreadItem(id="plan-1", text="do the thing", type="plan"))

    messages = thread_item_to_messages(item)

    assert len(messages) == 1
    assert messages[0].kind == "event"
    assert messages[0].content == {"type": "plan"}
    assert messages[0].native_id == "plan-1"
    assert messages[0].raw is not None
    assert messages[0].raw.get("id") == "plan-1"


# --- thread_read_items ----------------------------------------------------


def _thread(turns: list[Turn]) -> Thread:
    return Thread(
        id="thread-1",
        cli_version="0.147.0",
        created_at=0,
        cwd=AbsolutePathBuf("/workdir"),
        ephemeral=False,
        model_provider="openai",
        preview="hi",
        session_id="sess-1",
        source=SessionSource(SessionSourceValue.cli),
        status=ThreadStatus(IdleThreadStatus(type="idle")),
        turns=turns,
        updated_at=0,
    )


def test_thread_read_items_flattens_all_turns_and_maps_items() -> None:
    turn1 = Turn(
        id="turn-1",
        items=[_item(AgentMessageThreadItem(id="item-a", text="first", type="agentMessage"))],
        status=TurnStatus.completed,
    )
    turn2 = Turn(
        id="turn-2",
        items=[_item(AgentMessageThreadItem(id="item-b", text="second", type="agentMessage"))],
        status=TurnStatus.completed,
    )
    response = ThreadReadResponse(thread=_thread([turn1, turn2]))

    messages = thread_read_items(response, after_native_id=None)

    assert [m.content.get("text") for m in messages] == ["first", "second"]


def test_thread_read_items_after_native_id_drops_up_to_and_including_it() -> None:
    turn = Turn(
        id="turn-1",
        items=[
            _item(AgentMessageThreadItem(id="item-a", text="first", type="agentMessage")),
            _item(AgentMessageThreadItem(id="item-b", text="second", type="agentMessage")),
        ],
        status=TurnStatus.completed,
    )
    response = ThreadReadResponse(thread=_thread([turn]))

    messages = thread_read_items(response, after_native_id="item-a")

    assert [m.content.get("text") for m in messages] == ["second"]


def test_thread_read_items_after_native_id_not_found_drops_everything() -> None:
    # Deliberately the OPPOSITE of ClaudeBackend's own "not found -> nothing
    # dropped" rule (see thread_read_items's docstring): a cursor miss
    # happens on essentially every reconcile() call for Codex (live ids and
    # thread_read ids are different schemes, confirmed live), so returning
    # everything here would duplicate the mirror on every resumed turn.
    turn = Turn(
        id="turn-1",
        items=[_item(AgentMessageThreadItem(id="item-a", text="first", type="agentMessage"))],
        status=TurnStatus.completed,
    )
    response = ThreadReadResponse(thread=_thread([turn]))

    messages = thread_read_items(response, after_native_id="never-seen")

    assert messages == []


# --- _final_text_from_items -------------------------------------------


def test_final_text_prefers_final_answer_phase() -> None:
    messages = [
        AgentMessageThreadItem(
            id="a", text="commentary", phase=MessagePhase.commentary, type="agentMessage"
        ),
        AgentMessageThreadItem(
            id="b", text="the answer", phase=MessagePhase.final_answer, type="agentMessage"
        ),
    ]

    assert _final_text_from_items(messages) == "the answer"


def test_final_text_falls_back_to_last_phaseless_message() -> None:
    messages = [
        AgentMessageThreadItem(id="a", text="only message", phase=None, type="agentMessage"),
    ]

    assert _final_text_from_items(messages) == "only message"


def test_final_text_of_no_messages_is_none() -> None:
    assert _final_text_from_items([]) is None


# --- _usage_dict ------------------------------------------------------


def test_usage_dict_reads_the_total_breakdown() -> None:
    usage = ThreadTokenUsage(
        last=TokenUsageBreakdown(
            cached_input_tokens=1,
            input_tokens=2,
            output_tokens=3,
            reasoning_output_tokens=4,
            total_tokens=10,
        ),
        total=TokenUsageBreakdown(
            cached_input_tokens=5,
            input_tokens=6,
            output_tokens=7,
            reasoning_output_tokens=8,
            total_tokens=26,
        ),
    )

    assert _usage_dict(usage) == {
        "input_tokens": 6,
        "cached_input_tokens": 5,
        "output_tokens": 7,
        "reasoning_output_tokens": 8,
        "total_tokens": 26,
    }


# --- mcp_server_config_overrides ---------------------------------------


def test_mcp_server_config_overrides_builds_toml_assignments() -> None:
    server = McpServerDef(
        name="toolproxy",
        transport="stdio",
        command=["/usr/bin/python3", "-m", "tradewind.toolproxy"],
        env={"TRADEWIND_TOOL_SOCKET": "/tmp/x.sock"},
    )

    overrides = mcp_server_config_overrides(server)

    assert overrides == (
        'mcp_servers.toolproxy.command="/usr/bin/python3"',
        'mcp_servers.toolproxy.args=["-m", "tradewind.toolproxy"]',
        'mcp_servers.toolproxy.env={ TRADEWIND_TOOL_SOCKET = "/tmp/x.sock" }',
    )


def test_mcp_server_config_overrides_omits_env_when_empty() -> None:
    server = McpServerDef(
        name="toolproxy", transport="stdio", command=["python3", "-m", "tradewind.toolproxy"]
    )

    overrides = mcp_server_config_overrides(server)

    assert len(overrides) == 2
    assert not any("env" in override for override in overrides)


def test_mcp_server_config_overrides_rejects_non_stdio_transport() -> None:
    server = McpServerDef(name="http-server", transport="http", url="https://example.com")

    with pytest.raises(ConfigError):
        mcp_server_config_overrides(server)
