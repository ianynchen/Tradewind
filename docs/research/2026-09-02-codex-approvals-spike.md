# Spike: Codex approval events → `PermissionBroker` (P-2)

Date: 2026-09-02. Timeboxed spike (~0.5 day), task 13. Verified against a
real `codex app-server` subprocess (subscription auth, `codex` CLI 0.147.0
already logged in on this machine) driven through the pinned SDK. Not
production code — a throwaway scratch project
(`uv init` + `uv add openai-codex==0.147.0` + `uv add mcp`) at
`/private/tmp/.../scratchpad/codex-spike`, never touching tradewind's
`pyproject.toml`. Scripts and full run logs referenced below live only in
that scratch dir; this document is the durable artifact.

**Pinned version.** `openai-codex==0.147.0` on PyPI, matching the installed
`codex` CLI (`codex-cli 0.147.0`) — confirmed as the same official package
the prior research doc (`docs/research/2026-09-01-agent-backends.md`)
cites, not a PyPI lookalike. `pip index versions openai-codex` also listed
`0.144.4`; `0.147.0` was current at spike time.

## 1. Exact hook point

**The friendly `Codex` / `AsyncCodex` / `AsyncCodexClient` API classes do
not expose an approval hook at all.** Only the low-level, synchronous
`openai_codex.client.CodexClient` constructor takes one:

```python
# openai_codex/client.py
ApprovalHandler = Callable[[str, JsonObject | None], JsonObject]

class CodexClient:
    def __init__(
        self,
        config: CodexConfig | None = None,
        approval_handler: ApprovalHandler | None = None,
    ) -> None:
        ...
        self._approval_handler = approval_handler or self._default_approval_handler
```

Verified by reading the constructors of all four public wrappers:
`Codex.__init__` (`api.py:82`) builds `CodexClient(config=config)` with no
`approval_handler` parameter and no way to pass one through; `AsyncCodex`
does the same; `AsyncCodexClient.__init__` (`async_client.py:55`) is
`CodexClient(config=config)` — also no `approval_handler` param anywhere in
its signature. So **a tradewind Codex adapter cannot use `Codex`/`AsyncCodex`
if it wants broker-driven approvals** — it must construct a raw
`CodexClient` itself, call `.start()`/`.initialize()`/`.thread_start(...)`
directly (bypassing `Codex.thread_start`'s convenience wrapper only if it
also needs the raw `approvals_reviewer=user` override — see §3), and then
wrap the returned thread id in `openai_codex.api.Thread(client, thread_id)`
(a plain `@dataclass(slots=True)` with public fields `_client`/`id`, so
constructing it by hand is legitimate, not a private-API hack) to keep using
the ergonomic `.turn()`/`.run()` methods.

**Request routing (the actual hook).** `CodexClient` runs one background
reader thread (`_reader_loop`, `client.py:803`) that classifies every
incoming JSON-RPC line from the `codex app-server` subprocess. A message
with both `method` and `id` is a **server-initiated request** (not a
notification — notifications have `method` but no `id`, per the prior
research doc's characterization), and is dispatched synchronously, on that
same reader thread, to the approval handler:

```python
def _reader_loop(self) -> None:
    while True:
        msg = self._read_message()
        if "method" in msg and "id" in msg:
            response = self._handle_server_request(msg)
            self._write_message({"id": msg["id"], "result": response})
            continue
        ...

def _handle_server_request(self, msg: dict[str, JsonValue]) -> JsonObject:
    method = msg["method"]
    params = msg.get("params")
    return self._approval_handler(method, params if isinstance(params, dict) else None)

def _default_approval_handler(self, method: str, params: JsonObject | None) -> JsonObject:
    if method == "item/commandExecution/requestApproval":
        return {"decision": "accept"}
    if method == "item/fileChange/requestApproval":
        return {"decision": "accept"}
    return {}
```

**This is the exact hook point tradewind's Codex adapter must use:** the
`approval_handler: Callable[[str, JsonObject | None], JsonObject]` argument
to `CodexClient.__init__`. Neither `method` nor `params` is a typed pydantic
model — confirmed by grepping the generated model file
(`generated/v2_all.py`) for `requestApproval`/`ExecCommandApproval`/
`FileChangeApproval`: no hits. These two RPC surfaces (plus a third found
live, §4) are deliberately left as raw `JsonObject` in the SDK — there is no
compile-time contract, matching the prior research doc's note that approval
requests are untyped JSON-RPC server-requests.

**Threading consequence for the broker bridge.** The handler runs
synchronously on `CodexClient`'s own reader thread, not on any asyncio event
loop. tradewind's `PermissionBroker.decide()` is `async def`. Since
`AsyncCodexClient` doesn't accept `approval_handler` in the first place, a
tradewind Codex adapter must write its own thin async wrapper (mirroring
what `AsyncCodexClient` does for *outbound* calls via `asyncio.to_thread`,
but in the opposite direction for this *inbound* callback): capture the
adapter's running event loop once (`asyncio.get_running_loop()`) before
starting the client, and inside the sync `approval_handler` callback do
```python
future = asyncio.run_coroutine_threadsafe(broker.decide(name, input), loop)
verdict = future.result()  # blocks the reader thread until the broker decides
```
This is unavoidable — it is the same shape of bridge Claude's adapter does
*not* need (Claude's `can_use_tool` is itself an `async def` called from
within the SDK's own asyncio machinery).

## 2. `approval_mode` vs. the raw wire params — reviewer routing matters

The SDK's public `ApprovalMode` enum (`_approval_mode.py`) only maps to two
of the three real `approvals_reviewer` values:

| `ApprovalMode` | wire `approval_policy` | wire `approvals_reviewer` |
|---|---|---|
| `deny_all` | `never` | `None` (approvals reviewer unset) |
| `auto_review` | `on-request` | `ApprovalsReviewer.auto_review` |

`ApprovalsReviewer` (generated enum) actually has three members: `user`,
`auto_review`, `guardian_subagent`. **`auto_review` routes escalations to
Codex's own internal automated reviewer, not to the client's
`approval_handler`.** To reach the client callback at all, `thread_start`
must be called with the raw, un-enum-wrapped params —
`approvals_reviewer=ApprovalsReviewer.user` — which the convenience
`Codex.thread_start(approval_mode=...)` wrapper has no way to request. This
is why the adapter needs the raw `CodexClient.thread_start(ThreadStartParams(...))`
call directly rather than going through `Codex`/`Thread` for session setup
(verified: `ApprovalsReviewer.user` never appears anywhere in `api.py` or
`_approval_mode.py` — it is reachable only by hand-building
`ThreadStartParams`). Params used in the live run:

```python
ThreadStartParams(
    cwd=workdir,
    sandbox=SandboxMode.read_only,   # forces a write to escalate
    approval_policy=AskForApproval(root=AskForApprovalValue.on_request),
    approvals_reviewer=ApprovalsReviewer.user,
)
```

## 3. Live verification — exec approval (§ task step 3)

Script: `exp_exec_approval.py` (scratch dir). Thread started as above, two
turns, each asking Codex to run `touch <workdir>/{approved,denied}.txt`.
`approval_handler` counted commandExecution requests and returned `accept`
on the first, `reject` on the second.

**Captured request (verbatim, method + params), turn 1:**
```json
{
  "method": "item/commandExecution/requestApproval",
  "params": {
    "threadId": "01a063d5-adc1-73c0-a05d-329f356f2984",
    "turnId": "01a063d5-ae4b-7870-a165-c9ec8e7973e1",
    "itemId": "exec-79c9373b-e6e5-4c4c-9a34-bd0f891f19bd",
    "startedAtMs": 1788381352022,
    "environmentId": "local",
    "reason": "Do you want to allow creating approved.txt in the workspace?",
    "command": "/bin/zsh -lc 'touch /var/.../approved.txt'",
    "cwd": "/var/.../codex-spike-exec-3p8m66yw",
    "commandActions": [{"type": "unknown", "command": "touch /var/.../approved.txt"}],
    "proposedExecpolicyAmendment": ["touch", "/var/.../approved.txt"],
    "availableDecisions": [
      "accept",
      {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["touch", "/var/.../approved.txt"]}},
      "cancel"
    ]
  }
}
```
Response sent: `{"decision": "accept"}`. Effect: the `commandExecution` item
completed with `"status": "completed", "exit_code": 0`, and
`approved.txt` existed on disk afterward.

**Turn 2 (identical shape, `denied.txt`).** Response sent:
`{"decision": "reject"}`. Effect: the `commandExecution` item completed
with `"status": "failed", "exit_code": null, "duration_ms": null,
"aggregated_output": null` (the command never ran — this is not a
nonzero-exit failure, it's a not-executed-at-all state), `denied.txt` did
**not** exist on disk, and the agent's final message acknowledged it
couldn't create the file.

**Note on `availableDecisions`.** The request itself only *advertises*
`"accept"`, `{"acceptWithExecpolicyAmendment": {...}}}`, and `"cancel"` —
`"reject"` is not in that list, yet `{"decision": "reject"}` was accepted on
the wire and produced the denial effect above (matches the shape the SDK's
own `_default_approval_handler` would send for a decline, by symmetry with
its `accept` case — though the SDK never actually exercises a reject path
itself). Since this exchange is untyped (§1), treat the accepted decision
literals (`"accept"` / `"reject"`, possibly also `"cancel"`) as an
**observed-behavior contract of `codex app-server` 0.147.0, not a
documented one** — re-verify against whatever version Task 14 pins.

`item/fileChange/requestApproval` was **not independently exercised live**
(no turn in this spike triggered a file write, only a shell `touch`, which
in this sandbox/policy combination surfaced as `commandExecution`, not
`fileChange`) — the mapping used above assumes symmetry with
`commandExecution` based on `_default_approval_handler` treating both
identically. Flagged as unverified; low risk given the identical code path,
but Task 14 should add one live case for it before relying on it.

## 4. MCP tool calls vs. exec/patch approvals (§ task step 4) — the key finding

**Answer: MCP tool calls DO trigger an approval-shaped request, but through
a third, different JSON-RPC method and response schema — not
`item/*/requestApproval`.** Verified with a real stdio MCP server
(`echo_server.py`, one tool `echo(text) -> str`, built with the `mcp` PyPI
package — note: `mcp>=2.0` renamed `FastMCP` to `MCPServer` at
`mcp.server.mcpserver`, a compatibility trap for anyone copying older
snippets) registered via `CodexConfig(config_overrides=(...))`:

```python
CodexConfig(config_overrides=(
    'mcp_servers.echo.command="/path/to/venv/python3"',
    'mcp_servers.echo.args=["/path/to/echo_server.py"]',
))
```
(confirmed live via the `mcpServerStatus/list` JSON-RPC request — not part
of the public `Codex`/`Thread` API, called directly on `CodexClient` — that
the server connected and its tool was discovered: `{"name": "echo", ...,
"tools": {"echo": {...}}}`, alongside several of this machine's
pre-existing global `~/.codex` MCP servers picked up because the spike
didn't isolate `CODEX_HOME`).

Asking the agent to call the `echo` tool produced this **verbatim** captured
server-request — not an `item/mcpToolCall/requestApproval` as the naming
convention of the other two would suggest, but:

```json
{
  "method": "mcpServer/elicitation/request",
  "params": {
    "threadId": "01a063d8-3dfb-7771-b3a7-35e472e30392",
    "turnId": "01a063d8-3e78-7bb0-ac92-1d8d7408fe71",
    "serverName": "echo",
    "mode": "form",
    "_meta": {
      "codex_approval_kind": "mcp_tool_call",
      "persist": ["session", "always"],
      "tool_description": "Echo back the given text, verbatim.",
      "tool_params": {"text": "hello mcp"},
      "tool_params_display": [{"name": "text", "value": "hello mcp", "display_name": "text"}]
    },
    "message": "Allow the echo MCP server to run tool \"echo\"?",
    "requestedSchema": {"type": "object", "properties": {}}
  }
}
```

First attempt answered this with the exec-style shape
(`{"decision": "accept"}`), which is **wrong for this method** and was
silently treated as a decline: the `mcpToolCall` item completed with
`"status": "failed", "error": {"message": "user rejected MCP tool call"}`.
This is a real footgun — an approval_handler that only special-cases
`item/commandExecution/requestApproval` / `item/fileChange/requestApproval`
and returns some default for everything else will **silently deny every
MCP tool call** tradewind routes through Codex, with no exception raised.

Answering with the MCP-elicitation-shaped response instead —
`{"action": "accept", "content": {}}` (standard MCP elicitation reply:
`action ∈ {"accept","decline","cancel"}` + `content` matching
`requestedSchema`, here an empty object schema) — correctly ran the tool:
the `mcpToolCall` item completed with `"status": "completed"`, `"result":
{"content": [{"type": "text", "text": "echo: hello mcp"}], ...}`, and the
agent's final message was `"echo: hello mcp"`.

**Consequence for Task 14's design (this is the part that matters most):**

1. tradewind tools reach Codex the same way established for MCP generally
   (prior research §1.2): as tradewind's own hosted MCP stdio server,
   registered into Codex via `config_overrides`. Since that MCP path *is*
   gated by Codex's approval machinery (contrary to what the brief flagged
   as a possible bypass risk), the adapter's `approval_handler` **must**
   branch on method, not just on `item/commandExecution` vs
   `item/fileChange`:
   - `item/commandExecution/requestApproval`, `item/fileChange/requestApproval`
     → tool_name/input derived from `command`/`changes`; respond
     `{"decision": "accept"|"reject"}`.
   - `mcpServer/elicitation/request` **where** `params["_meta"]["codex_approval_kind"]
     == "mcp_tool_call"` → this is the one that fires for every tradewind
     tool call; respond `{"action": "accept"|"decline", "content": {}}`.
     (`mcpServer/elicitation/request` is presumably also used for other MCP
     elicitation kinds in general — gating on the `_meta.codex_approval_kind`
     discriminator, not just the method name, keeps the branch specific to
     tool-call approval and avoids misinterpreting some other elicitation
     shape as a permission decision.)
2. **Tool-name extraction is the open problem.** Unlike
   `commandExecution`/`fileChange`, this payload has **no structured tool-name
   field** — `serverName` identifies the MCP *server* (`"echo"`, or
   tradewind's single `"tradewind"` server, mirroring the Claude adapter's
   `_MCP_SERVER_NAME`), not which of tradewind's many tools was called. The
   only place the tool name appears is embedded in the human-readable
   `message` string (`Allow the {serverName} MCP server to run tool
   "{tool}"?`) — extractable with a regex
   (`r'run tool "(.*)"\?$'`) against this exact 0.147.0 wording, but this is
   *not* a documented, stable field; a phrasing change in a future Codex
   release would silently break `bare_name` extraction the way
   `claude_backend.py`'s `_strip_server_prefix` currently relies on a
   confirmed-empirically (not documented) `mcp__<server>__<tool>` naming
   convention. `_meta.tool_params`/`tool_description` are corroborating
   signal (tradewind knows its own tool schemas and could cross-check), but
   the regex on `message` is the only reliable source of the bare name.
   **This should be flagged for controller confirmation in the Task 14
   brief/report**, same as the two flags already on record in
   `claude_backend.py`'s module docstring for the Claude adapter.
3. Since tool execution still ultimately happens through tradewind's own
   `ToolHost.call`, **`ToolHost.call` remains a valid, Codex-version-proof
   fallback/defense-in-depth gate regardless of how reliably the adapter can
   parse `mcpServer/elicitation/request`** — exactly as the task brief
   anticipated. The Codex `approval_handler` branch above is what lets
   tradewind's Codex adapter *deny before Codex's own agent loop shows the
   user/model any tool result* (matching the UX Claude's `can_use_tool` gate
   gives, where a denial is visible as a first-class turn event rather than
   a tool call that silently never happened), but it is not the only place
   correctness can be enforced.

## 5. Chosen broker mapping for Task 14

```python
def _make_approval_handler(
    broker: PermissionBroker,
    loop: asyncio.AbstractEventLoop,
    pending_events: list[Event],
) -> ApprovalHandler:
    def approval_handler(method: str, params: JsonObject | None) -> JsonObject:
        params = params or {}
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            tool_name = "shell" if "command" in method else "apply_patch"  # exact mapping TBD in task 14
            tool_input = {"command": params.get("command")} if "command" in method else {"changes": params.get("changes")}
            verdict = asyncio.run_coroutine_threadsafe(
                broker.decide(tool_name, tool_input), loop
            ).result()
            if verdict == "deny":
                pending_events.append(PermissionRequested(tool_name=tool_name, tool_input=tool_input, verdict="deny"))
                return {"decision": "reject"}
            return {"decision": "accept"}

        if method == "mcpServer/elicitation/request" and (params.get("_meta") or {}).get("codex_approval_kind") == "mcp_tool_call":
            bare_name = _extract_tool_name(params)  # regex on params["message"], see §4.2
            tool_input = (params.get("_meta") or {}).get("tool_params", {})
            verdict = asyncio.run_coroutine_threadsafe(
                broker.decide(bare_name, tool_input), loop
            ).result()
            if verdict == "deny":
                pending_events.append(PermissionRequested(tool_name=bare_name, tool_input=tool_input, verdict="deny"))
                return {"action": "decline", "content": {}}
            return {"action": "accept", "content": {}}

        return {}  # anything else: SDK's own default-accept behavior doesn't apply here since we own the handler now; explicit {} matches the SDK default for unrecognized methods, i.e. effectively a fail-closed no-op
    return approval_handler
```

This mirrors `claude_backend.py`'s `_make_can_use_tool` shape exactly:
`allow` → let it run, `deny` → block it and record a `PermissionRequested`
event with `verdict="deny"` (FR-4.1), and the broker itself owns any
human-in-the-loop "ask" blocking (per `PermissionBroker.decide`'s
docstring). The two differences from Claude, both load-bearing:
- Codex needs **two** response shapes (`{"decision": ...}` vs.
  `{"action": ..., "content": {}}`) depending on which of the three methods
  fired, where Claude has one uniform `can_use_tool` callback for
  everything.
- The handler is **sync**, called from a non-event-loop thread, and must
  bridge into the async broker via `run_coroutine_threadsafe(...).result()`
  (§1) — Claude's `can_use_tool` is natively awaitable inside the SDK's own
  loop.

## 6. Limitations / unknowns carried forward

1. `item/fileChange/requestApproval` — mapped by symmetry with
   `commandExecution`, not independently live-verified (§3).
2. The accepted decision literals for `item/*/requestApproval`
   (`"accept"`/`"reject"`, possibly `"cancel"`) are an observed-behavior
   contract of `codex app-server` 0.147.0 (`availableDecisions` in the
   request itself didn't even list `"reject"` as an option, yet it worked)
   — not documented, not typed in the SDK. Re-verify on whatever version
   Task 14 pins.
3. Tool-name extraction for `mcpServer/elicitation/request` has no
   structured field; the regex-on-`message` approach (§4, point 2) is the
   only reliable source found and is not guaranteed stable across Codex
   releases. Needs controller confirmation before Task 14 relies on it.
4. Whether `mcpServer/elicitation/request` fires for *every* tradewind tool
   call regardless of `approval_policy`/`sandbox`, or whether some
   combination of settings (e.g. a fully-trusted/allow-listed MCP server)
   skips it, was not swept — the spike used one fixed
   `approval_policy=on-request, approvals_reviewer=user` combination
   throughout. Task 14 should confirm the policy/sandbox matrix that
   actually produces this request before assuming it always fires.
5. `mcpServerStatus/list` and other raw `CodexClient.request(...)` calls
   used during this spike are themselves untyped-by-convenience escape
   hatches outside `Codex`'s flat method surface — fine for a spike, but
   Task 14 should decide whether the adapter leans on more of these or
   stays within the documented flat-method surface plus just the one
   `approval_handler` extension point.
6. This spike ran against the personal, non-isolated `~/.codex` config
   (many pre-existing MCP servers were visible in `mcpServerStatus/list`
   output) — Task 14's adapter should pass an isolated `CODEX_HOME`/`cwd`
   per the DR-3 resume-planning note in ARCHITECTURE.md so a caller's real
   MCP server list never leaks into tradewind-driven threads by accident.

## 7. Source references

- `openai_codex/client.py` — `CodexClient`, `ApprovalHandler`, `_reader_loop`,
  `_handle_server_request`, `_default_approval_handler` (pip package
  `openai-codex==0.147.0`, installed at
  `.venv/lib/python3.13/site-packages/openai_codex/client.py` in the scratch
  project; upstream source is `sdk/python/src/openai_codex/client.py` in
  `github.com/openai/codex`).
- `openai_codex/async_client.py` — `AsyncCodexClient` (no `approval_handler`
  param).
- `openai_codex/api.py` — `Codex`, `AsyncCodex`, `Thread`, `TurnHandle`
  (no `approval_handler` param anywhere; `Thread`/`TurnHandle` are plain
  dataclasses, constructible by hand).
- `openai_codex/_approval_mode.py` — `ApprovalMode` enum, its mapping to
  `(AskForApproval, ApprovalsReviewer | None)`.
- `openai_codex/generated/v2_all.py` — `ApprovalsReviewer` (3 members:
  `user`, `auto_review`, `guardian_subagent`), `AskForApprovalValue`,
  `ThreadStartParams`, `ListMcpServerStatusParams/Response`; no typed model
  for either approval-request method or for `mcpServer/elicitation/request`.
- Live run logs (scratch dir, not committed):
  `exp_exec_approval.py` → captured exec approval requests/responses (§3);
  `exp_mcp_approval.py` + `echo_server.py` → captured MCP elicitation
  request/response (§4).
