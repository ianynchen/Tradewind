# Component: Antigravity Adapter (ACP)

Status: design. Approach approved by the owner (2026-11-15); revised after an
adversarial review with owner rulings on every critical and major finding
(2026-11-15). Not implemented.

## Purpose

The fifth `Backend` subclass (ARCHITECTURE §3.1): Google Antigravity, driven
through the official Agent Client Protocol server built into the Antigravity
CLI (`agy acp`, ACP over stdio). It lets a Tradewind caller run turns on the
owner's Antigravity subscription (Google AI Pro) and on every model that
subscription offers (Gemini 4, Gemini 3.8 Flash, Claude Opus/Sonnet 5.5,
GPT-OSS 120B at the time of writing).

### Why

The owner wants Meridian to use Antigravity "for as many areas in Meridian as
possible": create and review PRDs, FRDs and blueprints; create work orders;
create, update and close issues through an issue tracker's MCP server; fetch
code, submit, review, comment on, approve and merge pull requests; deploy.
Those are Meridian features (later pieces). This adapter gives Meridian the
four Tradewind capabilities they rest on, on Antigravity:

1. reliable structured output (reviews, documents, work orders);
2. caller tools and MCP servers in the session (issue management);
3. the agent's own shell, git and file tools, each risky call decided by the
   caller's `PermissionBroker`, or by the engine's own auto mode when the
   caller chooses that (fetch code, PRs, merge, deploy);
4. sessions that continue across turns.

### Scope rule (owner, 2026-11-15)

This backend runs on the owner's own machine, with the owner's login and
default environment. Multi-user deployments use the LangChain backend. The
test for every safety decision here is: **Tradewind must not make things
worse than the owner's own use of `agy` today.** Protections that only matter
in a multi-user setting (separate OS users, environment allowlists, separate
`agy` copies or logins) are out of scope.

## Goals

- A complete `Backend` for Antigravity with the four capabilities above.
- Structured output that is additive (FR-1.3), with the one documented
  Antigravity difference in "Structured output" step 6.
- In broker mode, every risky call that `agy` asks about reaches the broker
  in a stable, backend-neutral form, and a denial is a denial.
- Honest capability flags (R-1, FR-8).

## Non-goals

- LiteLLM (owner deferred, 2026-11-15).
- `agy` print mode and any print-mode/ACP hybrid (Approaches 2 and 3).
- The Meridian features themselves.
- Antigravity's API-key and Vertex auth methods, and the Antigravity Python
  SDK (API key and ADC only, no subscription).
- Passing `agy`'s questions to the caller (a later, separate design for all
  backends; see M5).
- One `agy` process for a whole session (T3 Code's model; see Process model).

## Anti-goals (a failure even if it works)

- In broker mode, a tool call that runs without the broker's approval when
  the broker would have denied it — above all `git push`, `gh pr merge`, a
  deploy command.
- A schema turn that silently returns prose as the structured result, or a
  result that does not validate against the schema.
- `--dangerously-skip-permissions`, ever. (`agy`'s Auto mode is allowed only
  as engine-auto, below.)
- Cutting off work that `agy` started (owner, M10).

## Delivery (owner, M12)

1. **Go/no-go spike** (below). Nothing else starts until it passes.
2. **PR 1 — shared parts, all backends:** engine-auto permission mode
   (Claude, Codex, Antigravity-ready), slash commands off (Claude, Codex), the
   backend-neutral broker vocabulary (`native`), the result-tool seam in
   `ToolHost`/`turn_runner` with a fake backend that exercises it as
   Antigravity will. One ADR; REQUIREMENTS updated in the same PR.
3. **PR 2 — the Antigravity adapter:** everything else in this document.
   Starts after PR 1 is merged (no stacked PRs).

## Approaches considered

| | Approach | Trade-off | Verdict |
|---|---|---|---|
| 1 | **ACP backend; structured output through a Tradewind result tool** | Native approvals, MCP servers per session, resume, cancel, models, system prompt. Schema enforced by Tradewind validation and one reminder; a turn can end with `None`. | **Chosen (owner)** |
| 2 | `agy` print mode (stream-json) | Engine-enforced schema, but no approval callback: approvals need a `PreToolUse` hook helper; if it fails, a merge may run unchecked. | Rejected |
| 3 | Both modes | Two protocols and two permission paths for one session. | Rejected |

## Constraints

- **Terms of service (owner's decision before production use).** A forum
  reply (not confirmed as Google staff) said driving `agy` with a personal
  Pro/Ultra login from another program breaks the terms. The binary contains
  "Your account is blocked due to a Terms of Service violation", so the risk
  is a blocked Google account. T3 Code's FAQ claims compliance
  (`docs/user/faq.md:52`); that is T3's claim, not Google's.
- ACP has no output-schema field; `agy acp` advertises only
  `_meta.antigravity.systemPrompt.append` (and `tier`). Verified on 1.5.3.
- GUIDELINES bind: no CI change, no version bump, match existing patterns,
  public-interface changes need an ADR, REQUIREMENTS change with the code.
- Live tests stay out of CI (env gate + `integration` marker), time-boxed,
  on the owner's quota.

## Go/no-go gate and version check (owner, M2)

**The check.** A scripted conformance suite, outside CI, run against the
installed `agy` with Tradewind's settings in broker mode. It plants traps
and observes them directly, because ACP does not report every action (e.g.
subagent calls look like ordinary calls):

- marker files for writes inside and outside the workspace;
- a local canary HTTP listener on loopback and a canary external URL;
- a decoy secret file outside the workspace (reads outside the workspace);
- input into an already-approved interactive shell (`send_command_input`);
- subagent steps, browser actions, background commands;
- a planted workspace hook, plugin and MCP server (must be detected, C1);
- a command that needs to leave the sandbox (must reach the client, M3);
- parallel turns on the same profile (locks, token refresh).

A path counts as **risky** if it writes outside the workspace, reaches the
network (loopback included), reads outside the workspace, or runs a
command, and it happens without a `session/request_permission` reaching
Tradewind. Reads under the system temp directory and `agy`'s own
customization directory are expected.

**First run:** if any risky path runs without asking, work stops and the
findings go to the owner. No adapter code is written before it passes.

**Every new `agy` version (owner: use the latest, no pin, no separate
copy):** Tradewind records the versions that passed in its data folder.
Before the first turn on an unrecorded version, it runs the check
automatically (minutes, a little quota). Pass → the version is recorded and
the turn runs. Fail → broker-mode turns on that version are refused with the
findings; engine-auto turns still run, because there `agy`'s own safety
decides. Google's server-side flags can change behaviour without a version
change; the owner can rerun the check at any time.

## Owns

- One `agy acp` process per turn and its process tree; a minimal ACP JSON-RPC
  client over stdio.
- Mapping `session/update` onto the frozen event taxonomy (ARCHITECTURE
  §3.2) and the mirror.
- The approval bridge (`session/request_permission` → broker) and the
  mapping into the shared broker vocabulary.
- The workspace-customization check, the version check, the result tool's
  use and its reminder, and its capability declaration.

## Depends on

PR 1's shared parts; the `Backend` port; `ToolHost` and its stdio shim; the
broker; the latest `agy` on `PATH`, logged in.

## Decisions

Owner decisions are marked **(owner, ID)**; the rest are the designer's
calls. The builder may revisit a call only by raising it.

### Process, auth and isolation

| Decision | Choice | Why |
|---|---|---|
| Process model | One `agy acp` process per turn, as Codex does: spawn, `initialize`, `authenticate`, `session/new` (first turn) or `session/resume`, prompt, close, terminate | `turn_runner` builds a new `ToolHost` and shim socket per turn, and the port has no session-close hook. Measured on 1.5.3: 1.3–1.8 s to a ready session, no large unpack. |
| Turn end **(owner, M10)** | The turn ends only when `agy` reports the root agent idle **and** no background task, subagent or swarm member running (ACP work-state/plan/tool updates; spike confirms the signal). Then `session/close` (if advertised) and terminate | Never cut off work. A turn can last as long as its longest background task. |
| Interrupt **(owner, M10)** | `session/cancel`, wait for the prompt response (stop reason `cancelled`); no response in 15 s → stop the whole process tree: `agy` is spawned in its own process group, then the tree is walked (T3 Code's method) and every descendant is signalled; survivors are reported by name and PID in a notice | A group kill misses processes that open their own terminal session; no method is complete on macOS, so the honest best is stop-and-report. |
| Auth | `SubscriptionAuth` only; `ApiKeyAuth` → `ConfigError`. After `initialize`, `authenticate {methodId: "oauth-personal"}`. Not logged in (stdout/stderr line `Open the following link to authenticate the ACP server:` or JSON-RPC `-32000`) → `ConfigError` naming the login command; `SUBSCRIPTION_REQUIRED` → `ConfigError` "no eligible subscription". Failures before `TurnStarted` raise, as on other backends | Strings from T3 Code (`antigravityAuthSupport.ts:26-27,166-171`, `AntigravityAuth.ts:124-126`). |
| Settings isolation **(owner, M1)** | Spawn with `--gemini-dir <Tradewind profile dir>` and `--disable-telemetry`, so Tradewind's `settings.json` (strictest tool permissions, no allow rules, AI credits off, sandbox on, `--include-default-customizations=false`) is separate from the owner's interactive `agy`. The profile dir comes from config (new `NativeStoreConfig.antigravity_dir`; FR-10.4: Tradewind reads no files or env by itself) | The owner's personal allow rules must not widen what the broker sees. |
| Login and token **(owner, M1)** | The spike finds where the token lives (keychain or file) and whether profiles are separate. If they cannot be separated, Tradewind shares the owner's login (accepted fallback). T3's `GEMINI_HOME` / `AGY_ACP_FORCE_FILE_STORAGE` are **not** used: they do not exist in 1.5.3 | A shared login adds no security risk (the token is already in the keychain); side effects are a shared quota and one account. |
| Environment **(owner, M4)** | Inherit the owner's environment. Remove only variables that switch `agy` away from subscription auth (at least `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`; the spike lists the full set) | Scope rule: no more risk than the owner's terminal; tools need `SSH_AUTH_SOCK`, `GH_TOKEN`, deploy credentials, proxies. |
| AI credits | Off in Tradewind's settings | Unattended turns must not spend paid credits when the quota runs out. |
| Parallel turns | Allowed; the spike runs several in parallel on one profile (database locks, token refresh). If they conflict, turns on one profile are serialised | — |

### Workspace and prompt

| Decision | Choice | Why |
|---|---|---|
| Workspace customizations **(owner, C1)** | Before spawn, look in the cwd and the repo root for `.agents/`, `.gemini/`, `.agent/` content other than empty skill folders (hooks, plugins, skills, MCP config). If found, call the caller's `on_workspace_customizations(files) -> "stop" \| "set_aside" \| "proceed"`. No callback, or a callback error → `stop` (`ConfigError` listing the files). `set_aside` moves the files to a backup folder outside the workspace and restores them after the turn (also after a failure or interrupt). `proceed` emits a notice "Antigravity customizations loaded without broker control: <files>" | Hooks and plugins run without any approval request. Moving files aside, not deleting, means the agent cannot commit a deletion and the repo is not damaged. |
| Empty skill folders | `agy` creates empty `.gemini/skills` and `.agents/skills` in the cwd; they are ignored by the check and left in place | They hold nothing that runs. |
| Slash commands **(owner, M11)** | Off: a prompt is always a message to the model. Use `agy`'s setting if ACP honours it; otherwise prefix a prompt that starts with `/`. (Claude and Codex in PR 1.) | A prompt from a PR description must not run `/logout` or `/clear`. |
| Skills | Not separately blocked. Prompt-triggered skills are off (slash commands); workspace skills fall under C1; default customizations are off; a skill's actions are tool calls and are decided like any other | A skill gives the model instructions, not permissions. |

### Permissions

| Decision | Choice | Why |
|---|---|---|
| Permission modes **(owner, C2/M9)** | **Broker mode** (a broker is set): session mode `default`, every approval request → broker. **Engine-auto** (`permission_mode="engine_auto"`, opt-in per session; PR 1 for all three backends): `agy` Auto mode with the sandbox; Claude's auto mode; Codex's auto-reviewer with the workspace-write sandbox. **No broker on Antigravity → engine-auto** (owner, M9: the caller accepts the risk; documented). Antigravity's Auto over ACP needs `agy` ≥ 1.5.4; without it, engine-auto raises `ConfigError` ("update agy or set a broker"), never allow-all | The owner works in auto mode by habit; the broker stays available for merge/deploy. Engine auto modes differ in strength (README table): Codex and `agy` auto-run anything inside their sandbox. |
| Sandbox **(owner, M3)** | On in both modes. In broker mode, a command that must leave the sandbox reaches the broker as a request; the spike confirms such requests reach the client | Contains what an approved script does; the risky steps (push, merge, deploy) show up as explicit escape requests. |
| Answering a request | Broker allow → `allow_once`. Broker deny → `reject_once`; the turn continues. Broker error or timeout → deny | One decision per call. |
| No `allow_once` offered **(owner)** | Broker allow → the narrowest allow option that is scoped to this conversation; **never** an option that persists to settings or switches to Auto mode; emit a notice naming the grant (e.g. "agy granted 'commands starting with git' for this conversation"). Broker deny → `reject_once`. Neither a matching allow nor a reject offered → cancel (→ `TurnFailed`, M5). Log every request's full option list | Matches what the owner would click in `agy`; nothing reaches beyond the conversation. |
| Mode changes **(owner, M7)** | Watch `current_mode_update` and config updates. Broker mode: a change away from `default` stops the turn with `TurnFailed("Antigravity changed the session mode from default to <m>; stopped to keep the broker in control")`. Engine-auto: logged only | The broker must not lose control silently. |
| Adapter-started cancels **(owner, M5)** | The adapter records its own cancels and ends the turn with `TurnFailed` and the real reason: "Antigravity asked a question: '<title>' (questions are not supported yet)" for `interaction_*` requests, "no allow/reject option for <request>" otherwise. Settings ask the agent to ask as few questions as `agy` allows | A caller-less cancel was otherwise recorded as "turn ended without emitting a terminal event". |
| Broker vocabulary **(owner, M8)** | Requests are mapped (PR 1 defines the shared kinds): `shell {command, cwd}`; `apply_patch {changes: [{path, kind}]}` with `agy`'s raw content under `native_args`; `fetch_url {url}`; `subagent {task}`; `read_outside_workspace {path}`; everything without a shared meaning (browser, `send_command_input`, web search, new tools) → `native {backend: "antigravity", tool, args, summary, unrecognised}`. A known kind whose critical field (command, path, url) cannot be read → **deny** with "unrecognised Antigravity request". Meridian's broker default for `native` and `unrecognised` is "ask me" | One rule ("no push to main") holds on every backend; an `agy` update can make Tradewind stricter, never silently looser. |
| FR-4.4 terminate | A broker `terminate` → reject the call, then interrupt; `end_reason="broker_terminated"` (Codex's pattern) | Consistency. |

### Tools

| Decision | Choice | Why |
|---|---|---|
| Built-in tools | Enabled; decided per the permission mode | Meridian needs shell, git and file edits. |
| Client capabilities | `fs.readTextFile=false`, `fs.writeTextFile=false`, `terminal=false` | The agent uses its own tools; Tradewind is not its file system or terminal. |
| Caller tools and MCP servers | All through the one `ToolHost` stdio shim in `session/new`/`session/resume` `mcpServers` (`{name, command, args, env: [{name, value}]}`); `ToolHost` gets the broker on this backend, as on Codex | One tool path; `ToolHost.call()` is the gate for shim calls. |
| Shim identification **(owner, C2)** | The shim gets a new random name every turn (`tradewind-<8 hex>`). A `session/request_permission` is auto-accepted only if its `toolCallId` was first announced in a `tool_call` with `_meta.is_mcp_tool_call == true` **and** the shim's server name; everything else goes to the broker. A caller tool whose name equals an `agy` built-in tool name → `ConfigError` (list from the spike). Title matching is never used for approval. If `agy` stops sending these fields, shim calls fall back to the broker (asked twice), never to auto-accept | The broker is asked exactly once per call and nothing is approved on a guess. |
| Shim denials | Emit `PermissionRequested` for a shim-path denial using Codex's `_denied_shim_permission_event` pattern | FR-4.1. |
| Abandoned shim calls | When `agy` gives up on a shim call (its MCP timeout, or the peer disconnects), `ToolHost` cancels the pending call, including a pending broker question | An "ask" broker answered late must not run a merge that the model will retry. (Small `ToolHost` change, PR 1.) |

### Configuration and protocol

| Decision | Choice | Why |
|---|---|---|
| Protocol version | Send `protocolVersion: 1`; accept 1 or 2 with the v1 shape | agy reports 2 with the v1 shape (T3 Code `client.ts:1109-1113`). Types from the upstream ACP schema (`schema-v1.21.0`). |
| ACP client | A small in-repo JSON-RPC client | ACP is small; no new dependency. A maintained official Python ACP library needs owner approval. |
| System prompt | `_meta.antigravity.systemPrompt.append` on every `session/new` and `session/resume`, if advertised | `agy` keeps none of it between processes. `supports_system_prompt=True` ("appended"). |
| Model and effort | Model via `session/set_config_option` `model`, validated against its values (unknown → `ConfigError`); then effort via the option with `category == "thought_level"`, validated after the model is set; an unknown level → `Unsupported` | T3 Code `AntigravityAcpSupport.ts:217-298`. |
| Resume | `session/resume` with the stored native id; drop updates with `_meta.isReplay == true` | `session/load` replays tool calls. |
| Stop reasons | `end_turn` → see Structured output; `cancelled` caused by the caller → interrupted (no terminal event); `cancelled` caused by the adapter → `TurnFailed` (M5); `max_tokens` → `TurnCompleted(end_reason="max_tokens")`; `refusal` (on Antigravity: quota exhausted) → `TurnFailed("Antigravity quota exhausted", reset time if given)`; `max_turn_requests` → `TurnFailed("agent turn limit reached")`; JSON-RPC error → `TurnFailed` with the message | `EndReason` is shared; no new member. |
| Hidden API errors | The spike checks whether 1.5.x still returns API errors as text plus `end_turn`. Tradewind does not read `agy`'s private database. If they are still hidden, it is documented as a known limit | — |
| Usage | Turn usage from the `session/prompt` result: input = `inputTokens + cachedReadTokens`, output = `outputTokens + thoughtTokens` | T3 Code `adapter.ts:303-317`. Quota windows out of scope. |
| Tool-round cap | `supports_tool_round_cap=False` | Calls `agy` approves itself are invisible, so an exact cap is impossible. |

### Changes outside the adapter (owner, M12)

PR 1 (one ADR, REQUIREMENTS in the same PR): `permission_mode` /
engine-auto on Claude, Codex and the port (with a capability flag); slash
commands off on Claude and Codex (Codex: verify whether the app server
expands them; test either way); the shared broker vocabulary including
`native`; the result-tool seam — a backend declares it needs the result tool,
`turn_runner` passes the effective `output_schema` to `ToolHost`, which
serves `tradewind_submit_result`, skips the broker for it, refuses a caller
tool with that name, and exposes the recorded result; abandoned-call
cancellation in `ToolHost`; a fake backend that exercises the seam.

PR 2: `BackendName` gains `"antigravity"` and the backend registry entry;
`NativeStoreConfig.antigravity_dir`; the `on_workspace_customizations`
callback in `SessionOptions`; the verified-version record; `turn_runner`
passes the broker to `ToolHost` for `antigravity` as for `codex`.

REQUIREMENTS to update (in the PR that changes the behaviour): glossary
("four backends"), FR-1.3 (the Antigravity reminder exchange), FR-3.1,
FR-4.1, FR-6.3/6.4 (store isolation mandatory here; no transcript
reconcile), FR-8.2, NFR-4 (latest `agy` plus the automatic check, not an
exact pin).

## Structured output (owner: way 1, M6)

When `ctx.output_schema` is set:

1. `ToolHost` serves `tradewind_submit_result`, whose input schema **is** the
   caller's schema.
2. The turn's prompt gets a trailing instruction block: "When you have
   finished, call `tradewind_submit_result` exactly once with your final
   answer." The mirror keeps only the caller's prompt.
3. Each call is validated with `jsonschema` (pending owner approval of the
   dependency; it is already in `uv.lock` through `mcp`). Valid → recorded;
   the tool answers "Result recorded." Invalid → the tool returns the
   validation errors. **The last valid result wins.**
4. `tradewind_submit_result` tool calls are hidden from events and the
   mirror, as Claude's `StructuredOutput` is.
5. Budget: at most 3 result calls per turn (a 4th gets "No more attempts."
   and is not recorded) and at most one reminder. If the agent ends the turn
   without a valid result and has made fewer than 3 calls, Tradewind sends
   the reminder: "Call `tradewind_submit_result` now with your final
   answer."
6. **Everything the agent does after the reminder stays visible** — text,
   thinking and tool calls (owner, M6). The reminder itself is recorded as a
   notice ("Tradewind reminded the agent to submit its result"), not as a
   user message. **`final_text` is the text of the main answer**, the one
   before the reminder. FR-1.3 states this Antigravity difference.
7. Still no valid result → the turn completes, `end_reason="end_turn"`,
   `structured_output=None`; Meridian offers Retry.
8. A result recorded before the turn ends early (interrupt, `max_tokens`,
   failure) is dropped.
9. Without a schema none of this happens. On non-schema turns the system
   prompt append says that `tradewind_submit_result` is not available, so the
   model does not reach for it after an earlier schema turn.

Follow-up (separate, small): make Claude's `final_text` rule the same when
its CLI reminder fires.

## Capabilities (FR-8)

| Flag | Value |
|---|---|
| `supports_system_prompt` | True (appended) |
| `supports_structured_output` | True (result tool; may complete with `None`) |
| `supports_interactive_permissions` | True |
| `supports_in_process_tools` | False (MCP shim, as on Codex; NFR-3) |
| `supports_native_resume` | True |
| `supports_fork` | False |
| `supports_transcript_read` | False |
| `supports_tool_round_cap` | False |
| `supports_turn_retry` | False |
| `supports_deny_reason` | False (ACP outcomes carry only an option id) |
| engine-auto (PR 1 flag) | True with `agy` ≥ 1.5.4 |

## Turn algorithm

1. Check the `agy` version against the verified record (run the check if
   new). Check workspace customizations (C1). Check the permission mode
   (broker, engine-auto, or no broker → engine-auto).
2. Spawn `agy acp` in its own process group (Settings isolation, Environment
   rows), `initialize`, `authenticate`, `session/new` or `session/resume`
   with this turn's shim and the system prompt append. Set `mode`
   (`default` or Auto), then model, then effort.
3. Emit `TurnStarted`. Send `session/prompt` (plus the schema block if set).
4. Map `session/update`s; hide `tradewind_submit_result`; watch the mode (M7).
5. Answer `session/request_permission`s (Permissions and Tools tables).
6. On the prompt result, apply the Stop reasons row and Structured output.
7. Wait for idle (Turn end row), close the session, stop the process tree,
   restore any set-aside workspace files.

## Acceptance

- The go/no-go suite (scripted, outside CI) passes on the installed `agy`.
- Unit tests against a fake ACP agent (scripted stdio peer, no network):
  event mapping; every Permissions and Tools row (incl. missing `allow_once`,
  persist/Auto options never chosen, mode change in each mode, adapter cancel
  reasons, shim identification by call id and server name, colliding tool
  names, unreadable critical fields → deny, `native` mapping); structured
  output (valid first call; invalid then valid; no call → reminder → valid;
  3 invalid → no reminder → `None`; last valid wins; early end drops the
  result; reminder content visible; `final_text` = main answer);
  workspace-customization outcomes incl. restore after interrupt;
  slash-prefixed prompt; resume; interrupt with a surviving child process;
  turn waits for a background task; engine-auto and no-broker paths;
  version record; capability flags.
- Live conformance (`tests/conformance/test_antigravity.py`, env gate
  `TRADEWIND_RUN_ANTIGRAVITY_INTEGRATION`, `integration` marker, 3-minute
  time box, cheapest model), run once by the operator: plain turn; structured
  output; a denied shell command never runs (marker file); a caller tool
  through the shim; resume continues context; engine-auto turn.
- RUNBOOK: login, the profile dir, what the check covers and how to rerun it,
  engine-auto strength per engine, the ToS note. ARCHITECTURE §3.1, README
  capability matrix, CHANGELOG.

## References

- T3 Code (MIT, © 2026 T3 Tools Inc.), commit `2579c3c4`, studied
  2026-11-15: `apps/server/src/provider/antigravityAuthSupport.ts`,
  `packages/provider-acp/src/server/{adapter.ts,runtimeModel.ts,AcpSessionRuntime.ts}`,
  `…/AntigravityAcpSupport.ts`, `…/acp/AntigravityProtocol.ts`,
  `…/acp/AntigravityTurnOutcome.ts`, `docs/user/providers-antigravity.md`.
  Copied logic or fixtures keep the MIT notice.
- Fixture: `packages/effect-acp/test/fixtures/antigravity-initialize.json`
  (agy 1.1.1; regenerate in the spike).
- Upstream ACP schema `schema-v1.21.0`.

## Left to the builder

- Exact ACP fields beyond those verified here; confirm against `agy acp`.
- In the spike: the idle/work-state signal for Turn end; whether
  `sessionCapabilities.close` is advertised; whether `agy` sends approval
  requests for stdio MCP (shim) tools and with which options; whether ACP
  honours a slash-command off switch; the full auth-switching variable list;
  the `agy` built-in tool names; the settings names for strict permissions,
  sandbox, AI credits and minimal questions; `agy`'s MCP call timeout.
