"""Tests for tradewind.application.tool_host.ToolHost: in-process dispatch,
MCP-server proxying, schema listing, and `ref:` secret resolution at connect
time (task-7 brief).
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any

import anyio
import pytest
from anyio.streams.buffered import BufferedByteReceiveStream
from mcp.client._memory import InMemoryTransport
from mcp.server.mcpserver import MCPServer

from tradewind.application.tool_host import RESULT_TOOL_NAME, ToolHost, ToolOutcome
from tradewind.domain.errors import ConfigError
from tradewind.domain.models import McpServerDef, Tool
from tradewind.toolproxy import protocol


def _resolve_ref_raises(_ref: str) -> str:
    raise AssertionError("resolve_ref should not be called when no MCP servers are configured")


# --- local tools: happy path dispatches to handler(**arguments) (Step 1) ---


async def test_call_local_tool_dispatches_to_handler_with_kwargs() -> None:
    async def handler(*, x: int, y: int) -> str:
        return f"sum={x + y}"

    tool = Tool(
        name="add", description="add two numbers", input_schema={"type": "object"}, handler=handler
    )
    host = ToolHost([tool], [], _resolve_ref_raises)

    outcome = await host.call("add", {"x": 1, "y": 2})

    assert outcome == ToolOutcome(content="sum=3", is_error=False)


async def test_call_local_tool_serializes_non_string_result_as_json() -> None:
    async def handler(**_: object) -> dict[str, object]:
        return {"ok": True}

    tool = Tool(
        name="status", description="status", input_schema={"type": "object"}, handler=handler
    )
    host = ToolHost([tool], [], _resolve_ref_raises)

    outcome = await host.call("status", {})

    assert outcome.is_error is False
    assert '"ok": true' in outcome.content


# --- local tools: handler exception is caught, never raised (Step 1) ---


async def test_call_local_tool_handler_exception_becomes_error_outcome() -> None:
    async def handler(**_: object) -> str:
        raise ValueError("boom")

    tool = Tool(
        name="broken", description="always fails", input_schema={"type": "object"}, handler=handler
    )
    host = ToolHost([tool], [], _resolve_ref_raises)

    outcome = await host.call("broken", {})

    assert outcome == ToolOutcome(content="boom", is_error=True)


# --- unknown tool name (Step 1) ---


async def test_call_unknown_tool_returns_error_outcome_without_raising() -> None:
    host = ToolHost([], [], _resolve_ref_raises)

    outcome = await host.call("does-not-exist", {})

    assert outcome.is_error is True


# --- schemas(): anthropic-format dicts for local tools (Step 1) ---


def test_schemas_lists_local_tools_in_anthropic_format() -> None:
    async def handler(**_: object) -> str:
        return "ok"

    tool = Tool(
        name="search",
        description="search the web",
        input_schema={"type": "object", "properties": {}},
        handler=handler,
    )
    host = ToolHost([tool], [], _resolve_ref_raises)

    assert host.schemas() == [
        {
            "name": "search",
            "description": "search the web",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]


# --- MCP servers: real in-memory client/server round trip (Step 1) ---
#
# `McpServerDef.transport` is a closed Literal["stdio", "http"] (domain
# model), so there is no third "in-memory" transport to select through the
# public constructor. Rather than add a test-only seam to ToolHost itself,
# these tests monkeypatch `tool_host.stdio_client` — the one call ToolHost
# makes to obtain a stdio transport — to return an `InMemoryTransport` wired
# to a real `mcp.server.mcpserver.MCPServer`. Everything downstream (the real
# `mcp.ClientSession`, real JSON-RPC message framing, real tool
# listing/dispatch) is exercised unmodified; only the subprocess spawn is
# swapped out, which is exactly the seam a stdio transport is supposed to
# hide from callers.


def _fake_mcp_server() -> MCPServer:
    server: MCPServer = MCPServer("fake-mcp")

    @server.tool()
    async def echo(text: str) -> str:
        """Echo `text` back, prefixed."""
        return f"echo:{text}"

    return server


def _patch_stdio_client(monkeypatch: pytest.MonkeyPatch, server: MCPServer) -> None:
    def fake_stdio_client(_params: object) -> AbstractAsyncContextManager[Any]:
        return InMemoryTransport(server)

    monkeypatch.setattr("tradewind.application.tool_host.stdio_client", fake_stdio_client)


def _mcp_server_def(name: str = "fake") -> McpServerDef:
    return McpServerDef(name=name, transport="stdio", command=["fake-mcp-command"])


async def test_mcp_tool_round_trip_lists_and_calls_namespaced_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_stdio_client(monkeypatch, _fake_mcp_server())
    host = ToolHost([], [_mcp_server_def()], _resolve_ref_raises)

    async with host:
        assert host.schemas() == [
            {
                "name": "mcp__fake__echo",
                "description": "Echo `text` back, prefixed.",
                "input_schema": {
                    "type": "object",
                    "properties": {"text": {"title": "Text", "type": "string"}},
                    "required": ["text"],
                    "title": "echoArguments",
                },
            }
        ]

        outcome = await host.call("mcp__fake__echo", {"text": "hi"})

    assert outcome == ToolOutcome(content="echo:hi", is_error=False)


async def test_mcp_tool_call_before_connect_is_unknown_tool() -> None:
    host = ToolHost([], [_mcp_server_def()], _resolve_ref_raises)

    outcome = await host.call("mcp__fake__echo", {"text": "hi"})

    assert outcome.is_error is True


async def test_aexit_disconnects_mcp_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_stdio_client(monkeypatch, _fake_mcp_server())
    host = ToolHost([], [_mcp_server_def()], _resolve_ref_raises)

    async with host:
        assert host.schemas() != []

    assert host.schemas() == []
    outcome = await host.call("mcp__fake__echo", {"text": "hi"})
    assert outcome.is_error is True


# --- resolve_ref: expands "ref:" env values at connect time only (Step 1) ---


async def test_resolve_ref_is_called_for_ref_prefixed_env_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def resolve_ref(ref: str) -> str:
        calls.append(ref)
        return "super-secret-value"

    server_def = McpServerDef(
        name="fake",
        transport="stdio",
        command=["fake-mcp-command"],
        env={"API_KEY": "ref:api_key"},
    )

    seen_env: dict[str, str] = {}

    def fake_stdio_client(params: Any) -> AbstractAsyncContextManager[Any]:
        seen_env.update(params.env)
        return InMemoryTransport(_fake_mcp_server())

    monkeypatch.setattr("tradewind.application.tool_host.stdio_client", fake_stdio_client)

    host = ToolHost([], [server_def], resolve_ref)
    async with host:
        pass

    assert calls == ["ref:api_key"]
    assert seen_env["API_KEY"] == "super-secret-value"


async def test_resolved_ref_value_never_appears_in_schemas(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve_ref(_ref: str) -> str:
        return "super-secret-value"

    server_def = McpServerDef(
        name="fake",
        transport="stdio",
        command=["fake-mcp-command"],
        env={"API_KEY": "ref:api_key"},
    )
    _patch_stdio_client(monkeypatch, _fake_mcp_server())

    host = ToolHost([], [server_def], resolve_ref)
    async with host:
        schemas_repr = repr(host.schemas())

    assert "super-secret-value" not in schemas_repr


async def test_non_ref_env_value_passed_through_without_calling_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def resolve_ref(ref: str) -> str:
        calls.append(ref)
        return "should-not-be-used"

    server_def = McpServerDef(
        name="fake",
        transport="stdio",
        command=["fake-mcp-command"],
        env={"PLAIN": "literal-value"},
    )

    seen_env: dict[str, str] = {}

    def fake_stdio_client(params: Any) -> AbstractAsyncContextManager[Any]:
        seen_env.update(params.env)
        return InMemoryTransport(_fake_mcp_server())

    monkeypatch.setattr("tradewind.application.tool_host.stdio_client", fake_stdio_client)

    host = ToolHost([], [server_def], resolve_ref)
    async with host:
        pass

    assert calls == []
    assert seen_env["PLAIN"] == "literal-value"


# --- HTTP transport: mirrors the stdio coverage above (Fix round 1) ---
#
# Same rationale as the stdio tests: `_connect`'s http branch calls
# `create_mcp_http_client(headers=...)` then `streamable_http_client(url,
# http_client=...)` (both re-exported names on `tool_host`, patched here) to
# get a `TransportStreams` pair; swapping that pair for an `InMemoryTransport`
# wired to a real `MCPServer` exercises the same real `mcp.ClientSession`
# machinery as the stdio tests, just entered through the http branch.


class _FakeHttpClient:
    """Stand-in for `httpx2.AsyncClient`: only needs to be usable as an async
    context manager (`_connect` enters it via the exit stack) and to carry
    the `headers` it was constructed with, for assertions."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers

    async def __aenter__(self) -> _FakeHttpClient:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        return None


def _patch_streamable_http_client(
    monkeypatch: pytest.MonkeyPatch, server: MCPServer
) -> dict[str, Any]:
    """Patch both calls `_connect`'s http branch makes, returning a dict that
    accumulates what each was called with (`headers`, `url`, `http_client`)
    for assertions."""
    captured: dict[str, Any] = {}

    def fake_create_mcp_http_client(
        headers: dict[str, str] | None = None, **_kwargs: object
    ) -> _FakeHttpClient:
        captured["headers"] = headers or {}
        return _FakeHttpClient(headers or {})

    def fake_streamable_http_client(
        url: str, *, http_client: object = None, **_kwargs: object
    ) -> AbstractAsyncContextManager[Any]:
        captured["url"] = url
        captured["http_client"] = http_client
        return InMemoryTransport(server)

    monkeypatch.setattr(
        "tradewind.application.tool_host.create_mcp_http_client", fake_create_mcp_http_client
    )
    monkeypatch.setattr(
        "tradewind.application.tool_host.streamable_http_client", fake_streamable_http_client
    )
    return captured


def _http_server_def(name: str = "fake-http") -> McpServerDef:
    return McpServerDef(name=name, transport="http", url="http://fake-mcp.invalid/mcp")


async def test_http_tool_round_trip_lists_and_calls_namespaced_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_streamable_http_client(monkeypatch, _fake_mcp_server())
    host = ToolHost([], [_http_server_def()], _resolve_ref_raises)

    async with host:
        assert host.schemas() == [
            {
                "name": "mcp__fake-http__echo",
                "description": "Echo `text` back, prefixed.",
                "input_schema": {
                    "type": "object",
                    "properties": {"text": {"title": "Text", "type": "string"}},
                    "required": ["text"],
                    "title": "echoArguments",
                },
            }
        ]

        outcome = await host.call("mcp__fake-http__echo", {"text": "hi"})

    assert outcome == ToolOutcome(content="echo:hi", is_error=False)


async def test_http_resolve_ref_is_called_for_ref_prefixed_header_and_reaches_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def resolve_ref(ref: str) -> str:
        calls.append(ref)
        return "super-secret-value"

    server_def = McpServerDef(
        name="fake-http",
        transport="http",
        url="http://fake-mcp.invalid/mcp",
        headers={"Authorization": "ref:api_key"},
    )
    captured = _patch_streamable_http_client(monkeypatch, _fake_mcp_server())

    host = ToolHost([], [server_def], resolve_ref)
    async with host:
        pass

    assert calls == ["ref:api_key"]
    # (c) the resolved header value reaches the http-client constructor.
    assert captured["headers"] == {"Authorization": "super-secret-value"}


async def test_http_resolved_ref_value_never_appears_in_schemas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def resolve_ref(_ref: str) -> str:
        return "super-secret-value"

    server_def = McpServerDef(
        name="fake-http",
        transport="http",
        url="http://fake-mcp.invalid/mcp",
        headers={"Authorization": "ref:api_key"},
    )
    _patch_streamable_http_client(monkeypatch, _fake_mcp_server())

    host = ToolHost([], [server_def], resolve_ref)
    async with host:
        schemas_repr = repr(host.schemas())

    assert "super-secret-value" not in schemas_repr


async def test_http_non_ref_header_value_passed_through_without_calling_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def resolve_ref(ref: str) -> str:
        calls.append(ref)
        return "should-not-be-used"

    server_def = McpServerDef(
        name="fake-http",
        transport="http",
        url="http://fake-mcp.invalid/mcp",
        headers={"X-Plain": "literal-value"},
    )
    captured = _patch_streamable_http_client(monkeypatch, _fake_mcp_server())

    host = ToolHost([], [server_def], resolve_ref)
    async with host:
        pass

    assert calls == []
    assert captured["headers"] == {"X-Plain": "literal-value"}


# --- __aenter__ partial-failure cleanup (Fix round 1, Minor) ---


async def test_aenter_failure_on_second_server_leaves_host_with_no_mcp_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_stdio_client(monkeypatch, _fake_mcp_server())

    def fake_streamable_http_client_raises(
        _url: str,
        *,
        http_client: object = None,  # noqa: ARG001 -- kept to match the real call's `http_client=` kwarg
        **_kwargs: object,
    ) -> AbstractAsyncContextManager[Any]:
        raise ConnectionError("second server unreachable")

    monkeypatch.setattr(
        "tradewind.application.tool_host.create_mcp_http_client",
        lambda headers=None, **_kwargs: _FakeHttpClient(headers or {}),
    )
    monkeypatch.setattr(
        "tradewind.application.tool_host.streamable_http_client",
        fake_streamable_http_client_raises,
    )

    host = ToolHost([], [_mcp_server_def("ok"), _http_server_def("broken")], _resolve_ref_raises)

    with pytest.raises(ConnectionError):
        await host.__aenter__()

    assert host.schemas() == []
    outcome = await host.call("mcp__ok__echo", {"text": "hi"})
    assert outcome.is_error is True


# --- FR-4.4: the socket-path broker honors a Denial's reason (terminate is
# a recorded limitation on this path -- no handle to interrupt from here) ---


async def test_denial_reason_becomes_the_error_result_on_the_socket_path() -> None:
    from tradewind.domain.models import Denial

    class _Broker:
        async def decide(self, _tool_name: str, _tool_input: dict[str, object]) -> object:
            return Denial(reason="quota exhausted for this tool")

    async def handler(**_: object) -> str:
        raise AssertionError("denied tool must not run")

    host = ToolHost(
        [Tool(name="t", description="d", input_schema={"type": "object"}, handler=handler)],
        [],
        lambda ref: ref,
        broker=_Broker(),  # type: ignore[arg-type]
    )

    outcome = await host.call("t", {})

    assert outcome.is_error is True
    assert outcome.content == "quota exhausted for this tool"


# --- 05 §6: the result-tool seam (`tradewind_submit_result`) ---

_RESULT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"answer": {"type": "integer"}},
    "required": ["answer"],
}


class _BrokerMustNotBeAsked:
    async def decide(self, tool_name: str, _tool_input: dict[str, object]) -> object:
        raise AssertionError(f"the result tool is a structural pass-through; asked for {tool_name}")


def _result_host() -> ToolHost:
    return ToolHost(
        [],
        [],
        _resolve_ref_raises,
        broker=_BrokerMustNotBeAsked(),  # type: ignore[arg-type]
        result_schema=_RESULT_SCHEMA,
    )


def test_result_tool_is_listed_with_the_callers_schema_verbatim() -> None:
    host = _result_host()

    [schema] = host.schemas()

    assert schema["name"] == RESULT_TOOL_NAME == "tradewind_submit_result"
    assert schema["input_schema"] == _RESULT_SCHEMA


async def test_valid_result_is_recorded_without_asking_the_broker() -> None:
    host = _result_host()

    outcome = await host.call(RESULT_TOOL_NAME, {"answer": 42})

    assert outcome == ToolOutcome(content="Result recorded.", is_error=False)
    assert host.result_slot.last_valid == {"answer": 42}
    assert host.result_slot.attempts == 1


async def test_invalid_result_answers_the_schema_errors_then_a_valid_one_is_recorded() -> None:
    host = _result_host()

    invalid = await host.call(RESULT_TOOL_NAME, {"answer": "not a number"})
    valid = await host.call(RESULT_TOOL_NAME, {"answer": 7})

    assert invalid.is_error is True
    assert "'not a number' is not of type 'integer'" in invalid.content
    assert valid.content == "Result recorded."
    assert host.result_slot.last_valid == {"answer": 7}
    assert host.result_slot.attempts == 2


async def test_invalid_result_after_a_valid_one_keeps_the_last_valid() -> None:
    host = _result_host()

    await host.call(RESULT_TOOL_NAME, {"answer": 1})
    await host.call(RESULT_TOOL_NAME, {"answer": 2})
    await host.call(RESULT_TOOL_NAME, {})

    assert host.result_slot.last_valid == {"answer": 2}


async def test_fourth_result_call_is_refused_and_not_recorded() -> None:
    # Why: a bounded number of attempts stops a model looping on the tool.
    host = _result_host()
    for value in (1, 2, 3):
        await host.call(RESULT_TOOL_NAME, {"answer": value})

    fourth = await host.call(RESULT_TOOL_NAME, {"answer": 4})

    assert fourth == ToolOutcome(content="No more attempts.", is_error=True)
    assert host.result_slot.last_valid == {"answer": 3}
    assert host.result_slot.attempts == 3


async def test_clear_resets_the_result_slot() -> None:
    host = _result_host()
    for value in (1, 2, 3):
        await host.call(RESULT_TOOL_NAME, {"answer": value})

    host.result_slot.clear()

    assert host.result_slot.attempts == 0
    assert host.result_slot.last_valid is None
    assert (await host.call(RESULT_TOOL_NAME, {"answer": 5})).content == "Result recorded."


def test_caller_tool_named_like_the_result_tool_is_a_config_error() -> None:
    async def handler(**_: object) -> str:
        return "x"

    tool = Tool(
        name=RESULT_TOOL_NAME, description="d", input_schema={"type": "object"}, handler=handler
    )

    with pytest.raises(ConfigError, match=RESULT_TOOL_NAME):
        ToolHost([tool], [], _resolve_ref_raises, result_schema=_RESULT_SCHEMA)


def test_invalid_result_schema_is_a_config_error_at_construction() -> None:
    with pytest.raises(ConfigError):
        ToolHost([], [], _resolve_ref_raises, result_schema={"type": "not-a-type"})


async def test_without_result_schema_the_result_tool_is_unknown() -> None:
    host = ToolHost([], [], _resolve_ref_raises)

    outcome = await host.call(RESULT_TOOL_NAME, {"answer": 1})

    assert host.schemas() == []
    assert outcome.is_error is True
    assert host.result_slot.attempts == 0


# --- 05 "Port and interface changes": abandoned shim calls are cancelled (a
# late "ask" answer must not run a call the engine gave up on) ---


async def test_abandoned_socket_call_cancels_the_pending_broker_question() -> None:
    asked = anyio.Event()
    cancelled = anyio.Event()
    handler_ran = False

    class _BlockingBroker:
        async def decide(self, _tool_name: str, _tool_input: dict[str, object]) -> object:
            asked.set()
            try:
                await anyio.sleep_forever()
            except anyio.get_cancelled_exc_class():
                cancelled.set()
                raise
            return "allow"

    async def handler(**_: object) -> str:
        nonlocal handler_ran
        handler_ran = True
        return "ran"

    host = ToolHost(
        [Tool(name="t", description="d", input_schema={"type": "object"}, handler=handler)],
        [],
        _resolve_ref_raises,
        broker=_BlockingBroker(),  # type: ignore[arg-type]
    )
    socket_path = await host.serve_socket()
    try:
        stream = await anyio.connect_unix(socket_path)
        request = protocol.CallRequest(name="t", arguments={})
        await stream.send((protocol.encode_request(request) + "\n").encode("utf-8"))
        with anyio.fail_after(5):
            await asked.wait()
        await stream.aclose()

        with anyio.fail_after(5):
            await cancelled.wait()
        assert handler_ran is False
    finally:
        await host.stop_socket()


async def test_socket_call_still_answers_when_the_client_waits() -> None:
    async def handler(**_: object) -> str:
        return "ok"

    host = ToolHost(
        [Tool(name="t", description="d", input_schema={"type": "object"}, handler=handler)],
        [],
        _resolve_ref_raises,
    )
    socket_path = await host.serve_socket()
    try:
        stream = await anyio.connect_unix(socket_path)
        async with stream:
            first = protocol.CallRequest(name="t", arguments={})
            # Two lines in one write: the second must not be lost while the
            # first call is watched for an abandoned connection.
            line = (protocol.encode_request(first) + "\n").encode("utf-8")
            await stream.send(line + line)
            buffered = BufferedByteReceiveStream(stream)
            with anyio.fail_after(5):
                replies = [
                    protocol.decode_response(
                        (await buffered.receive_until(b"\n", protocol.MAX_LINE_BYTES)).decode()
                    )
                    for _ in range(2)
                ]
        assert replies == [protocol.CallResponse(content="ok", is_error=False)] * 2
    finally:
        await host.stop_socket()


async def _socket_call(socket_path: Any, request: protocol.Request) -> protocol.Response:
    stream = await anyio.connect_unix(socket_path)
    async with stream:
        await stream.send((protocol.encode_request(request) + "\n").encode("utf-8"))
        buffered = BufferedByteReceiveStream(stream)
        line = await buffered.receive_until(b"\n", protocol.MAX_LINE_BYTES)
        return protocol.decode_response(line.decode("utf-8"))


async def test_abandoned_socket_call_lets_an_already_running_handler_finish() -> None:
    # Why: an approved action (e.g. a merge) must not stop half-way because
    # the engine gave up waiting for its reply.
    started = anyio.Event()
    release = anyio.Event()
    finished = anyio.Event()
    side_effects = 0

    async def handler(**_: object) -> str:
        nonlocal side_effects
        started.set()
        await release.wait()
        side_effects += 1
        finished.set()
        return "merged"

    host = ToolHost(
        [Tool(name="merge", description="d", input_schema={"type": "object"}, handler=handler)],
        [],
        _resolve_ref_raises,
    )
    socket_path = await host.serve_socket()
    try:
        stream = await anyio.connect_unix(socket_path)
        request = protocol.CallRequest(name="merge", arguments={})
        await stream.send((protocol.encode_request(request) + "\n").encode("utf-8"))
        with anyio.fail_after(5):
            await started.wait()
        await stream.aclose()
        # Let the server see the closed connection before the handler resumes.
        for _ in range(20):
            await anyio.sleep(0)
        release.set()

        with anyio.fail_after(5):
            await finished.wait()
        assert side_effects == 1
    finally:
        await host.stop_socket()


async def test_raising_handler_gives_the_same_error_reply_on_the_socket_path() -> None:
    async def handler(**_: object) -> str:
        raise RuntimeError("boom")

    host = ToolHost(
        [Tool(name="t", description="d", input_schema={"type": "object"}, handler=handler)],
        [],
        _resolve_ref_raises,
    )
    socket_path = await host.serve_socket()
    try:
        with anyio.fail_after(5):
            reply = await _socket_call(socket_path, protocol.CallRequest(name="t", arguments={}))
        assert reply == protocol.CallResponse(content="boom", is_error=True)
    finally:
        await host.stop_socket()


async def test_mcp_proxied_call_works_on_the_socket_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_stdio_client(monkeypatch, _fake_mcp_server())

    async with ToolHost([], [_mcp_server_def()], _resolve_ref_raises) as host:
        socket_path = await host.serve_socket()
        request = protocol.CallRequest(name="mcp__fake__echo", arguments={"text": "hi"})
        with anyio.fail_after(5):
            reply = await _socket_call(socket_path, request)
        expected = await host.call("mcp__fake__echo", {"text": "hi"})

    assert reply == protocol.CallResponse(content=expected.content, is_error=False)
    assert expected.is_error is False


async def test_stop_socket_force_stops_an_approved_handler_that_never_returns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Why: a hung approved tool must not keep the turn's end open forever
    # (time cap + force stop).
    from tradewind.application import tool_host

    monkeypatch.setattr(tool_host, "_STOP_SOCKET_GRACE_S", 0.2)
    started = anyio.Event()

    async def handler(**_: object) -> str:
        started.set()
        await anyio.sleep_forever()
        return "never"

    host = ToolHost(
        [Tool(name="hang", description="d", input_schema={"type": "object"}, handler=handler)],
        [],
        _resolve_ref_raises,
    )
    socket_path = await host.serve_socket()
    stream = await anyio.connect_unix(socket_path)
    async with stream:
        request = protocol.CallRequest(name="hang", arguments={})
        await stream.send((protocol.encode_request(request) + "\n").encode("utf-8"))
        with anyio.fail_after(5):
            await started.wait()

        with caplog.at_level("WARNING", logger="tradewind.application.tool_host"):
            with anyio.fail_after(5):
                await host.stop_socket()

    assert [r.__dict__.get("tool") for r in caplog.records] == ["hang"]
    assert not socket_path.exists()
