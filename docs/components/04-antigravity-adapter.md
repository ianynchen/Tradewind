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
  may use the subscription through `agy acp`. T3 Code's FAQ claims it is
  "within the TOS of … Google Antigravity" (`docs/user/faq.md:52`); that is
  T3's claim, not Google's. The owner knows; this design does not decide it.
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
| Auth | `SubscriptionAuth` only; `ApiKeyAuth` → `ConfigError`. After `initialize`, send `authenticate {methodId: "oauth-personal"}`. Not logged in is detected by the line `Open the following link to authenticate the ACP server:` on stdout/stderr or JSON-RPC error `-32000` → `ConfigError` naming the login command; `SUBSCRIPTION_REQUIRED` → `ConfigError` "no eligible subscription" | The point is the subscription; Tradewind never handles Google credentials. Detection strings from T3 Code (`antigravityAuthSupport.ts:26-27,166-171`, `AntigravityAuth.ts:124-126`). |
| Isolation (revised after T3 Code) | Spawn `agy acp --gemini-dir <tradewind profile dir> --app-data-dir antigravity-acp --disable-telemetry` with a **clean environment** (not inherited): `GEMINI_HOME=<profile dir>`, `AGY_ACP_FORCE_FILE_STORAGE=1`, `BROWSER` set to a no-op, a private `TMPDIR` removed after exit, plus only `PATH`/`HOME`/locale; scrub `GEMINI_API_KEY`, `GOOGLE_*`, `AGY_ACP_*`. Profile dir mode 0700. Login is one-time per profile dir: `agy acp --login --gemini-dir <profile dir> --app-data-dir antigravity-acp` (RUNBOOK) | `--app-data-dir` alone does not isolate: agy joins it under `--gemini-dir` even when absolute. The clean env stops API-key auth from taking over and keeps the token out of the shared macOS keychain. Spawn set adapted from T3 Code (MIT) `antigravityAuthSupport.ts:61-81,196-232,466-477`. |
| Client capabilities | `fs.readTextFile=false`, `fs.writeTextFile=false`, `terminal=false` | The agent uses its own file, shell and git tools inside `agy`'s sandbox; Tradewind does not become the agent's file system or terminal. |
| Built-in tools | Enabled, broker-gated (like Codex) | Meridian needs shell, git and file edits. Disabling them (as on Claude/Cursor) would defeat the purpose. |
| Permissions | Session mode `default` (set with `session/set_config_option` `mode`; the CLI rejects other permission modes); every `session/request_permission` → broker; allow → the `allow_once` option (never `allow_always`); deny → `reject_once`; broker error or timeout → reject. **If the needed option is missing, answer `{outcome: "cancelled"}`**, which ends the turn (fail closed). A request whose `toolCallId` starts with `interaction_` is a question to the user, not an approval: v1 answers `cancelled` and emits a notice, because Tradewind has no question channel | One-time options keep the broker in every decision. Option handling and the `interaction_` quirk from T3 Code (`adapter.ts:1414-1435,6905-6923`, `AntigravityProtocol.ts:48-53`). |
| Calls `agy` approves itself | The Tradewind profile dir (Isolation row) holds a `settings.json` with the strictest tool-permission level `agy` offers and no allow rules, and **no** `config/hooks.json` or non-empty `config/mcp_config.json` (hooks and configured MCP servers run outside the client's approval; T3 Code refuses such profiles, `AntigravityTextGeneration.ts:67-101`). The adapter refuses to start if either is present. RUNBOOK records exactly which calls still skip the client (go/no-go gate) | The operator's personal `agy` rules, hooks and MCP config must not widen what Tradewind's broker sees. T3 Code writes no permission rules and does not answer this question, so the gate's spike is still needed. |
| Caller tools and MCP servers | All exposed through the one `ToolHost` stdio shim in `session/new`/`session/resume` `mcpServers` (`{name, command, args, env: [{name, value}]}`). Shim calls are recognised in updates by `_meta.is_mcp_tool_call` and the tool name in the title (`Running <tool>` / `Run <tool>?`); a `session/request_permission` for a shim tool is auto-accepted (`allow_once`) because `ToolHost.call()` consults the broker itself (exactly the Codex rule) | One tool path for all backends; no double broker consult. Recognition from T3 Code (`runtimeModel.ts:1091-1095`, `AntigravityProtocol.ts:483-503`); T3 itself does not auto-accept its MCP tools, so Tradewind must do it explicitly. |
| System prompt | `_meta.antigravity.systemPrompt.append` on **every** `session/new` and `session/resume`, only if `initialize` advertised `agentCapabilities._meta.antigravity.systemPrompt.append` | `agy` keeps none of it between processes (T3 Code `AntigravityAcpSupport.ts:92-106`); with a process per turn it must be sent every turn. `supports_system_prompt=True`, documented as "appended to Antigravity's own prompt". |
| Model and effort | `ModelSpec.model` → `session/set_config_option` `model`, validated against that option's values (unknown → `ConfigError` "model unavailable for this Google account"); then `ModelSpec.effort` → the option whose `category == "thought_level"` (its id is `thinking_level`), validated **after** the model is set because levels differ per model; a level the model lacks → `Unsupported` | T3 Code `AntigravityAcpSupport.ts:217-298`. |
| Protocol version | Send `protocolVersion: 1`; accept a reply of 1 or 2 with the v1 shape | agy reports 2 with the v1 shape (T3 Code `client.ts:1109-1113`). Types are taken from the upstream ACP JSON schema (`schema-v1.21.0`), not from T3's generated TypeScript. |
| Structured output **(owner: way 1)** | See "Structured output" below | — |
| Tool-round cap | `supports_tool_round_cap=False` in v1 | Calls `agy` approves itself are invisible to the client, so Tradewind cannot count rounds exactly (FR-6.5 requires exact). Raise `Unsupported`, as Codex does. |
| Deny reason | `supports_deny_reason=False` | ACP's permission outcome carries only an option id; there is no reason channel. The reason is still recorded in `PermissionRequested` and the mirror. |
| Resume | `supports_native_resume=True` via **`session/resume`** with the stored native session id (same params as `session/new`, incl. `_meta`); updates carrying `_meta.isReplay == true` are dropped as a guard | `session/load` replays tool calls; `session/resume` does not (T3 Code `AntigravityAdapterV2.ts:237`, `runtimeModel.ts:1279-1282`). The spike confirms resume across a new process with the same profile dir on 1.5.3. |
| Transcript read | `False` in v1 | `session/load` replays history as updates; using it as a transcript source is possible later but not needed. |
| Fork | `False` | No ACP fork. Client-level fork-by-copy (store) still applies where the store is the record. |
| Turn retry | `False` | The loop is `agy`'s; pre-turn spawn retry only, as for other SDK backends. |
| Interrupt | `session/cancel` (a notification), then wait for the `session/prompt` response (stop reason `cancelled`); no response within 15 s → kill the process. The turn ends without a terminal event (port contract) | ACP native; timeout from T3 Code (`AcpSessionRuntime.ts:83,2412-2465`). |
| Usage | `session/prompt` result `usage` is this turn's only; Gemini-style, so input = `inputTokens + cachedReadTokens`, output = `outputTokens + thoughtTokens`; absent fields stay `None` | T3 Code `adapter.ts:303-317`. Quota windows are not in ACP; out of scope. |
| Process model (designer's call, after the builder check) | **One `agy acp` process per turn**, as Codex starts one `codex app-server` per turn: spawn, `initialize`, `authenticate`, `session/new` (first turn) or `session/resume` (later turns), prompt, then `session/close` if advertised, and terminate | `turn_runner` builds a new `ToolHost` (and its shim socket) for every turn and the port has no session-close hook, so a long-lived process would hold a dead shim socket and could never be closed. Per-turn also takes each turn's own tools and result tool. Measured on agy 1.5.3 (native binary): about 1.3–1.8 s from spawn to a ready session, no large unpack. (T3 Code's note of a ~1 GB unpack per launch is about an older PyInstaller bundle; T3 keeps one process per thread.) |
| Changes outside the adapter (designer's call, minimal) | (1) `BackendName` gains `"antigravity"`. (2) `turn_runner` gives the broker to `ToolHost` for `antigravity` exactly as it does for `codex`, because shim calls are auto-accepted at the ACP layer and `ToolHost.call()` must gate them. (3) `ToolHost` accepts one optional, per-turn result tool (name, schema, recorder) that `turn_runner` supplies when the backend asks for it and `output_schema` is set; this tool skips the broker, like Claude's `StructuredOutput`. Nothing else outside the adapter changes. | Without (2), every shim tool runs unchecked: the top anti-goal. (3) is the smallest seam that gets the result tool into the per-turn shim. |
| `refusal`, `max_turn_requests` stop reasons (designer's call, revised after T3 Code) | `TurnFailed`, no new `EndReason` member, no reminder. **`refusal` on Antigravity means quota exhausted** (agy maps only `QUOTA_EXHAUSTED` to it): message "Antigravity quota exhausted", with the reset time if the agent's text gives one. `max_turn_requests` → "agent turn limit reached" | The `EndReason` list is shared by all backends. Quota meaning from T3 Code `AntigravityTurnOutcome.ts:114-126`. Meridian treats a failed review as "not complete" and offers Retry. |
| Upstream errors hidden as `end_turn` | Older agy returned some API errors as a chat message plus `end_turn` (T3 Code reads agy's conversation database to detect them). agy 1.5.x's changelog says a failed ACP turn now reports its specific error. The spike checks 1.5.3; Tradewind does **not** read agy's private database. If errors are still hidden, a schema turn ends with `None` (reminder, then Retry), and a plain turn's `final_text` is the error text — documented as a known limit | Reading agy's internal SQLite is fragile and outside the protocol. |

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

1. Spawn this turn's `agy acp` process (Isolation row), `initialize`
   (protocol 1, client capabilities above), `authenticate`, then
   `session/new` (cwd, `mcpServers=[this turn's shim]`, `_meta.antigravity.
   systemPrompt.append`) on the first turn, or `session/resume` with the same
   params for the stored native id on later turns. Set `mode` to `default`,
   then model, then effort, with `session/set_config_option`.
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

## References

- T3 Code (MIT, © 2026 T3 Tools Inc.), commit `2579c3c4`, studied 2026-11-15:
  `apps/server/src/provider/antigravityAuthSupport.ts`,
  `packages/provider-acp/src/server/{adapter.ts,runtimeModel.ts,AcpSessionRuntime.ts}`,
  `…/AntigravityAcpSupport.ts`, `…/acp/AntigravityProtocol.ts`,
  `…/acp/AntigravityTurnOutcome.ts`, `docs/user/providers-antigravity.md`. Knowledge and
  protocol details are reused; any copied logic or fixture keeps the MIT notice.
- Reusable test fixture: `packages/effect-acp/test/fixtures/antigravity-initialize.json`
  (agy 1.1.1; regenerate for 1.5.3 in the spike).
- Upstream ACP schema `schema-v1.21.0` (agentclientprotocol).

## Left to the builder

- Exact ACP method and field names beyond those verified here (`initialize`,
  `session/new`, its `models`/`modes`/`configOptions`/`_meta` shape) — read
  the ACP v1 schema and confirm against `agy acp`.
- In the go/no-go spike, also confirm on 1.5.3: `session/resume` across a
  new process; whether `sessionCapabilities.close` is advertised; whether agy
  sends `session/request_permission` for stdio MCP (shim) tools and with which
  option kinds; whether API errors still come back as `end_turn`.
- Which `agy` setting gives the strictest tool permissions.
- How shim-tool calls appear in `tool_call` updates (so they are not
  double-reported) — mirror the Codex adapter's handling.
