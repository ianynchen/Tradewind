"""`python -m tradewind.toolproxy`: a stdio MCP server that proxies
`tools/list`/`tools/call` to the live `ToolHost` registry running in the
host process, over the unix socket `ToolHost.serve_socket()` started
(task-12 brief). This is what lets an engine without in-process tool
support (Codex) call live Python closures registered in the host process:
the host hands the engine `ToolHost.shim_server_def()`, the engine spawns
this module as an ordinary stdio MCP server, and every `tools/call` it
makes is forwarded here to the socket and back.

Dependency-light and layer-leaf on purpose: this module is spawned as a
subprocess inside engine sandboxes, so it must run with nothing more than
the `mcp` package, `anyio`, and the stdlib (plus its sibling
`tradewind.toolproxy.protocol`) -- it must never import
`tradewind.application` or anything that pulls the rest of tradewind in.
"""

from __future__ import annotations

import os
from typing import cast

import anyio
import mcp_types as types
from anyio.streams.buffered import BufferedByteReceiveStream
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from tradewind.toolproxy import protocol


class ProxyError(RuntimeError):
    """The socket connection to `ToolHost.serve_socket` failed, or the tool
    host sent back something that doesn't match the expected response for
    the request that was made. Raised out of an MCP handler, this becomes a
    JSON-RPC error the calling engine sees (`Server.run`'s default
    `raise_exceptions=False` turns handler exceptions into error responses
    instead of crashing the shim)."""


async def _call_socket(request: protocol.Request) -> protocol.Response:
    socket_path = os.environ.get(protocol.SOCKET_ENV_VAR)
    if not socket_path:
        raise ProxyError(f"{protocol.SOCKET_ENV_VAR} is not set")
    stream = await anyio.connect_unix(socket_path)
    async with stream:
        await stream.send((protocol.encode_request(request) + "\n").encode("utf-8"))
        buffered = BufferedByteReceiveStream(stream)
        try:
            line = await buffered.receive_until(b"\n", protocol.MAX_LINE_BYTES)
        except anyio.EndOfStream as exc:
            raise ProxyError("tool host closed the connection without responding") from exc
        try:
            return protocol.decode_response(line.decode("utf-8"))
        except protocol.ProtocolError as exc:
            raise ProxyError(f"tool host sent an unparseable response: {exc}") from exc


def _tool_from_schema(schema: dict[str, object]) -> types.Tool:
    name = schema.get("name")
    if not isinstance(name, str):
        raise ProxyError(f"tool schema missing a string 'name': {schema!r}")
    description = schema.get("description")
    input_schema = schema.get("input_schema", {})
    if not isinstance(input_schema, dict):
        raise ProxyError(f"tool schema 'input_schema' must be an object: {schema!r}")
    return types.Tool(
        name=name,
        description=description if isinstance(description, str) else None,
        input_schema=cast("dict[str, object]", input_schema),
    )


async def _on_list_tools(
    _ctx: ServerRequestContext[object], _params: types.PaginatedRequestParams | None
) -> types.ListToolsResult:
    response = await _call_socket(protocol.ListRequest())
    if not isinstance(response, protocol.ListResponse):
        raise ProxyError(f"expected a list response from the tool host, got {response!r}")
    return types.ListToolsResult(tools=[_tool_from_schema(schema) for schema in response.tools])


async def _on_call_tool(
    _ctx: ServerRequestContext[object], params: types.CallToolRequestParams
) -> types.CallToolResult:
    arguments = params.arguments if params.arguments is not None else {}
    response = await _call_socket(protocol.CallRequest(name=params.name, arguments=arguments))
    if not isinstance(response, protocol.CallResponse):
        raise ProxyError(f"expected a call response from the tool host, got {response!r}")
    return types.CallToolResult(
        content=[types.TextContent(text=response.content)], is_error=response.is_error
    )


def _build_server() -> Server[object]:
    return Server("tradewind-toolproxy", on_list_tools=_on_list_tools, on_call_tool=_on_call_tool)


async def _run() -> None:
    server = _build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    anyio.run(_run)


if __name__ == "__main__":
    main()
