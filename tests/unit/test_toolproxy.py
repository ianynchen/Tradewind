"""Tests for the toolproxy stdio shim (task-12 brief): the
newline-delimited wire protocol (`tradewind.toolproxy.protocol`), the unix
socket server ToolHost.serve_socket adds to `ToolHost`, and the real
`python -m tradewind.toolproxy` subprocess proxying `tools/list`/
`tools/call` to a live tool registry running in *this* process.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import anyio
import pytest
from anyio.streams.buffered import BufferedByteReceiveStream
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp_types import TextContent

from tradewind.application.tool_host import ToolHost
from tradewind.domain.models import Tool
from tradewind.toolproxy import protocol


def _resolve_ref_raises(_ref: str) -> str:
    raise AssertionError("resolve_ref should not be called; no MCP servers are configured")


def _echo_tool(name: str = "echo") -> Tool:
    async def handler(*, text: str) -> str:
        return f"echo:{text}"

    return Tool(
        name=name,
        description="echo text back",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=handler,
    )


# --- protocol.py: encode/decode round trips and malformed-input errors ---


def test_encode_decode_list_request_round_trips() -> None:
    line = protocol.encode_request(protocol.ListRequest())

    assert line == '{"op": "list"}'
    assert protocol.decode_request(line) == protocol.ListRequest()


def test_encode_decode_call_request_round_trips() -> None:
    request = protocol.CallRequest(name="add", arguments={"x": 1, "y": 2})

    decoded = protocol.decode_request(protocol.encode_request(request))

    assert decoded == request


def test_encode_decode_list_response_round_trips() -> None:
    response = protocol.ListResponse(tools=[{"name": "add", "description": "", "input_schema": {}}])

    decoded = protocol.decode_response(protocol.encode_response(response))

    assert decoded == response


def test_encode_decode_call_response_round_trips() -> None:
    response = protocol.CallResponse(content="ok", is_error=False)

    decoded = protocol.decode_response(protocol.encode_response(response))

    assert decoded == response


def test_encode_decode_error_response_round_trips() -> None:
    response = protocol.ErrorResponse(error="boom")

    decoded = protocol.decode_response(protocol.encode_response(response))

    assert decoded == response


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        "[]",
        '{"op": "unknown"}',
        '{"op": "call"}',
        '{"op": "call", "name": 1}',
        '{"op": "call", "name": "x", "arguments": "not-an-object"}',
    ],
)
def test_decode_request_raises_protocol_error_on_malformed_input(line: str) -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_request(line)


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        "[]",
        "{}",
        '{"error": 1}',
        '{"tools": "not-a-list"}',
        '{"content": 1}',
        '{"content": "ok", "is_error": "not-a-bool"}',
    ],
)
def test_decode_response_raises_protocol_error_on_malformed_input(line: str) -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_response(line)


# --- ToolHost.serve_socket: raw NDJSON socket protocol ---


async def _socket_call(socket_path: Path, request: protocol.Request) -> protocol.Response:
    stream = await anyio.connect_unix(socket_path)
    async with stream:
        await stream.send((protocol.encode_request(request) + "\n").encode("utf-8"))
        buffered = BufferedByteReceiveStream(stream)
        line = await buffered.receive_until(b"\n", protocol.MAX_LINE_BYTES)
        return protocol.decode_response(line.decode("utf-8"))


async def test_serve_socket_returns_a_path_and_is_idempotent() -> None:
    host = ToolHost([_echo_tool()], [], _resolve_ref_raises)

    socket_path = await host.serve_socket()
    try:
        assert socket_path.exists()
        assert await host.serve_socket() == socket_path
    finally:
        await host.stop_socket()


async def test_serve_socket_concurrent_first_callers_start_only_one_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix round 1, Minor: two callers racing the very first `serve_socket()`
    (before `_socket_path` is set) must not each start their own listener --
    `_socket_lock` should serialize the create-if-absent section so only one
    `anyio.create_unix_listener` call happens, and every racing caller gets
    back the same path."""
    host = ToolHost([_echo_tool()], [], _resolve_ref_raises)
    real_create_unix_listener = anyio.create_unix_listener
    call_count = 0

    async def counting_create_unix_listener(*args: object, **kwargs: object) -> object:
        nonlocal call_count
        call_count += 1
        await anyio.sleep(0.01)  # widen the race window
        return await real_create_unix_listener(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(anyio, "create_unix_listener", counting_create_unix_listener)

    results: list[Path] = []
    try:
        async with anyio.create_task_group() as tg:

            async def _call() -> None:
                results.append(await host.serve_socket())

            for _ in range(10):
                tg.start_soon(_call)
    finally:
        await host.stop_socket()

    assert call_count == 1
    assert len(results) == 10
    assert len(set(results)) == 1


async def test_socket_list_lists_registered_tools_in_anthropic_format() -> None:
    host = ToolHost([_echo_tool()], [], _resolve_ref_raises)
    socket_path = await host.serve_socket()
    try:
        response = await _socket_call(socket_path, protocol.ListRequest())
    finally:
        await host.stop_socket()

    assert response == protocol.ListResponse(
        tools=[
            {
                "name": "echo",
                "description": "echo text back",
                "input_schema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }
        ]
    )


async def test_socket_call_dispatches_to_the_local_handler() -> None:
    host = ToolHost([_echo_tool()], [], _resolve_ref_raises)
    socket_path = await host.serve_socket()
    try:
        response = await _socket_call(
            socket_path, protocol.CallRequest(name="echo", arguments={"text": "hi"})
        )
    finally:
        await host.stop_socket()

    assert response == protocol.CallResponse(content="echo:hi", is_error=False)


async def test_socket_call_unknown_tool_is_an_error_outcome_not_a_crash() -> None:
    host = ToolHost([], [], _resolve_ref_raises)
    socket_path = await host.serve_socket()
    try:
        response = await _socket_call(
            socket_path, protocol.CallRequest(name="missing", arguments={})
        )
    finally:
        await host.stop_socket()

    assert response == protocol.CallResponse(content="unknown tool: 'missing'", is_error=True)


async def test_socket_malformed_json_line_gets_an_error_response_and_server_stays_up() -> None:
    host = ToolHost([_echo_tool()], [], _resolve_ref_raises)
    socket_path = await host.serve_socket()
    try:
        stream = await anyio.connect_unix(socket_path)
        async with stream:
            await stream.send(b"not valid json at all\n")
            buffered = BufferedByteReceiveStream(stream)
            line = await buffered.receive_until(b"\n", protocol.MAX_LINE_BYTES)
            response = protocol.decode_response(line.decode("utf-8"))
            assert isinstance(response, protocol.ErrorResponse)

            # The connection -- and the server -- are still alive: a
            # well-formed request on the same connection still works.
            await stream.send((protocol.encode_request(protocol.ListRequest()) + "\n").encode())
            line = await buffered.receive_until(b"\n", protocol.MAX_LINE_BYTES)
            assert isinstance(protocol.decode_response(line.decode("utf-8")), protocol.ListResponse)

        # And the server still accepts brand new connections too.
        follow_up = await _socket_call(socket_path, protocol.ListRequest())
        assert isinstance(follow_up, protocol.ListResponse)
    finally:
        await host.stop_socket()


async def test_socket_oversized_line_gets_an_error_response_and_server_stays_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix round 1, Minor: a line longer than `protocol.MAX_LINE_BYTES` must
    still get an `ErrorResponse` on the wire, not just a silent disconnect.
    `MAX_LINE_BYTES` is patched down so the test doesn't need to push tens
    of megabytes over a socket -- both `tool_host.py` and this test read
    `protocol.MAX_LINE_BYTES` as a live attribute lookup, so patching the
    one shared module constant covers both sides consistently."""
    monkeypatch.setattr(protocol, "MAX_LINE_BYTES", 64)
    host = ToolHost([_echo_tool()], [], _resolve_ref_raises)
    socket_path = await host.serve_socket()
    try:
        stream = await anyio.connect_unix(socket_path)
        async with stream:
            # No trailing `\n`: if it were appended here, a single local
            # `receive()` can return the whole write (delimiter included)
            # in one shot, and `receive_until` checks for the delimiter
            # *before* checking the length cap -- so the line would be
            # read successfully instead of tripping `DelimiterNotFound`.
            # Withholding the delimiter forces the length cap to fire.
            oversized = b"x" * 200
            await stream.send(oversized)
            buffered = BufferedByteReceiveStream(stream)
            line = await buffered.receive_until(b"\n", 1024)
            response = protocol.decode_response(line.decode("utf-8"))
            assert response == protocol.ErrorResponse(error="line too long")

        # The server itself is unaffected: a fresh connection still works.
        follow_up = await _socket_call(socket_path, protocol.ListRequest())
        assert isinstance(follow_up, protocol.ListResponse)
    finally:
        await host.stop_socket()


async def test_socket_handles_concurrent_connections() -> None:
    host = ToolHost([_echo_tool()], [], _resolve_ref_raises)
    socket_path = await host.serve_socket()
    try:
        async with anyio.create_task_group() as tg:
            results: list[protocol.Response] = [protocol.ErrorResponse(error="")] * 10

            async def _call(i: int) -> None:
                results[i] = await _socket_call(
                    socket_path, protocol.CallRequest(name="echo", arguments={"text": str(i)})
                )

            for i in range(10):
                tg.start_soon(_call, i)
    finally:
        await host.stop_socket()

    assert results == [
        protocol.CallResponse(content=f"echo:{i}", is_error=False) for i in range(10)
    ]


async def test_stop_socket_removes_the_socket_file() -> None:
    host = ToolHost([_echo_tool()], [], _resolve_ref_raises)
    socket_path = await host.serve_socket()
    assert socket_path.exists()

    await host.stop_socket()

    assert not socket_path.exists()


async def test_stop_socket_is_a_no_op_when_no_socket_is_running() -> None:
    host = ToolHost([], [], _resolve_ref_raises)

    await host.stop_socket()  # must not raise


async def test_aexit_stops_the_socket_server_too() -> None:
    host = ToolHost([_echo_tool()], [], _resolve_ref_raises)

    async with host:
        socket_path = await host.serve_socket()
        assert socket_path.exists()

    assert not socket_path.exists()


async def test_serve_socket_uses_configured_socket_dir() -> None:
    socket_dir = Path(tempfile.mkdtemp(prefix="tw-cfg-"))
    host = ToolHost([_echo_tool()], [], _resolve_ref_raises, socket_dir=socket_dir)

    socket_path = await host.serve_socket()
    try:
        assert socket_path.parent == socket_dir
    finally:
        await host.stop_socket()


# --- serve_socket socket-dir permissions (fix round 1, Important) ---


async def test_serve_socket_default_dir_is_mode_0o700() -> None:
    """No `socket_dir` given: `tempfile.mkdtemp()` creates it, which is
    already owner-only -- confirm `serve_socket` doesn't loosen that."""
    host = ToolHost([_echo_tool()], [], _resolve_ref_raises)

    socket_path = await host.serve_socket()
    try:
        assert (socket_path.parent.stat().st_mode & 0o777) == 0o700
    finally:
        await host.stop_socket()


async def test_serve_socket_creates_missing_caller_dir_as_mode_0o700_regardless_of_umask() -> None:
    """A caller-supplied `socket_dir` that doesn't exist yet must still end
    up owner-only -- forced past a permissive umask, not just whatever
    `mkdir`'s masked `mode` happens to leave behind."""
    parent = Path(tempfile.mkdtemp(prefix="tw-parent-"))
    socket_dir = parent / "sockets"
    assert not socket_dir.exists()
    host = ToolHost([_echo_tool()], [], _resolve_ref_raises, socket_dir=socket_dir)

    old_umask = os.umask(0o022)  # permissive: proves 0o700 isn't just luck
    try:
        await host.serve_socket()
    finally:
        os.umask(old_umask)
    try:
        assert (socket_dir.stat().st_mode & 0o777) == 0o700
    finally:
        await host.stop_socket()


async def test_serve_socket_does_not_touch_permissions_of_a_preexisting_caller_dir() -> None:
    """A caller-supplied `socket_dir` that already exists keeps whatever
    permissions the caller gave it -- `serve_socket` only forces `0o700` on
    a directory it creates itself."""
    socket_dir = Path(tempfile.mkdtemp(prefix="tw-existing-"))
    socket_dir.chmod(0o755)
    host = ToolHost([_echo_tool()], [], _resolve_ref_raises, socket_dir=socket_dir)

    await host.serve_socket()
    try:
        assert (socket_dir.stat().st_mode & 0o777) == 0o755
    finally:
        await host.stop_socket()


# --- ToolHost.shim_server_def ---


async def test_shim_server_def_points_at_python_dash_m_toolproxy() -> None:
    host = ToolHost([_echo_tool()], [], _resolve_ref_raises)

    try:
        server_def = await host.shim_server_def()
        expected_socket_path = str(await host.serve_socket())
    finally:
        await host.stop_socket()

    assert server_def.transport == "stdio"
    assert server_def.command == [sys.executable, "-m", "tradewind.toolproxy"]
    assert server_def.env == {protocol.SOCKET_ENV_VAR: expected_socket_path}


async def test_shim_server_def_starts_the_socket_if_not_already_running() -> None:
    host = ToolHost([_echo_tool()], [], _resolve_ref_raises)

    try:
        server_def = await host.shim_server_def()
        assert Path(server_def.env[protocol.SOCKET_ENV_VAR]).exists()
    finally:
        await host.stop_socket()


# --- The proof test (brief Step 1): a real `python -m tradewind.toolproxy`
# subprocess, driven as an MCP client over stdio, proxies tools/list and
# tools/call back to a live Python closure running in *this* test process. ---


async def test_shim_subprocess_proxies_tools_list_and_calls_the_live_closure() -> None:
    # Mutated only by the tool's own handler -- a closure over this local,
    # never serialized, never passed to the subprocess. If the assertion
    # below passes, the subprocess's `tools/call` really did run *this*
    # process's handler via the socket, not some copy of it.
    mutated = {"total": 0}

    async def increment(*, amount: int) -> str:
        mutated["total"] += amount
        return f"total={mutated['total']}"

    tool = Tool(
        name="increment",
        description="add amount to a running total",
        input_schema={
            "type": "object",
            "properties": {"amount": {"type": "integer"}},
            "required": ["amount"],
        },
        handler=increment,
    )
    host = ToolHost([tool], [], _resolve_ref_raises)
    socket_path = await host.serve_socket()
    try:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "tradewind.toolproxy"],
            env={protocol.SOCKET_ENV_VAR: str(socket_path)},
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()

            listed = await session.list_tools()
            assert [t.name for t in listed.tools] == ["increment"]

            result = await session.call_tool("increment", {"amount": 5})
            second = await session.call_tool("increment", {"amount": 7})
    finally:
        await host.stop_socket()

    # The live closure in THIS process was mutated by the subprocess's call.
    assert mutated["total"] == 12

    assert result.is_error is False
    text = "".join(block.text for block in result.content if isinstance(block, TextContent))
    assert text == "total=5"
    second_text = "".join(block.text for block in second.content if isinstance(block, TextContent))
    assert second_text == "total=12"
