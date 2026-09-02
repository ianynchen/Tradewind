"""Application tool host: in-process dispatch plus an MCP client proxy for
caller-declared MCP servers (task-7 brief).

`ToolHost` is the one registry behind two renderings (ARCHITECTURE §3 R-2):
local `Tool`s dispatch straight to `handler(**arguments)`; `McpServerDef`s
are connected as MCP clients and their tools are namespaced
`mcp__<server>__<tool>`. It is an async context manager — entering connects
every declared MCP server (stdio via `mcp.client.stdio`, http via the
streamable-HTTP client) and lists each one's tools; exiting disconnects
every client session and forgets the MCP tool schemas.

`resolve_ref` expands `ref:`-prefixed `McpServerDef.env`/`headers` values
into live secrets at connect time only (I-2): the resolved values are used
to build the transport and are never stored on `ToolHost` or exposed
through `schemas()` — only the declared `Tool`/`McpServerDef` metadata is.

`serve_socket`/`stop_socket`/`shim_server_def` (task-12 brief) are the other
half of the same registry: a unix-socket server, speaking the
newline-delimited JSON protocol in `tradewind.toolproxy.protocol`, that lets
`python -m tradewind.toolproxy` -- a stdio MCP shim spawned inside engines
with no in-process tool support (Codex) -- proxy `tools/list`/`tools/call`
back to this same `_tools`/`_mcp_targets` registry. `tradewind.toolproxy` is
not one of the layers named in the import-linter `layers` contract
(pyproject.toml `[tool.importlinter]`), so `tradewind.application` importing
from it is unconstrained by that contract, and `tradewind.toolproxy` itself
never imports `tradewind.application` (verified by `lint-imports`; the shim
must stay leaf since it runs inside engine sandboxes), so no cycle results.
"""

from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import uuid
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import cast

import anyio
from anyio.abc import SocketListener, SocketStream, TaskGroup
from anyio.streams.buffered import BufferedByteReceiveStream
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp_types import TextContent

from tradewind.domain.models import McpServerDef, Tool
from tradewind.toolproxy import protocol


@dataclass
class ToolOutcome:
    """The result of one `ToolHost.call()`: `content` is always a string
    (a local handler's non-string return is JSON-encoded; an MCP result's
    text content parts are concatenated), `is_error` distinguishes a tool
    failure from success without the caller needing to inspect `content`."""

    content: str
    is_error: bool


def _local_schema(tool: Tool) -> dict[str, object]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": cast("dict[str, object]", tool.input_schema),
    }


def _mcp_tool_name(server_name: str, tool_name: str) -> str:
    return f"mcp__{server_name}__{tool_name}"


class ToolHost:
    def __init__(
        self,
        tools: list[Tool],
        mcp_servers: list[McpServerDef],
        resolve_ref: Callable[[str], str],
        socket_dir: Path | None = None,
    ) -> None:
        self._tools = {tool.name: tool for tool in tools}
        self._mcp_servers = mcp_servers
        self._resolve_ref = resolve_ref
        self._sessions: dict[str, ClientSession] = {}
        self._mcp_schemas: dict[str, dict[str, object]] = {}
        self._mcp_targets: dict[str, tuple[str, str]] = {}
        self._exit_stack: AsyncExitStack | None = None
        # `serve_socket` state (task-12 brief): `socket_dir` is where the
        # socket file is created (`ToolHostConfig.socket_dir`, passed
        # through by the caller); `None` falls back to a fresh
        # `tempfile.mkdtemp()` per `serve_socket` call.
        self._socket_dir = socket_dir
        self._socket_path: Path | None = None
        self._socket_listener: SocketListener | None = None
        self._socket_task_group: TaskGroup | None = None

    async def __aenter__(self) -> ToolHost:
        exit_stack = AsyncExitStack()
        self._exit_stack = exit_stack
        try:
            for server in self._mcp_servers:
                session = await self._connect(server, exit_stack)
                self._sessions[server.name] = session
                listed = await session.list_tools()
                for tool in listed.tools:
                    name = _mcp_tool_name(server.name, tool.name)
                    self._mcp_targets[name] = (server.name, tool.name)
                    self._mcp_schemas[name] = {
                        "name": name,
                        "description": tool.description or "",
                        "input_schema": cast("dict[str, object]", tool.input_schema),
                    }
        except BaseException:
            await exit_stack.aclose()
            self._exit_stack = None
            self._sessions.clear()
            self._mcp_schemas.clear()
            self._mcp_targets.clear()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
            self._exit_stack = None
        self._sessions.clear()
        self._mcp_schemas.clear()
        self._mcp_targets.clear()
        await self.stop_socket()

    async def _connect(self, server: McpServerDef, exit_stack: AsyncExitStack) -> ClientSession:
        if server.transport == "stdio":
            if not server.command:
                raise ValueError(f"MCP server {server.name!r}: stdio transport requires `command`")
            env = {key: self._resolve(value) for key, value in server.env.items()}
            params = StdioServerParameters(
                command=server.command[0], args=server.command[1:], env=env
            )
            read, write = await exit_stack.enter_async_context(stdio_client(params))
        else:
            if not server.url:
                raise ValueError(f"MCP server {server.name!r}: http transport requires `url`")
            headers = {key: self._resolve(value) for key, value in server.headers.items()}
            http_client = create_mcp_http_client(headers=headers)
            await exit_stack.enter_async_context(http_client)
            read, write = await exit_stack.enter_async_context(
                streamable_http_client(server.url, http_client=http_client)
            )
        session = await exit_stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    def _resolve(self, value: str) -> str:
        return self._resolve_ref(value) if value.startswith("ref:") else value

    async def serve_socket(self) -> Path:
        """Start the unix-socket server the toolproxy shim (and any other
        out-of-process caller) connects to, returning the socket path.

        Idempotent: a second call while a socket server is already running
        returns the existing path without starting another listener. Usable
        with or without `async with host:` -- it only touches the local
        `_tools`/`_mcp_targets` registry (via `schemas()`/`call()`), not the
        MCP client sessions `__aenter__` connects. `stop_socket()` (which
        `__aexit__` also calls) stops the server and removes the socket
        file.
        """
        if self._socket_path is not None:
            return self._socket_path
        socket_dir = (
            self._socket_dir
            if self._socket_dir is not None
            else Path(tempfile.mkdtemp(prefix="tradewind-tp-"))
        )
        socket_dir.mkdir(parents=True, exist_ok=True)
        # Short, unique filename: unix socket paths are capped at ~104-108
        # bytes (`sockaddr_un.sun_path`), and a caller-supplied `socket_dir`
        # can already be deep (e.g. a system tempdir).
        socket_path = socket_dir / f"{uuid.uuid4().hex[:8]}.sock"
        listener = await anyio.create_unix_listener(socket_path)
        task_group = anyio.create_task_group()
        await task_group.__aenter__()
        task_group.start_soon(self._serve_socket_forever, listener)
        self._socket_listener = listener
        self._socket_task_group = task_group
        self._socket_path = socket_path
        return socket_path

    async def _serve_socket_forever(self, listener: SocketListener) -> None:
        # `listener.aclose()` (from `stop_socket`) unblocks the pending
        # `accept()` inside `serve()` with `ClosedResourceError` -- expected
        # shutdown, not a crash.
        with contextlib.suppress(anyio.ClosedResourceError):
            await listener.serve(self._handle_socket_connection)

    async def stop_socket(self) -> None:
        """Stop the socket server started by `serve_socket` and remove the
        socket file. A no-op if no socket server is running."""
        listener = self._socket_listener
        task_group = self._socket_task_group
        socket_path = self._socket_path
        if listener is None or task_group is None or socket_path is None:
            return
        task_group.cancel_scope.cancel()
        await listener.aclose()
        await task_group.__aexit__(None, None, None)
        socket_path.unlink(missing_ok=True)
        self._socket_listener = None
        self._socket_task_group = None
        self._socket_path = None

    async def _handle_socket_connection(self, stream: SocketStream) -> None:
        buffered = BufferedByteReceiveStream(stream)
        async with stream:
            while True:
                try:
                    line = await buffered.receive_until(b"\n", protocol.MAX_LINE_BYTES)
                except (anyio.IncompleteRead, anyio.DelimiterNotFound):
                    return
                response = await self._handle_socket_line(line)
                await stream.send((protocol.encode_response(response) + "\n").encode("utf-8"))

    async def _handle_socket_line(self, line: bytes) -> protocol.Response:
        try:
            request = protocol.decode_request(line.decode("utf-8"))
        except (protocol.ProtocolError, UnicodeDecodeError) as exc:
            # Malformed request: an error response, never a crash (brief) --
            # the connection stays open for the caller's next line.
            return protocol.ErrorResponse(error=str(exc))
        if isinstance(request, protocol.ListRequest):
            return protocol.ListResponse(tools=self.schemas())
        outcome = await self.call(request.name, request.arguments)
        return protocol.CallResponse(content=outcome.content, is_error=outcome.is_error)

    async def shim_server_def(self) -> McpServerDef:
        """The `McpServerDef` for `python -m tradewind.toolproxy`, wired to
        this host's socket (starting `serve_socket` first if it isn't
        running yet). Adding the result to a caller's `mcp_servers` -- e.g.
        an engine with no in-process tool support -- gets it live access to
        every tool this `ToolHost` exposes, local and MCP-proxied alike."""
        socket_path = await self.serve_socket()
        return McpServerDef(
            name="toolproxy",
            transport="stdio",
            command=[sys.executable, "-m", "tradewind.toolproxy"],
            env={protocol.SOCKET_ENV_VAR: str(socket_path)},
        )

    def schemas(self) -> list[dict[str, object]]:
        local = [_local_schema(tool) for tool in self._tools.values()]
        return local + list(self._mcp_schemas.values())

    async def call(self, name: str, arguments: dict[str, object]) -> ToolOutcome:
        if name in self._tools:
            return await self._call_local(self._tools[name], arguments)
        if name in self._mcp_targets:
            return await self._call_mcp(name, arguments)
        return ToolOutcome(content=f"unknown tool: {name!r}", is_error=True)

    async def _call_local(self, tool: Tool, arguments: dict[str, object]) -> ToolOutcome:
        try:
            result = await tool.handler(**arguments)
        except Exception as exc:  # handler errors become tool-result content, never raise (brief)
            return ToolOutcome(content=str(exc), is_error=True)
        content = result if isinstance(result, str) else json.dumps(result)
        return ToolOutcome(content=content, is_error=False)

    async def _call_mcp(self, name: str, arguments: dict[str, object]) -> ToolOutcome:
        # `_mcp_targets` and `_sessions` are populated together in `__aenter__`
        # and cleared together in `__aexit__`, so a hit in `_mcp_targets`
        # (the caller of `call()` already checked) always has a session here.
        server_name, tool_name = self._mcp_targets[name]
        session = self._sessions[server_name]
        try:
            result = await session.call_tool(tool_name, arguments)
        except Exception as exc:  # MCP call errors become tool-result content, never raise
            return ToolOutcome(content=str(exc), is_error=True)
        text = "".join(block.text for block in result.content if isinstance(block, TextContent))
        return ToolOutcome(content=text, is_error=result.is_error)
