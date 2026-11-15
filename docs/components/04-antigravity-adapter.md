# Component: Antigravity Adapter (ACP)

Status: design, owner-approved approach (2026-11-15). Not implemented.

## Purpose

The fifth `Backend` subclass (ARCHITECTURE §3.1): Google Antigravity, driven
through the official Agent Client Protocol server built into the Antigravity
CLI (`agy acp`, ACP protocol version 1 over stdio, CLI ≥ 1.5.3). It lets a
Tradewind caller run turns on the owner's Antigravity subscription (Google AI
Pro) and on every model that subscription offers (Gemini 4, Gemini 3.8 Flash,
Claude Opus/Sonnet 5.5, GPT-OSS 120B at the time of writing).

### Why

The owner wants Meridian to use Antigravity "for as many areas in Meridian as
possible": create and review PRDs, FRDs and blueprints; create work orders;
create, update and close issues through an issue tracker's MCP server; fetch
code, submit, review, comment on, approve and merge pull requests; deploy.
Those are Meridian features (later pieces). This adapter's job is to give
Meridian the four Tradewind capabilities they rest on, on Antigravity:

1. reliable structured output (reviews, documents, work orders);
2. caller tools and MCP servers in the session (issue management);
3. the agent's own shell, git and file tools, **every risky call approved by
   the caller's `PermissionBroker`** (fetch code, PRs, merge, deploy);
4. sessions that continue across turns (load, resume, cancel).

## Goals

- A complete `Backend` for Antigravity with the four capabilities above.
- Structured output that is additive (FR-1.3, as on the other backends): a turn
  without a schema is a plain ACP turn; a schema turn's content events and
  mirror are a plain turn's; the result lives only in
  `TurnResult.structured_output`; no valid result → the turn completes with
  `structured_output=None` and `final_text` kept.
- Every tool call that the agent asks the client to approve goes to the
  broker, and a broker denial is a denial (fail closed).
- Honest capability flags (R-1, FR-8): what ACP or `agy` cannot do is `False`
  and raises `Unsupported`.

## Non-goals

- LiteLLM (owner deferred, 2026-11-15).
- `agy` print mode (`-p --output-format stream-json --json-schema`) and any
  hybrid of print mode and ACP (approaches 2 and 3 below, rejected).
- The Meridian features themselves (PR workflow, deploy, issue sync). They are
  Meridian pieces that use this backend.
- Antigravity's API-key and Vertex auth methods. The point is the
  subscription; API keys already reach Gemini through other routes.
- The Antigravity Python SDK (`google-antigravity`): it has API-key and ADC
  auth only, no subscription.

## Anti-goals (a failure even if it works)

- A tool call that runs without the broker's approval when the broker would
  have denied it — above all `git push`, `gh pr merge`, a deploy command.
- A schema turn that silently returns prose as if it were the structured
  result, or a structured result that does not validate against the schema.
- Running with `--dangerously-skip-permissions` or Auto mode in any
  configuration Tradewind ships.

## Constraints

- **Terms of service (owner to settle before production use).** A reply on
  the Google developer forum (not confirmed as Google staff) said driving
  `agy` from another program with a personal Pro/Ultra login breaks the
  Antigravity terms. ACP is official and made for editors, so this may be
  acceptable, but no Google statement confirms that a program like Tradewind
  may use the subscription through `agy acp`. The owner knows; this design does
  not decide it.
- ACP v1 has no output-schema field. `agy acp` advertises only
  `_meta.antigravity.systemPrompt.append` (and `tier`) as extensions; the
  engine's internal finish-tool schema (`FinishToolConfig`) is reachable only
  from print mode's `--json-schema`. Verified on agy 1.5.3 by `initialize` /
  `session/new` and by the binary's symbols.
- `agy` itself may approve some tool calls without asking the client
  (permission rules in its `settings.json`, the default temp-directory grant,
  read-only access to its customization directory, modes such as
  `accept-edits`). Such calls never reach the broker. See Decisions →
  Permissions.
- GUIDELINES bind: no CI change, no version bump, match existing patterns; a
  new runtime dependency needs owner approval.
- Live tests stay out of CI (module env gate + `integration` marker), are
  time-boxed, and use the owner's subscription quota.

## Approaches considered

| | Approach | Trade-off | Verdict |
|---|---|---|---|
| 1 | **ACP backend; structured output through a Tradewind result tool** | Native approvals (`session/request_permission`), MCP servers per session, load/resume/cancel, models, system prompt. Schema is enforced by Tradewind validation and one reminder, not by the engine; a turn can end with `None`. | **Chosen (owner, 2026-11-15)** |
| 2 | `agy` print mode (stream-json) | Engine-enforced schema. No approval callback: approvals would need a `PreToolUse` hook helper that calls back into Tradewind; if the helper fails, a merge may run unchecked. | Rejected: weakest where failure is worst |
| 3 | Both: ACP for plain turns, print mode for schema turns | Two protocols and two permission paths for one session. | Rejected: complexity |

Why 1: for merge and deploy, a missed approval is the worst failure; a `None`
result that Meridian retries (Meridian piece-4 decision 12) is the mildest.
Approach 1 has the strong approval path and the mild failure.

## Go/no-go gate (owner, 2026-11-15)

The first build task is a verification spike: with Tradewind's own
`--app-data-dir` and the strictest tool-permission setting `agy` offers, which
tool calls does `agy` run **without** sending `session/request_permission`?
Reads under the system temp directory and `agy`'s own customization directory
are expected and acceptable. If any risky call runs without asking (a shell
command, a file write outside the temp directory, `git push`, a network call,
an MCP call to a server other than the Tradewind shim), **work stops and the
findings go to the owner**. No adapter code is written before this gate
passes.

## Owns

- The `agy acp` subprocess for each turn (spawn, `initialize`, auth check,
  shutdown) and a minimal ACP v1 JSON-RPC client over its stdio.
- Mapping ACP `session/update` notifications onto the frozen event taxonomy
  (ARCHITECTURE §3.2) and the mirror.
- The approval bridge: `session/request_permission` → `PermissionBroker`.
- The result tool for structured output and its reminder.
- Its capability declaration.

## Depends on

The `Backend` port, `ToolHost` and its stdio shim (`ToolHost.shim_server_def()`,
`python -m tradewind.toolproxy` — the same bridge Codex uses), the
`PermissionBroker`, and an installed, logged-in `agy` (≥ 1.5.3) on `PATH`.

## Decisions

Owner decisions are marked **(owner)**. Everything else is the designer's
call, made to fit the existing backends; the builder may revisit a call only by
raising it, not silently.

| Decision | Choice | Why |
|---|---|---|
| Transport | ACP v1 over stdio, `agy acp` **(owner: way 1)** | Native approvals; see Approaches. |
| ACP client | A small in-repo JSON-RPC client for the ACP subset used | ACP v1 is small (a dozen methods); avoids a new runtime dependency. If the builder finds a maintained official Python ACP library clearly better, raise it for owner approval. |
| Auth | `SubscriptionAuth` only; `ApiKeyAuth` → `ConfigError` | The point is the subscription. Login is the operator's one-time `agy acp --login`; Tradewind never handles Google credentials. `session/new` returns `_meta.antigravity.tier`; a missing login surfaces as `ConfigError` with the login command. |
| Client capabilities | `fs.readTextFile=false`, `fs.writeTextFile=false`, `terminal=false` | The agent uses its own file, shell and git tools inside `agy`'s sandbox; Tradewind does not become the agent's file system or terminal. |
| Built-in tools | Enabled, broker-gated (like Codex) | Meridian needs shell, git and file edits. Disabling them (as on Claude/Cursor) would defeat the purpose. |
| Permissions | Session mode `default`; every `session/request_permission` → broker; allow → the request's one-time allow option (never "always allow"); deny → its reject option; broker error or timeout → reject (fail closed) | One-time options keep the broker in every decision. |
| Calls `agy` approves itself | Tradewind runs `agy acp` with its own `--app-data-dir` (a Tradewind-owned directory, not the operator's interactive `agy` settings), and that directory's settings set the strictest tool-permission level `agy` offers, with no allow rules. The builder records in RUNBOOK exactly which calls still skip the client (expected: reads under the temp directory and the customization directory) | The operator's personal `agy` allow rules must not widen what Tradewind's broker sees. Login in a separate app-data-dir is a separate `agy acp --login --app-data-dir …` (verify). |
| Caller tools and MCP servers | All exposed through the one `ToolHost` stdio shim in `session/new.mcpServers`; shim calls are auto-accepted at the ACP layer because `ToolHost.call()` consults the broker itself (exactly the Codex rule, `codex_backend.py` approval routing) | One tool path for all backends; no double broker consult. |
| System prompt | `_meta.antigravity.systemPrompt.append` on `session/new` | The only system-prompt channel `agy acp` offers; it appends, it does not replace. `supports_system_prompt=True`, documented as "appended to Antigravity's own prompt". |
| Model and effort | `ModelSpec.model` → `session/set_config_option` `model`; `ModelSpec.effort` → `thought_level` (low/medium/high); an effort `agy` lacks (`xhigh`) → `Unsupported` | Uses the session's advertised config options; values validated against `session/new`'s option list. |
| Structured output **(owner: way 1)** | See "Structured output" below | — |
| Tool-round cap | `supports_tool_round_cap=False` in v1 | Calls `agy` approves itself are invisible to the client, so Tradewind cannot count rounds exactly (FR-6.5 requires exact). Raise `Unsupported`, as Codex does. |
| Deny reason | `supports_deny_reason=False` | ACP's permission outcome carries only an option id; there is no reason channel. The reason is still recorded in `PermissionRequested` and the mirror. |
| Resume | `supports_native_resume=True` via `session/load` (or `session/resume`, whichever attaches without re-running; builder verifies) using the stored native session id. Updates that `session/load` replays are not re-emitted or re-mirrored | `agy` advertises load, resume, list and close. |
| Transcript read | `False` in v1 | `session/load` replays history as updates; using it as a transcript source is possible later but not needed. |
| Fork | `False` | No ACP fork. Client-level fork-by-copy (store) still applies where the store is the record. |
| Turn retry | `False` | The loop is `agy`'s; pre-turn spawn retry only, as for other SDK backends. |
| Interrupt | `session/cancel`; the turn ends without a terminal event (port contract) | ACP native. |
| Usage | Map `session/prompt` result usage / context-usage updates where present; absent fields stay `None` | `agy` 1.5.x sends usage and context-window updates. |
| Process model (designer's call, after the builder check) | **One `agy acp` process per turn**, as Codex starts one `codex app-server` per turn: spawn, `initialize`, `session/new` (first turn) or `session/load` (later turns), prompt, then `session/close` and terminate | `turn_runner` builds a new `ToolHost` (and its shim socket) for every turn and the port has no session-close hook, so a long-lived process would hold a dead shim socket and could never be closed. Per-turn also takes each turn's own tools and result tool. Cost: process start and `session/load` on every turn. |
| Changes outside the adapter (designer's call, minimal) | (1) `BackendName` gains `"antigravity"`. (2) `turn_runner` gives the broker to `ToolHost` for `antigravity` exactly as it does for `codex`, because shim calls are auto-accepted at the ACP layer and `ToolHost.call()` must gate them. (3) `ToolHost` accepts one optional, per-turn result tool (name, schema, recorder) that `turn_runner` supplies when the backend asks for it and `output_schema` is set; this tool skips the broker, like Claude's `StructuredOutput`. Nothing else outside the adapter changes. | Without (2), every shim tool runs unchecked: the top anti-goal. (3) is the smallest seam that gets the result tool into the per-turn shim. |
| `refusal`, `max_turn_requests` stop reasons (designer's call) | `TurnFailed` with a clear message (`"model refused"`, `"agent turn limit reached"`); no new `EndReason` member; no reminder | The `EndReason` list is shared by all backends; neither value means a clean finish or the caller's exact cap. Meridian treats a failed review as "not complete" and offers Retry. |

## Structured output (owner: way 1)

When `ctx.output_schema` is set:

1. Tradewind adds one tool to that turn's tool set, served through the same
   `ToolHost` shim: `tradewind_submit_result`, whose input schema **is** the
   caller's schema, and whose description says it records the final answer.
2. `systemPrompt.append` (set at `session/new`) is not per-turn, so the
   instruction goes in the turn's prompt as a trailing instruction block: "When
   you have finished, call `tradewind_submit_result` exactly once with your
   final answer." That block is not mirrored as user content (the mirror keeps
   the caller's prompt only), mirroring LangChain's un-stored instruction.
3. When the shim receives the call, Tradewind validates the arguments against
   the schema. Valid → stored as the turn's structured result; the tool
   returns "Result recorded." Invalid → the tool returns the validation error so
   the model can call again (counts toward the reminder budget below).
4. The `tradewind_submit_result` tool_call/tool_call_update notifications are
   hidden from events and the mirror (as Claude's `StructuredOutput` is).
5. Budget (designer's call): at most **3** result-tool calls per turn (valid
   or not; a 4th is answered "No more attempts." and not recorded), and
   exactly **one** reminder per turn, whatever happened before it.
   If the agent ends the turn (`stopReason: end_turn`) without a valid result,
   Tradewind sends **one** reminder prompt in the same session ("Call
   `tradewind_submit_result` now with your final answer."). The reminder and
   its reply's text are not mirrored as a new user turn; the reply's visible
   content is part of the same Tradewind turn.
6. Still no valid result → the turn completes, `end_reason="end_turn"`,
   `structured_output=None`, `final_text` = the last visible response text.
   Meridian then offers "Retry".
7. Without a schema, none of this happens: no extra tool, no instruction, no
   reminder.

Validation needs a JSON Schema validator. Meridian's schemas stay in the
vendors' strict subset, so the builder may implement the subset needed or
propose the `jsonschema` package (owner approval: new dependency).

## Capabilities (FR-8)

| Flag | Value |
|---|---|
| `supports_system_prompt` | True (appended) |
| `supports_structured_output` | True (result tool; may complete with `None`) |
| `supports_interactive_permissions` | True |
| `supports_in_process_tools` | False (tools reach the agent through the MCP shim, exactly as on Codex; same flag for the same mechanism, NFR-3) |
| `supports_native_resume` | True |
| `supports_fork` | False |
| `supports_transcript_read` | False |
| `supports_tool_round_cap` | False |
| `supports_turn_retry` | False |
| `supports_deny_reason` | False |

## Turn algorithm

1. Spawn this turn's `agy acp` process with Tradewind's `--app-data-dir`,
   `initialize` (protocol 1, client capabilities above), then `session/new`
   (cwd, `mcpServers=[this turn's shim]`, `_meta.antigravity.systemPrompt.
   append`) on the first turn, or `session/load` for the stored native id on
   later turns. Apply model and effort with `session/set_config_option`.
2. Emit `TurnStarted`. Send `session/prompt` with the caller's prompt (plus the
   schema instruction block if a schema is set).
3. Map each `session/update`: `agent_message_chunk` → `TextDelta`, thought
   chunks → thinking, `tool_call`/`tool_call_update` → tool items (hiding
   `tradewind_submit_result`), plan updates and usage → existing event kinds
   or ignored where the taxonomy has no slot (taxonomy is frozen).
4. Answer each `session/request_permission` through the broker (shim tools
   auto-accepted); emit `PermissionRequested` on deny as the other backends do.
5. On the `session/prompt` result: `end_turn` → structured-output steps 5–6 if
   a schema is set; `cancelled` → interrupted (no terminal event);
   `max_tokens` → `TurnCompleted` with `end_reason="max_tokens"`;
   `max_turn_requests`/`refusal` → `TurnFailed` (Decisions); a JSON-RPC error
   → `TurnFailed` with the agent's message (agy 1.5.x reports model and quota
   errors specifically).
6. `session/close`, then terminate the process.

## Acceptance

- Unit tests against a fake ACP agent (a scripted stdio JSON-RPC peer, no
  network): event mapping, approval bridge (allow, deny, broker error →
  reject, one-time options only), shim auto-accept, structured output (valid
  first call, invalid then valid, no call then reminder then valid, no result
  → `None`), hidden result-tool items, no-schema turn unchanged, resume via
  load, cancel, `ApiKeyAuth` and `xhigh` → errors, capability flags.
- Live conformance (`tests/conformance/test_antigravity.py`, env gate
  `TRADEWIND_RUN_ANTIGRAVITY_INTEGRATION`, `integration` marker, 3-minute
  time box, cheapest model), run once by the operator:
  1. plain text turn;
  2. structured output `{"ok": boolean}`;
  3. a shell command the broker **denies** never runs (e.g. a command that
     would create a marker file; assert the file does not exist);
  4. a caller tool through the shim;
  5. resume continues context.
- RUNBOOK: login (`agy acp --login` with Tradewind's app-data-dir), which calls
  `agy` approves without asking, the live-test command, the ToS note.
- ARCHITECTURE §3.1 backend list, README capability matrix, CHANGELOG.

## Left to the builder

- Exact ACP method and field names beyond those verified here (`initialize`,
  `session/new`, its `models`/`modes`/`configOptions`/`_meta` shape) — read
  the ACP v1 schema and confirm against `agy acp`.
- `session/load` versus `session/resume` for Tradewind's resume.
- Which `agy` setting gives the strictest tool permissions, and whether a
  separate `--app-data-dir` needs its own login.
- How shim-tool calls appear in `tool_call` updates (so they are not
  double-reported) — mirror the Codex adapter's handling.
