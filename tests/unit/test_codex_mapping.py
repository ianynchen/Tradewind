"""Pure mapping tests for `tradewind.adapters.codex_backend`: `ThreadItem` ->
`NormalizedMessage` (task-14 brief). SDK dataclasses are constructed directly
(no live `codex app-server`) -- a real session is
`tests/conformance/test_codex.py` (`@pytest.mark.integration`).
"""

from __future__ import annotations

from typing import Any

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
    _denied_shim_permission_event,
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
    assert messages[0].content == {
        "query": "weather today",
        "results": None,
        "action": None,
        "queries": None,
    }
    assert messages[0].native_id == "ws-1"


@pytest.mark.parametrize(
    ("action", "query", "expected_action", "expected_queries"),
    [
        (
            {"type": "search", "query": "site:python.org latest", "queries": None},
            "site:python.org latest",
            {"type": "search", "query": "site:python.org latest", "queries": None},
            None,
        ),
        (
            {"type": "search", "query": None, "queries": ["a", "b"]},
            "a ...",
            {"type": "search", "query": None, "queries": ["a", "b"]},
            ["a", "b"],
        ),
        (
            {"type": "openPage", "url": "https://example.com"},
            "https://example.com",
            {"type": "openPage", "url": "https://example.com"},
            None,
        ),
        (
            {"type": "findInPage", "url": "https://example.com", "pattern": "x"},
            "x",
            {"type": "findInPage", "url": "https://example.com", "pattern": "x"},
            None,
        ),
    ],
)
def test_web_search_records_action_and_queries(
    action: dict[str, Any],
    query: str,
    expected_action: dict[str, Any],
    expected_queries: list[str] | None,
) -> None:
    item = _item(
        WebSearchThreadItem.model_validate(
            {"id": "ws-2", "type": "webSearch", "query": query, "action": action}
        )
    )

    (message,) = thread_item_to_messages(item)

    assert message.content == {
        "query": query,
        "results": None,
        "action": expected_action,
        "queries": expected_queries,
    }


def test_unknown_item_kind_maps_to_event_with_raw() -> None:
    item = _item(PlanThreadItem(id="plan-1", text="do the thing", type="plan"))

    messages = thread_item_to_messages(item)

    assert len(messages) == 1
    assert messages[0].kind == "event"
    assert messages[0].content == {"type": "plan"}
    assert messages[0].native_id == "plan-1"
    assert messages[0].raw is not None
    assert messages[0].raw.get("id") == "plan-1"


# --- _denied_shim_permission_event (task-14 fix round 1: FR-4.1) --------
#
# `ToolHost.call()`'s own broker gate (`tool_host.py`) denies tradewind's
# tool calls made through the shim server -- the approval_handler layer
# auto-accepts those and never learns about a deny (module docstring), so
# this function is what surfaces the required `PermissionRequested` event,
# recognizing `ToolHost.call()`'s own "permission denied" sentinel on a
# completed (not `completed`-status) mcpToolCall/dynamicToolCall item.


def test_denied_mcp_tool_call_produces_permission_requested() -> None:
    item = _item(
        McpToolCallThreadItem(
            id="call-3",
            server="toolproxy",
            tool="denied_tool",
            arguments={"x": "b"},
            status=McpToolCallStatus.failed,
            result=McpToolCallResult(content=[{"type": "text", "text": "permission denied"}]),
            type="mcpToolCall",
        )
    )

    event = _denied_shim_permission_event(item)

    assert event is not None
    assert event.tool_name == "denied_tool"
    assert event.tool_input == {"x": "b"}
    assert event.verdict == "deny"


def test_denied_mcp_tool_call_via_error_field_produces_permission_requested() -> None:
    item = _item(
        McpToolCallThreadItem(
            id="call-4",
            server="toolproxy",
            tool="denied_tool",
            arguments={},
            status=McpToolCallStatus.failed,
            error=McpToolCallError(message="permission denied"),
            type="mcpToolCall",
        )
    )

    event = _denied_shim_permission_event(item)

    assert event is not None
    assert event.tool_name == "denied_tool"


def test_denied_dynamic_tool_call_produces_permission_requested() -> None:
    item = _item(
        DynamicToolCallThreadItem(
            id="dyn-3",
            tool="denied_tool",
            arguments={"x": "b"},
            status=DynamicToolCallStatus.failed,
            content_items=[
                DynamicToolCallOutputContentItem(
                    InputTextDynamicToolCallOutputContentItem(
                        text="permission denied", type="inputText"
                    )
                )
            ],
            type="dynamicToolCall",
        )
    )

    event = _denied_shim_permission_event(item)

    assert event is not None
    assert event.tool_name == "denied_tool"
    assert event.verdict == "deny"


def test_completed_mcp_tool_call_produces_no_permission_event() -> None:
    item = _item(
        McpToolCallThreadItem(
            id="call-5",
            server="toolproxy",
            tool="allowed_tool",
            arguments={"x": "a"},
            status=McpToolCallStatus.completed,
            result=McpToolCallResult(content=[{"type": "text", "text": "ok"}]),
            type="mcpToolCall",
        )
    )

    assert _denied_shim_permission_event(item) is None


def test_failed_mcp_tool_call_with_a_different_error_produces_no_permission_event() -> None:
    # A real tool failure (not a broker deny) must not be misread as one --
    # only the exact "permission denied" sentinel triggers this.
    item = _item(
        McpToolCallThreadItem(
            id="call-6",
            server="toolproxy",
            tool="allowed_tool",
            arguments={},
            status=McpToolCallStatus.failed,
            error=McpToolCallError(message="connection timed out"),
            type="mcpToolCall",
        )
    )

    assert _denied_shim_permission_event(item) is None


def test_denied_mcp_tool_call_from_a_non_shim_server_produces_no_permission_event() -> None:
    # Item-6a fix: `_DENIED_TOOL_RESULT_CONTENT` is a plain, unnamespaced
    # sentinel -- some OTHER MCP server's own genuine failure could
    # coincidentally produce the exact same text. Only tradewind's own shim
    # server (`ToolHost.call()`'s broker gate) ever means an actual deny
    # here, so a different `server` must not synthesize the event.
    item = _item(
        McpToolCallThreadItem(
            id="call-7",
            server="some-other-mcp-server",
            tool="denied_tool",
            arguments={"x": "b"},
            status=McpToolCallStatus.failed,
            result=McpToolCallResult(content=[{"type": "text", "text": "permission denied"}]),
            type="mcpToolCall",
        )
    )

    assert _denied_shim_permission_event(item) is None


def test_non_tool_call_item_produces_no_permission_event() -> None:
    item = _item(AgentMessageThreadItem(id="item-9", text="hi", type="agentMessage"))

    assert _denied_shim_permission_event(item) is None


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


# --- turn governance: action mapping (05-turn-governance §1, §7 Codex row)


def test_shell_action_keeps_the_command_as_its_critical_field() -> None:
    from tradewind.adapters.codex_backend import shell_action

    action = shell_action({"command": "git status", "cwd": "/w", "itemId": "exec-1"})

    assert action.kind == "shell"
    assert action.fields["command"] == "git status"
    assert action.fields["cwd"] == "/w"
    assert action.backend == "codex"
    assert action.native_name == "shell"
    assert action.missing_critical_field is False


def test_shell_action_without_a_command_is_missing_its_critical_field() -> None:
    """Owner M8: a known kind without its critical field is denied by the
    floor -- the adapter only marks it."""
    from tradewind.adapters.codex_backend import shell_action

    assert shell_action({"cwd": "/w"}).missing_critical_field is True


def test_file_write_action_lists_each_change_with_its_workspace_flag() -> None:
    from tradewind.adapters.codex_backend import file_write_action

    changes = [
        FileUpdateChange(
            path="a.py", kind=PatchChangeKind(AddPatchChangeKind(type="add")), diff=""
        ),
        FileUpdateChange(
            path="/w/src/b.py", kind=PatchChangeKind(AddPatchChangeKind(type="add")), diff=""
        ),
        FileUpdateChange(
            path="/etc/hosts", kind=PatchChangeKind(AddPatchChangeKind(type="add")), diff=""
        ),
    ]

    action = file_write_action(changes, workspace="/w", native_args={"changes": None})

    assert action.kind == "file_write"
    assert action.native_name == "apply_patch"
    assert action.fields["changes"] == [
        {"path": "a.py", "change": "add", "in_workspace": True},
        {"path": "/w/src/b.py", "change": "add", "in_workspace": True},
        {"path": "/etc/hosts", "change": "add", "in_workspace": False},
    ]
    assert action.missing_critical_field is False


def test_file_write_action_with_unknown_changes_is_missing_its_critical_field() -> None:
    from tradewind.adapters.codex_backend import file_write_action

    action = file_write_action(None, workspace="/w", native_args={"changes": None})

    assert action.missing_critical_field is True


def test_observed_actions_cover_commands_file_changes_and_web() -> None:
    """05 §7, 07 §4: observe sees completed command, file-change and web items."""
    from tradewind.adapters.codex_backend import observed_actions

    command = _item(
        CommandExecutionThreadItem(
            id="exec-1",
            command="ls",
            command_actions=[],
            cwd=LegacyAppPathString("/w"),
            status=CommandExecutionStatus.completed,
            type="commandExecution",
        )
    )
    patch = _item(
        FileChangeThreadItem(
            id="patch-1",
            changes=[
                FileUpdateChange(
                    path="a.py", kind=PatchChangeKind(AddPatchChangeKind(type="add")), diff=""
                )
            ],
            status=PatchApplyStatus.completed,
            type="fileChange",
        )
    )
    text = _item(AgentMessageThreadItem(id="m-1", type="agentMessage", text="hi"))

    (shell,) = observed_actions(command, workspace="/w")
    (write,) = observed_actions(patch, workspace="/w")

    assert shell.kind == "shell"
    assert shell.fields["command"] == "ls"
    assert write.kind == "file_write"
    assert observed_actions(text, workspace="/w") == []


def _web_item(action: dict[str, Any] | None) -> Any:
    return _item(
        WebSearchThreadItem.model_validate(
            {"id": "ws-9", "type": "webSearch", "query": "q", "action": action}
        )
    )


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ({"type": "search", "query": "a"}, [("web_search", "query", "a")]),
        (
            {"type": "search", "queries": ["a", "b"]},
            [("web_search", "query", "a"), ("web_search", "query", "b")],
        ),
        (
            {"type": "search", "query": "a", "queries": ["x", "y"]},
            [
                ("web_search", "query", "a"),
                ("web_search", "query", "x"),
                ("web_search", "query", "y"),
            ],
        ),
        (
            {"type": "search", "query": "a", "queries": ["a", "b"]},
            [("web_search", "query", "a"), ("web_search", "query", "b")],
        ),
        ({"type": "openPage", "url": "https://e.com"}, [("fetch_url", "url", "https://e.com")]),
        (
            {"type": "findInPage", "url": "https://e.com", "pattern": "p"},
            [("fetch_url", "url", "https://e.com")],
        ),
    ],
)
def test_observed_actions_map_web_items(
    action: dict[str, Any], expected: list[tuple[str, str, str]]
) -> None:
    from tradewind.adapters.codex_backend import observed_actions

    actions = observed_actions(_web_item(action), workspace=None)

    assert [(a.kind, k, a.fields[k]) for a in actions for k in a.fields] == expected
    for a in actions:
        assert a.backend == "codex"
        assert a.native_name == "webSearch"
        assert a.native_args.items() >= action.items()
        assert a.summary == next(iter(a.fields.values()))
        assert a.missing_critical_field is False


@pytest.mark.parametrize(
    ("action", "kind"),
    [
        (None, "web_search"),
        ({"type": "search"}, "web_search"),
        ({"type": "search", "queries": []}, "web_search"),
        ({"type": "openPage"}, "fetch_url"),
        ({"type": "findInPage", "pattern": "p"}, "fetch_url"),
        ({"type": "other"}, "web_search"),
    ],
)
def test_observed_actions_web_item_missing_critical_field(
    action: dict[str, Any] | None, kind: str
) -> None:
    from tradewind.adapters.codex_backend import observed_actions

    (only,) = observed_actions(_web_item(action), workspace=None)

    assert only.kind == kind
    assert only.missing_critical_field is True
    assert only.native_name == "webSearch"
    assert only.summary == (action["type"] if action else "webSearch")


@pytest.mark.parametrize(
    ("path", "workspace", "inside"),
    [
        ("a.py", "/w", True),
        ("src/../a.py", "/w", True),
        ("../x", "/w", False),
        ("a/../../x", "/w", False),
        ("../../etc/passwd", "/w", False),
        ("/w/src/b.py", "/w", True),
        ("/w/../etc/hosts", "/w", False),
        ("/wx/b.py", "/w", False),
        ("/etc/hosts", "/w", False),
        ("a.py", None, False),
        ("/w/a.py", None, False),
    ],
)
def test_in_workspace_resolves_relative_paths_before_the_check(
    path: str, workspace: str | None, inside: bool
) -> None:
    """`in_workspace` is a security field: a caller rule `file_write where
    in_workspace is_false -> deny` must not be bypassed by `..` in a
    relative path, and an unknown workspace is never "inside"."""
    from tradewind.adapters.codex_backend import file_write_action

    change = FileUpdateChange(
        path=path, kind=PatchChangeKind(AddPatchChangeKind(type="add")), diff=""
    )

    action = file_write_action([change], workspace=workspace, native_args={})

    assert action.fields["changes"] == [{"path": path, "change": "add", "in_workspace": inside}]
