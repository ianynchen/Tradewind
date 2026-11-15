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

`result_schema` (05-turn-governance §6) adds the result-tool seam for
engines with no output-schema support: `RESULT_TOOL_NAME` is served through
the same `schemas()`/`call()` path as every other tool (so the shim exposes
it), validated with `jsonschema`, and recorded in `result_slot`. It is a
structural pass-through: it never consults the broker.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import tempfile
import uuid
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Final, cast

import anyio
import jsonschema
from anyio.abc import SocketListener, SocketStream
from anyio.streams.buffered import BufferedByteReceiveStream
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp_types import TextContent

from tradewind.domain.errors import ConfigError
from tradewind.domain.models import McpServerDef, PermissionBroker, Tool, normalize_decision
from tradewind.toolproxy import protocol

#: The result tool's name (05 §6). A caller tool of this name is refused.
RESULT_TOOL_NAME: Final = "tradewind_submit_result"

#: Calls of the result tool that count; a further call is answered
#: `"No more attempts."` and not recorded (05 §6).
MAX_RESULT_ATTEMPTS: Final = 3


@dataclass
class ResultSlot:
    """What the result tool has received this turn (05 §6). `attempts` is
    the number of counted calls (0..`MAX_RESULT_ATTEMPTS`, valid or not);
    `last_valid` is the arguments of the last call that passed validation,
    or `None`. Mutable on purpose: `ToolHost.call` updates it in place and
    the turn reads it after the engine finishes. Not safe for concurrent
    writers beyond the single event loop that runs `call()`."""

    attempts: int = 0
    last_valid: dict[str, object] | None = field(default=None)

    def clear(self) -> None:
        """Forget every attempt and the recorded result."""
        self.attempts = 0
        self.last_valid = None


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


def _result_validator(
    result_schema: dict[str, object], tools: dict[str, Tool]
) -> Callable[[dict[str, object]], list[str]]:
    """Check `result_schema` once and return a function giving the
    validation error messages for one set of arguments (empty = valid)."""
    if RESULT_TOOL_NAME in tools:
        raise ConfigError(
            f"tool name {RESULT_TOOL_NAME!r} is reserved for the result tool; "
            "rename the caller tool"
        )
    validator_class = jsonschema.validators.validator_for(
        result_schema, default=jsonschema.Draft202012Validator
    )
    try:
        validator_class.check_schema(result_schema)
    except jsonschema.SchemaError as exc:
        raise ConfigError(f"output_schema is not a valid JSON Schema: {exc.message}") from exc
    validator = validator_class(result_schema)

    def error_messages(arguments: dict[str, object]) -> list[str]:
        return [str(error.message) for error in validator.iter_errors(arguments)]

    return error_messages


class ToolHost:
    def __init__(
        self,
        tools: list[Tool],
        mcp_servers: list[McpServerDef],
        resolve_ref: Callable[[str], str],
        socket_dir: Path | None = None,
        broker: PermissionBroker | None = None,
        result_schema: dict[str, object] | None = None,
    ) -> None:
        """`result_schema`, when set, serves `RESULT_TOOL_NAME` with that
        schema verbatim as its input schema (05 §6). Raises `ConfigError`
        if `result_schema` is not a valid JSON Schema for the draft it
        declares (default Draft 2020-12), or if a caller tool is named
        `RESULT_TOOL_NAME`."""
        self._tools = {tool.name: tool for tool in tools}
        self.result_slot = ResultSlot()
        self._result_schema = result_schema
        self._result_validator = (
            None if result_schema is None else _result_validator(result_schema, self._tools)
        )
        self._mcp_servers = mcp_servers
        self._resolve_ref = resolve_ref
        # `broker` (task-14 brief, codex adapter): when set, `call()` itself
        # consults it before dispatching -- an AUTHORITATIVE gate, not just
        # a courtesy. Default `None` preserves every existing caller's
        # behavior unchanged (Claude/Langchain already consult their own
        # `ctx.broker` *before* ever calling `ToolHost.call()`, so wiring one
        # here too would ask the same broker twice per call -- harmless for
        # a stateless allow/deny broker, but wrong for "ask" semantics,
        # which blocks on a human and must not be asked the same question
        # twice). It exists because `ToolHost.call()` is *also* reachable
        # directly over `serve_socket()`'s unix socket (task-12) by an
        # out-of-process engine (Codex, via `tradewind.toolproxy`) with no
        # broker-aware layer of its own in front of that path -- for that
        # caller, this is the only place a deny can be enforced no matter
        # how the engine's own approval machinery decided. `turn_runner.py`
        # wires this in only for backends that need it (currently: codex).
        self._broker = broker
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
        self._socket_keeper: asyncio.Task[None] | None = None
        self._socket_stop: anyio.Event | None = None
        # Guards `serve_socket`'s create-if-absent section (fix round 1,
        # Important): without it, two concurrent first callers could both
        # pass the `self._socket_path is None` check and each start a
        # listener, leaking one.
        self._socket_lock = anyio.Lock()

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
        returns the existing path without starting another listener; this
        holds even for concurrent callers racing the first call (`_socket_
        lock` serializes the create-if-absent section, so only one of them
        actually starts a listener -- the rest see `_socket_path` already
        set once they get the lock and return that). Usable with or without
        `async with host:` -- it only touches the local `_tools`/
        `_mcp_targets` registry (via `schemas()`/`call()`), not the MCP
        client sessions `__aenter__` connects. `stop_socket()` (which
        `__aexit__` also calls) stops the server and removes the socket
        file.

        Security: the socket is a bare unix domain socket with no
        authentication of its own -- anyone who can connect to it can call
        every tool this `ToolHost` exposes. A directory `serve_socket`
        creates (no `socket_dir` given, or a `socket_dir` that doesn't yet
        exist) is always created `0o700` (owner-only), regardless of umask,
        so the socket inside it is only reachable by the current user. A
        caller-supplied `socket_dir` that already exists is left exactly as
        the caller made it -- `serve_socket` does not alter permissions on a
        directory it did not create; callers are responsible for that dir's
        own permissions.
        """
        already_serving = self._socket_path
        if already_serving is not None:
            return already_serving
        async with self._socket_lock:
            started_while_waiting = self._socket_path
            if started_while_waiting is not None:
                return started_while_waiting
            if self._socket_dir is not None:
                socket_dir = self._socket_dir
                created_by_us = not socket_dir.exists()
            else:
                # `tempfile.mkdtemp()` already creates its directory `0o700`.
                socket_dir = Path(tempfile.mkdtemp(prefix="tradewind-tp-"))
                created_by_us = True
            socket_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if created_by_us:
                # `mkdir`'s `mode` is masked by umask, so force it: a
                # world/group-readable dir would make the socket inside it
                # connectable (and every registered tool callable) by any
                # local user.
                socket_dir.chmod(0o700)
            # Short, unique filename: unix socket paths are capped at
            # ~104-108 bytes (`sockaddr_un.sun_path`), and a caller-supplied
            # `socket_dir` can already be deep (e.g. a system tempdir).
            socket_path = socket_dir / f"{uuid.uuid4().hex[:8]}.sock"
            listener = await anyio.create_unix_listener(socket_path)
            ready = anyio.Event()
            stop = anyio.Event()
            # A plain `asyncio.Task` running `_socket_keeper_run`, not a
            # `TaskGroup`/`CancelScope` entered directly here, is deliberate:
            # `stop_socket()` must be callable from a *different* task than
            # whichever task called `serve_socket()` -- e.g. `__aexit__`
            # running in a task other than the one that started the socket,
            # or (fix round 1) N concurrent callers racing this method under
            # `_socket_lock`, only one of which reaches this line. anyio's
            # `TaskGroup`/`CancelScope.__aexit__` hard-requires the exiting
            # task to be the same one that entered it
            # (`current_task() is not self._host_task` -> `RuntimeError`,
            # confirmed against `anyio/_backends/_asyncio.py`) -- and even
            # cancelling that scope from a different task, then leaving it
            # unexited, corrupts that *caller's own* cancel-scope stack once
            # the caller task finishes (confirmed empirically: a real
            # `RuntimeError` from anyio's own per-task scope bookkeeping).
            # `_socket_keeper_run` sidesteps this by owning its inner
            # `TaskGroup` entirely itself -- entered and exited by the same
            # (keeper) task, so no cross-task rule is ever bent -- while
            # being *reachable* from any task via the plain `asyncio.Task`
            # wrapping it: `.cancel()`/`await` a bare `asyncio.Task` has no
            # entering-task restriction. (A bare `asyncio.Task` running
            # `listener.serve()` *directly*, with no inner `TaskGroup`
            # wrapping it, was tried and rejected: `anyio`'s own
            # `UNIXSocketListener.accept()` -- reached via `listener.serve`
            # -- never wakes from `aclose()` when its enclosing task isn't
            # one `anyio` created itself, confirmed by a minimal repro. It
            # works fine once `_serve_socket_forever` is spawned through a
            # real `TaskGroup.start_soon`, which `_socket_keeper_run` does.)
            keeper = asyncio.ensure_future(self._socket_keeper_run(listener, ready, stop))
            # Assigned *before* awaiting readiness (fix round 2, Important):
            # if the task awaiting `serve_socket()` is cancelled while
            # suspended at `ready.wait()` below, `stop_socket()` must still
            # be able to reach this listener/keeper to tear it down --
            # otherwise the cancellation leaks a live, listening socket
            # nothing can ever reach again (the local `listener`/`keeper`
            # variables would simply die with the cancelled call).
            self._socket_listener = listener
            self._socket_keeper = keeper
            self._socket_stop = stop
            self._socket_path = socket_path
            try:
                await ready.wait()
            except BaseException:
                # Cancelled (or some other failure) mid-wait: tear the
                # now-orphaned server back down before propagating,
                # shielded so this cleanup can't itself be cut short by the
                # same (or a further) cancellation.
                with anyio.CancelScope(shield=True):
                    stop.set()
                    await listener.aclose()
                    await keeper
                socket_path.unlink(missing_ok=True)
                self._socket_listener = None
                self._socket_keeper = None
                self._socket_stop = None
                self._socket_path = None
                raise
            return socket_path

    async def _socket_keeper_run(
        self, listener: SocketListener, ready: anyio.Event, stop: anyio.Event
    ) -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._serve_socket_forever, listener)
            ready.set()
            await stop.wait()
            tg.cancel_scope.cancel()

    async def _serve_socket_forever(self, listener: SocketListener) -> None:
        # `listener.aclose()` (from `stop_socket`) unblocks the pending
        # `accept()` inside `serve()` with `ClosedResourceError`; the
        # `_socket_keeper_run` cancel scope closing unblocks it with a plain
        # cancellation instead -- both are expected shutdown, not a crash.
        with contextlib.suppress(anyio.ClosedResourceError):
            await listener.serve(self._handle_socket_connection)

    async def stop_socket(self) -> None:
        """Stop the socket server started by `serve_socket` and remove the
        socket file. A no-op if no socket server is running.

        Safe to call from any task *except* the keeper's own subtree (see
        the comment in `serve_socket` on why the keeper task is a plain
        `asyncio.Task` running its own self-contained `TaskGroup` -- that is
        what makes calling this from an *unrelated* task safe in the first
        place). The keeper task itself is guarded below (raises
        `RuntimeError`): waiting for `keeper` from inside `keeper` itself
        would deadlock forever. A tool handler invoked *through* this
        socket is a narrower, unguarded instance of the same problem --
        `_handle_socket_connection`, and thus any tool call it dispatches
        via `call()`, runs as a descendant of the keeper's inner
        `TaskGroup` (spawned via `_socket_keeper_run`'s `tg.start_soon`),
        so calling `stop_socket()` from such a handler deadlocks the same
        way, just not detectably from here (fix round 2, ruling: documented
        restriction, not fully enforced -- accepted risk). Tool handlers
        served by this socket must never call `stop_socket()`.
        """
        listener = self._socket_listener
        keeper = self._socket_keeper
        stop = self._socket_stop
        socket_path = self._socket_path
        if listener is None or keeper is None or stop is None or socket_path is None:
            return
        if asyncio.current_task() is keeper:
            raise RuntimeError(
                "ToolHost.stop_socket() must not be called from the socket server's own "
                "keeper task -- awaiting it here would deadlock waiting for this same "
                "task to finish. (Narrow guard: a tool handler invoked *through* this "
                "socket runs as a descendant of the keeper task, not the keeper task "
                "itself, and hits the same deadlock undetected -- never call "
                "stop_socket() from a tool handler served by this socket.)"
            )
        stop.set()
        await listener.aclose()
        await keeper  # waits for the keeper's own TaskGroup to fully wind down
        socket_path.unlink(missing_ok=True)
        self._socket_listener = None
        self._socket_keeper = None
        self._socket_stop = None
        self._socket_path = None

    async def _handle_socket_connection(self, stream: SocketStream) -> None:
        buffered = BufferedByteReceiveStream(stream)
        async with stream:
            while True:
                try:
                    line = await buffered.receive_until(b"\n", protocol.MAX_LINE_BYTES)
                except anyio.IncompleteRead:
                    return
                except anyio.DelimiterNotFound:
                    # Oversized line: an error response, not a silent close
                    # (fix round 1, Minor). The oversized bytes stay stuck at
                    # the front of `buffered`'s internal buffer with no safe
                    # resync point, so this connection still ends here -- but
                    # the caller sees why, and the *server* (other
                    # connections, and new ones after this) is unaffected.
                    error = protocol.ErrorResponse(error="line too long")
                    await self._send_response_line(stream, error)
                    return
                response = await self._answer_unless_abandoned(stream, buffered, line)
                if response is None or not await self._send_response_line(stream, response):
                    return

    async def _answer_unless_abandoned(
        self, stream: SocketStream, buffered: BufferedByteReceiveStream, line: bytes
    ) -> protocol.Response | None:
        """Answer one request line, or return `None` if the peer closes the
        connection first. A closed peer means the engine gave up on the call
        (05 "Port and interface changes"): the call is cancelled while it is
        still before its tool runs, so a pending broker question is
        cancelled and a late "ask" answer can never start the tool. Once the
        broker has allowed the call and the tool (local handler or
        MCP-proxied call) has started, the tool runs in a shielded scope
        (`_call(shield_dispatch=True)`): an approved action finishes or
        fails cleanly, and only its reply is discarded. Bytes that arrive
        while the call runs (a second, pipelined request line) are fed back
        into `buffered`, so the protocol is unchanged. An exception from the
        call is re-raised as itself, not wrapped in the task group's
        `ExceptionGroup`, so callers see the same exception type as before
        this method existed. Limitation: a peer that half-closes its write
        side after sending (`send_eof`) is treated as gone; the shim never
        does that."""
        answers: list[protocol.Response] = []
        failures: list[Exception] = []
        async with anyio.create_task_group() as tg:

            async def answer() -> None:
                try:
                    answers.append(await self._handle_socket_line(line))
                except Exception as exc:  # re-raised below, outside the task group
                    failures.append(exc)
                tg.cancel_scope.cancel()

            tg.start_soon(answer)
            while True:
                try:
                    data = await stream.receive()
                except (anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError):
                    tg.cancel_scope.cancel()
                    break
                buffered.feed_data(data)
        if failures:
            raise failures[0]
        return answers[0] if answers else None

    async def _send_response_line(self, stream: SocketStream, response: protocol.Response) -> bool:
        """Write one NDJSON response line; returns whether it was actually
        sent. If the peer already vanished, `stream.send()` can raise
        `BrokenResourceError`/`ClosedResourceError` (fix round 2, cheap
        hardening) -- left uncaught, that would propagate out of
        `_handle_socket_connection` and out of `listener.serve()`'s
        internal task group, killing the *whole* socket server over one
        departed peer. Caught here instead: the caller just ends this one
        connection (`async with stream:` closes it) and the server carries
        on."""
        try:
            await stream.send((protocol.encode_response(response) + "\n").encode("utf-8"))
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            return False
        return True

    async def _handle_socket_line(self, line: bytes) -> protocol.Response:
        try:
            request = protocol.decode_request(line.decode("utf-8"))
        except (protocol.ProtocolError, UnicodeDecodeError) as exc:
            # Malformed request: an error response, never a crash (brief) --
            # the connection stays open for the caller's next line.
            return protocol.ErrorResponse(error=str(exc))
        if isinstance(request, protocol.ListRequest):
            return protocol.ListResponse(tools=self.schemas())
        outcome = await self._call(request.name, request.arguments, shield_dispatch=True)
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
        result: list[dict[str, object]] = (
            []
            if self._result_schema is None
            else [
                {
                    "name": RESULT_TOOL_NAME,
                    "description": "Submit the final result of this task. "
                    "The arguments must match the input schema.",
                    "input_schema": self._result_schema,
                }
            ]
        )
        return local + list(self._mcp_schemas.values()) + result

    async def call(self, name: str, arguments: dict[str, object]) -> ToolOutcome:
        return await self._call(name, arguments, shield_dispatch=False)

    async def _call(
        self, name: str, arguments: dict[str, object], *, shield_dispatch: bool
    ) -> ToolOutcome:
        """`shield_dispatch` (socket path only): once the broker has allowed
        the call, run the tool in a shielded scope so an abandoned
        connection cannot stop an approved action half-way. In-process
        callers (`call()`) keep today's behaviour: cancelling them cancels
        the tool."""
        if name == RESULT_TOOL_NAME and self._result_validator is not None:
            # Structural pass-through (05 §2.3): never asks the broker.
            return self._submit_result(self._result_validator, arguments)
        if name not in self._tools and name not in self._mcp_targets:
            return ToolOutcome(content=f"unknown tool: {name!r}", is_error=True)
        if self._broker is not None:
            decision = await self._broker.decide(name, arguments)
            verdict, reason, _terminate = normalize_decision(decision)
            if verdict == "deny":
                # FR-4.4: the reason reaches the model as the error result.
                # `terminate` is NOT deliverable on this socket path (no
                # handle to interrupt from here) -- a recorded Phase-3
                # limitation, not a silent drop (spec + RUNBOOK).
                return ToolOutcome(content=reason or "permission denied", is_error=True)
        with anyio.CancelScope(shield=shield_dispatch):
            if name in self._tools:
                outcome = await self._call_local(self._tools[name], arguments)
            else:
                outcome = await self._call_mcp(name, arguments)
        return outcome

    def _submit_result(
        self,
        error_messages: Callable[[dict[str, object]], list[str]],
        arguments: dict[str, object],
    ) -> ToolOutcome:
        slot = self.result_slot
        if slot.attempts >= MAX_RESULT_ATTEMPTS:
            return ToolOutcome(content="No more attempts.", is_error=True)
        slot.attempts += 1
        errors = error_messages(arguments)
        if errors:
            return ToolOutcome(content="\n".join(errors), is_error=True)
        slot.last_valid = dict(arguments)
        return ToolOutcome(content="Result recorded.", is_error=False)

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
