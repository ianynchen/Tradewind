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
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from types import TracebackType
from typing import cast

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp_types import TextContent

from tradewind.domain.models import McpServerDef, Tool


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
    ) -> None:
        self._tools = {tool.name: tool for tool in tools}
        self._mcp_servers = mcp_servers
        self._resolve_ref = resolve_ref
        self._sessions: dict[str, ClientSession] = {}
        self._mcp_schemas: dict[str, dict[str, object]] = {}
        self._mcp_targets: dict[str, tuple[str, str]] = {}
        self._exit_stack: AsyncExitStack | None = None

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
