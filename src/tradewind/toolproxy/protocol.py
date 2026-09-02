"""Toolproxy wire protocol: the newline-delimited JSON spoken over the unix
socket between `tradewind.application.tool_host.ToolHost.serve_socket` (the
server, running in the host process) and `python -m tradewind.toolproxy` (the
client, a stdio MCP shim spawned inside an engine sandbox that has no
in-process tool access -- task-12 brief).

Lives under `tradewind.toolproxy`, not `tradewind.application`, so the shim
(which must stay leaf -- stdlib + `mcp` + `anyio` only, never
`tradewind.application`, since it is spawned as a subprocess inside engine
sandboxes) can import it without pulling in the rest of the application
layer. `application/tool_host.py` imports it back the other way
(`tradewind.application` -> `tradewind.toolproxy`); `tradewind.toolproxy`
is not one of the layers named in the import-linter `layers` contract
(pyproject.toml `[tool.importlinter]`), so that import direction is
unconstrained by it, and `tradewind.toolproxy` itself imports nothing from
`tradewind.application` or `tradewind.domain`, so no cycle is created.

Two request shapes, both JSON objects terminated by `\n`:

- `{"op": "list"}` -> `{"tools": [<anthropic-format schema dict>, ...]}`
- `{"op": "call", "name": ..., "arguments": {...}}` ->
  `{"content": "...", "is_error": bool}`

A line that fails to decode as either becomes `{"error": "..."}` on the wire
(`ErrorResponse`) -- the server never crashes or drops the connection over a
malformed line.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final, cast

OP_LIST: Final = "list"
OP_CALL: Final = "call"

#: Env var `ToolHost.shim_server_def()` sets on the shim subprocess, and the
#: shim (`tradewind.toolproxy.__main__`) reads, carrying the unix socket path.
SOCKET_ENV_VAR: Final = "TRADEWIND_TOOL_SOCKET"

#: Longest single NDJSON line either side of the socket will read before
#: giving up (`anyio.DelimiterNotFound`) rather than buffering unboundedly.
MAX_LINE_BYTES: Final = 16 * 1024 * 1024


class ProtocolError(ValueError):
    """A socket line could not be decoded as a valid toolproxy request or
    response. Callers catch this to turn a malformed line into an
    `ErrorResponse` instead of crashing the connection."""


@dataclass(frozen=True)
class ListRequest:
    """`{"op": "list"}`: list every tool `ToolHost` currently exposes."""


@dataclass(frozen=True)
class CallRequest:
    """`{"op": "call", "name": ..., "arguments": {...}}`: invoke one tool."""

    name: str
    arguments: dict[str, object]


Request = ListRequest | CallRequest


@dataclass(frozen=True)
class ListResponse:
    """`tools`: anthropic-format schema dicts, `ToolHost.schemas()` verbatim."""

    tools: list[dict[str, object]]


@dataclass(frozen=True)
class CallResponse:
    """Mirrors `ToolHost.ToolOutcome`: `content` is always a string, `is_error`
    distinguishes a tool failure from success."""

    content: str
    is_error: bool


@dataclass(frozen=True)
class ErrorResponse:
    """A request the server could not honor -- malformed JSON, an unknown
    `op`, or a wrongly-shaped payload. Never raised across the socket: the
    connection stays open for the next line."""

    error: str


Response = ListResponse | CallResponse | ErrorResponse


def encode_request(request: Request) -> str:
    """Encode one request as a single JSON line (no trailing `\\n`)."""
    payload: dict[str, object]
    if isinstance(request, ListRequest):
        payload = {"op": OP_LIST}
    else:
        payload = {"op": OP_CALL, "name": request.name, "arguments": request.arguments}
    return json.dumps(payload)


def decode_request(line: str) -> Request:
    """Decode one socket line into a `Request`.

    Raises:
        ProtocolError: `line` is not valid JSON, not a JSON object, has an
            `op` other than `"list"`/`"call"`, or a `"call"` is missing a
            string `name` / has a non-object `arguments`.
    """
    payload = _decode_json_object(line)
    op = payload.get("op")
    if op == OP_LIST:
        return ListRequest()
    if op == OP_CALL:
        name = payload.get("name")
        if not isinstance(name, str):
            raise ProtocolError("'call' request requires a string 'name'")
        arguments = payload.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ProtocolError("'call' request 'arguments' must be an object")
        return CallRequest(name=name, arguments=cast("dict[str, object]", arguments))
    raise ProtocolError(f"unknown op {op!r}")


def encode_response(response: Response) -> str:
    """Encode one response as a single JSON line (no trailing `\\n`)."""
    payload: dict[str, object]
    if isinstance(response, ListResponse):
        payload = {"tools": response.tools}
    elif isinstance(response, CallResponse):
        payload = {"content": response.content, "is_error": response.is_error}
    else:
        payload = {"error": response.error}
    return json.dumps(payload)


def decode_response(line: str) -> Response:
    """Decode one socket line into a `Response`.

    Raises:
        ProtocolError: `line` is not valid JSON, not a JSON object, or does
            not match any of the three known response shapes.
    """
    payload = _decode_json_object(line)
    if "error" in payload:
        error = payload["error"]
        if not isinstance(error, str):
            raise ProtocolError("error response 'error' must be a string")
        return ErrorResponse(error=error)
    if "tools" in payload:
        tools = payload["tools"]
        if not isinstance(tools, list):
            raise ProtocolError("list response 'tools' must be an array")
        return ListResponse(tools=cast("list[dict[str, object]]", tools))
    if "content" in payload:
        content = payload["content"]
        is_error = payload.get("is_error", False)
        if not isinstance(content, str):
            raise ProtocolError("call response 'content' must be a string")
        if not isinstance(is_error, bool):
            raise ProtocolError("call response 'is_error' must be a boolean")
        return CallResponse(content=content, is_error=is_error)
    raise ProtocolError(f"unrecognized response shape: {sorted(payload)}")


def _decode_json_object(line: str) -> dict[str, object]:
    try:
        parsed: object = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ProtocolError(f"expected a JSON object, got {type(parsed).__name__}")
    return cast("dict[str, object]", parsed)
