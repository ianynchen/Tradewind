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
   `jsonschema` is approved as a declared dependency (owner, 2026-11-15).
2. **PR 1 — shared parts, all backends:** engine-auto permission mode
   (Claude, Codex, Antigravity-ready); slash commands off (Claude, Codex);
   the backend-neutral broker vocabulary (`native`); the result-tool seam in
   `ToolHost`/`turn_runner`; the `prepare_turn` port hook and the runner's
   stop checks around it; `ctx.timeout_reason` and the runner's timeout
   notice; the runner's broker wrapper for pending-approval activity;
   cancellation of
   abandoned shim calls in `ToolHost`; the inactivity watchdog and its
   activity channel, wired for Claude, Codex and LangChain; `jsonschema`
   declared; a fake backend that exercises the seams as Antigravity will. One
   ADR; REQUIREMENTS (incl. FR-6.6) updated in the same PR.
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
and observes them directly, because ACP does not report every action. The check has its own
time limit (default 15 minutes); hitting it records the outcome **"timed out"**
for that version, which is not a fail: the waiting broker-mode turns are
refused before `TurnStarted` with `ConfigError("Antigravity safety check
timed out for agy <version>; retry later")`, and the next broker-mode turn runs the check again,
at most once per hour per version (backoff), so a slow check cannot run on
every turn. (A single trap that stays inconclusive after its retries still
makes the check fail.) The check runs in its own throwaway workspace. A `stop()` while a turn waits in `prepare_turn` for the check cancels
that wait: the Antigravity backend counts a turn as in flight
from the start of `prepare_turn`, so `interrupt()` reaches it;
`prepare_turn` returns, and the runner sees the stop request and records
the turn as interrupted without calling `run()`. The check runs in its own
task, so it completes and records its result even when the waiting turn
stops (it does stop if the whole process exits). No new error class is
added. Traps work (e.g.
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

**Evidence per trap.** A trap passes only with positive evidence that it was
attempted: either a `session/request_permission` for it reached Tradewind,
or the trap fired (marker present, canary hit). No evidence → the trap is
**inconclusive**; it is retried up to twice, then the check fails. The check
answers every approval request with **allow** (so that an action which
would have run is observable) and records each request; it runs in a
throwaway workspace under the system temp directory; the "external" canary
is a second local listener reached through a non-loopback interface
address, so no outside host is needed. Workspace hooks, plugins and MCP
servers are tested by the C1 detector (planted files must be detected); the
check does not run them.

**Where it lives.** A module in `src/` (`tradewind.adapters.antigravity_check`),
because it runs automatically; its own unit tests use the fake ACP agent.

**First run:** if any risky path runs without asking, work stops and the
findings go to the owner. No adapter code is written before it passes.

**Every new `agy` version (owner: use the latest, no pin, no separate
copy):** Tradewind records **pass and fail, with the findings**, per version
in its data folder. Before the first **broker-mode** turn on an unrecorded
version, it runs the check automatically (minutes, a little quota), under a
lock so only one check runs at a time; other turns wait for its result. The
check runs before the turn's watchdog starts, so it does not use the turn's
time. Pass → the turn runs. Fail → broker-mode turns on that version are
refused with the recorded findings, without rerunning the check;
engine-auto turns are not checked, because there `agy`'s own safety
decides. The owner reruns the check on demand (e.g. after Google changes
server-side flags), and each run also refreshes the list of `agy` built-in
tool names (C2).

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
| Turn end **(owner, M10)** | The turn waits until `agy` reports the root agent idle **and** no background task, subagent or swarm member running (ACP work-state/plan/tool updates; the spike confirms the signal). **The terminal event (`TurnCompleted`/`TurnFailed`) is emitted only after that idle point**, so the caller cannot start the next turn while work runs; output from background work during the wait is part of the turn's events. Then `session/close` (if advertised) and terminate `agy` itself. **Descendant processes still alive at a normal end are not killed** (e.g. a `nohup deploy.sh &` that `agy` does not track); they are reported (Process report row) | Never cut off work. A turn can last as long as its longest background task. |
| Interrupt **(owner, M10)** | `session/cancel`, wait for the prompt response (stop reason `cancelled`). If it answers within 15 s, `agy` is terminated and live descendants are reported, **not** killed (same rule as a normal end; the caller asked to stop the agent, not to destroy its work). No response in 15 s → stop the whole process tree: `agy` is spawned in its own process group, then the tree is walked (T3 Code's method) and every descendant is signalled; survivors are reported. An interrupt during the idle wait ends the turn as interrupted and drops any recorded structured result | A group kill misses processes that open their own terminal session; no method is complete on macOS, so the honest best is stop-and-report. |
| Watchdog **(owner, C-A + inactivity ruling)** | Three limits (see "Inactivity watchdog" below): a **model-silence** limit, a **tool-silence** limit, and an optional **hard** limit set by the caller per turn. When any fires: interrupt as above, `end_reason="timeout"`, and a process report naming which limit fired. The version check runs before the watchdog starts | Inactivity is the best signal of a stuck agent; long healthy work keeps running. |
| Process report **(owner, C-A)** | On a watchdog timeout, an interrupt, or descendants alive at a normal end: one WARNING log entry **and** a mirrored `ItemCompleted` with `kind="event"` (the FR-6.6 retry-notice form), emitted **before** the terminal event. It contains turn id, session id, backend, which limit fired and its value and the elapsed time (timeout), each live process (name, PID, full command line, start time), `agy`'s last reported work state, and the last tool call | Debugging: the same record for all three cases, in logs and history; the terminal event stays last (`ports.py:386-388`). |
| Auth | `SubscriptionAuth` only; `ApiKeyAuth` → `ConfigError`. After `initialize`, `authenticate {methodId: "oauth-personal"}`. Not logged in (stdout/stderr line `Open the following link to authenticate the ACP server:` or JSON-RPC `-32000`) → `ConfigError` naming the login command; `SUBSCRIPTION_REQUIRED` → `ConfigError` "no eligible subscription". Failures before `TurnStarted` raise, as on other backends | Strings from T3 Code (`antigravityAuthSupport.ts:26-27,166-171`, `AntigravityAuth.ts:124-126`). |
| Settings isolation **(owner, M1)** | Spawn with `--gemini-dir <Tradewind profile dir>`, `--disable-telemetry` and `--include-default-customizations=false`, so Tradewind's `settings.json` (strictest tool permissions, no allow rules, AI credits off, sandbox on) is separate from the owner's interactive `agy`. The profile dir comes from config (new `NativeStoreConfig.antigravity_dir`; FR-10.4: Tradewind reads no files or env by itself) | The owner's personal allow rules must not widen what the broker sees. |
| Login and token **(owner, M1)** | The spike finds where the token lives (keychain or file) and whether profiles are separate. If they cannot be separated, Tradewind shares the owner's login (accepted fallback). T3's `GEMINI_HOME` / `AGY_ACP_FORCE_FILE_STORAGE` are **not** used: they do not exist in 1.5.3 | A shared login adds no security risk (the token is already in the keychain); side effects are a shared quota and one account. |
| Environment **(owner, M4)** | Inherit the owner's environment. Remove only variables that switch `agy` away from subscription auth (at least `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`; the spike lists the full set) | Scope rule: no more risk than the owner's terminal; tools need `SSH_AUTH_SOCK`, `GH_TOKEN`, deploy credentials, proxies. |
| AI credits | Off in Tradewind's settings | Unattended turns must not spend paid credits when the quota runs out. |
| Parallel turns | Allowed. The spike runs several in parallel on one profile (database locks, token refresh); if they conflict, turns on one profile are serialised | — |

### Workspace and prompt

| Decision | Choice | Why |
|---|---|---|
| Workspace customizations **(owner, C1)** | Before spawn, look in the cwd, the repo root and every folder between them for `.agents/`, `.gemini/`, `.agent/` content other than empty skill folders (hooks, plugins, skills, MCP config); the spike confirms `agy` loads from nowhere else. If found, call the caller's `on_workspace_customizations(files) -> "stop" \| "delete" \| "proceed"`. No callback, or a callback error → `stop` (`ConfigError` listing the files). `delete`: copy the files to a timestamped backup folder in Tradewind's data folder, then delete them from the workspace; `run()` emits a notice after `TurnStarted` naming the deleted files and the backup path. Tradewind never restores them; restoring is the user's responsibility (owner). If the files are tracked by git, the deletion is a normal working-tree change that a later commit may include; that is also the user's responsibility. `proceed`: `run()` emits a notice after `TurnStarted`: "Antigravity customizations loaded without broker control: <files>" | Hooks and plugins run without any approval request. The owner's ruling, literally: stop, delete, or proceed. |
| Empty skill folders | `agy` creates empty `.gemini/skills` and `.agents/skills` in the cwd; they are ignored by the check and left in place | They hold nothing that runs. |
| Slash commands **(owner, M11)** | Off: a prompt is always a message to the model. Use the engine's own off switch where it exists; otherwise a prompt whose first non-whitespace character is `/` is sent with the plain-text prefix `Message: ` (so `/clear the old tests` reaches the model as `Message: /clear the old tests`); the mirror keeps the caller's original prompt. The spike and PR 1 confirm per engine that the prefixed form is not expanded. (Claude and Codex in PR 1, same rule.) | A prompt from a PR description must not run `/logout` or `/clear`. |
| Skills | Not separately blocked. Prompt-triggered skills are off (slash commands); workspace skills fall under C1; default customizations are off; a skill's actions are tool calls and are decided like any other | A skill gives the model instructions, not permissions. |

### Permissions

| Decision | Choice | Why |
|---|---|---|
| Permission modes **(owner, C2/M9)** | **Broker mode** (a broker is set, `permission_mode` not `engine_auto`): session mode `default`, every approval request → broker. **Engine-auto** (`permission_mode="engine_auto"`, opt-in per session; PR 1 for all three backends): `agy` Auto mode with the sandbox; Claude's auto mode; Codex's auto-reviewer with the workspace-write sandbox (PR 1 discovers the exact Claude and Codex settings). **No broker on Antigravity → engine-auto** (owner, M9; documented as the caller's accepted risk). "No broker" means the runner's default allow-all broker, detected with `is_default_allow_all` as Codex does (`turn_runner.py:88-102`); Codex's own no-broker behaviour (read-only sandbox) does not change. Antigravity's Auto over ACP is expected in `agy` ≥ 1.5.4 (the spike confirms it); where Auto is not offered, engine-auto raises `ConfigError` ("update agy or set a broker") in `run()` before `TurnStarted`, never allow-all; the capability flag is True and the check is at run time (`capabilities()` takes no context) | The owner works in auto mode by habit; the broker stays available for merge/deploy. Engine auto modes differ in strength (README table): Codex and `agy` auto-run anything inside their sandbox. |
| Who decides, per mode | See the table "Decisions per mode" below | — |
| Sandbox **(owner, M3)** | On in both modes. In broker mode, a command that must leave the sandbox reaches the broker as a request; the spike confirms such requests reach the client | Contains what an approved script does; the risky steps (push, merge, deploy) show up as explicit escape requests. |
| Answering a request | Broker allow → `allow_once`. Broker deny → `reject_once`; the turn continues. Broker error or timeout → deny | One decision per call. |
| No `allow_once` offered **(owner)** | Broker allow → the narrowest allow option that is scoped to this conversation; **never** an option that persists to settings or switches to Auto mode; emit a notice naming the grant (e.g. "agy granted 'commands starting with git' for this conversation"). Broker deny → `reject_once`; if only a `reject_always` is offered, use it only if it is scoped to this conversation, else cancel. Neither a usable allow nor a usable reject → cancel (→ `TurnFailed`, M5). Log every request's full option list | Matches what the owner would click in `agy`; nothing reaches beyond the conversation. |
| Mode changes **(owner, M7)** | Watch `current_mode_update` and config updates. Broker mode: a change away from `default` stops the turn with `TurnFailed("Antigravity changed the session mode from default to <m>; stopped to keep the broker in control")`. Engine-auto: logged only | The broker must not lose control silently. |
| Adapter-started cancels **(owner, M5)** | The adapter records its own cancels and ends the turn with `TurnFailed` and the real reason: "Antigravity asked a question: '<title>' (questions are not supported yet)" for `interaction_*` requests, "no allow/reject option for <request>" otherwise. Settings ask the agent to ask as few questions as `agy` allows | A caller-less cancel was otherwise recorded as "turn ended without emitting a terminal event". |
| Broker vocabulary **(owner, M8)** | Requests are mapped (PR 1 defines the shared kinds): `shell {command, cwd}`; `apply_patch {changes: [{path, kind}]}` with `agy`'s raw content under `native_args`; `fetch_url {url}`; `subagent {task}`; `read_outside_workspace {path}`; everything without a shared meaning (browser, `send_command_input`, web search, new tools) → `native {backend: "antigravity", tool, args, summary, unrecognised}`. A known kind whose critical field (command, path, url) cannot be read → **deny** with "unrecognised Antigravity request". Meridian's broker default for `native` and `unrecognised` is "ask me" | One rule ("no push to main") holds on every backend; an `agy` update can make Tradewind stricter, never silently looser. |
| FR-4.4 terminate | On the `request_permission` path, a broker `terminate` → reject the call, then interrupt; `end_reason="broker_terminated"` (Codex's pattern). On the shim path, terminate is not deliverable (`tool_host.py:443-446`), exactly as on Codex; documented | Consistency. |

### Decisions per mode

Who decides each kind of request. "Broker" means the caller's broker;
"engine" means the engine's own auto review.

| Request | Broker mode | Engine-auto, broker set | Engine-auto, no broker |
|---|---|---|---|
| `agy` approval request (`session/request_permission`) for a built-in tool | Broker | Engine (Auto mode does not ask; if it still asks, the broker answers) | Engine (if it still asks: allow once) |
| Sandbox escape request | Broker | Broker (`agy` asks the client) | Allow once (owner M9: caller accepts the risk) |
| Shim tool (caller tools, caller MCP servers) | Broker, inside `ToolHost` | Broker, inside `ToolHost` | Allowed (default allow-all broker) |
| `tradewind_submit_result` | Never asked | Never asked | Never asked |

Claude and Codex in engine-auto: the engine decides its own tools; caller
tools still go through Tradewind's broker path as today. PR 1 discovers the
exact settings: Claude's permission mode for auto and whether `can_use_tool`
still runs for anything; Codex's auto-reviewer configuration
(`approvals_reviewer` and the sandbox mode).

### Tools

| Decision | Choice | Why |
|---|---|---|
| Built-in tools | Enabled; decided per the permission mode | Meridian needs shell, git and file edits. |
| Client capabilities | `fs.readTextFile=false`, `fs.writeTextFile=false`, `terminal=false` | The agent uses its own tools; Tradewind is not its file system or terminal. |
| Caller tools and MCP servers | All through the one `ToolHost` stdio shim in `session/new`/`session/resume` `mcpServers` (`{name, command, args, env: [{name, value}]}`); `ToolHost` gets the broker on this backend, as on Codex | One tool path; `ToolHost.call()` is the gate for shim calls. |
| Shim identification **(owner, C2)** | The shim gets a new random name every turn (`tradewind-<8 hex>`). A `session/request_permission` is auto-accepted only if its `toolCallId` was first announced in a `tool_call` with `_meta.is_mcp_tool_call == true` **and** the shim's server name; everything else goes to the broker. A caller tool whose name equals an `agy` built-in tool name → `ConfigError`. The name list is recorded by the version check; engine-auto turns on an unchecked version use the most recently recorded list, plus a list shipped with Tradewind from the spike. Title matching is never used for approval. Accepted exception: if `agy` stops sending these fields, shim calls fall back to the broker and are asked twice, never auto-accepted | The broker is asked exactly once per call in the normal case, and nothing is approved on a guess. |
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
| Stop reasons | `end_turn` → see Structured output; `cancelled` caused by the caller → interrupted (no terminal event); `cancelled` caused by the adapter → `TurnFailed` (M5); `max_tokens` → `TurnCompleted(end_reason="max_tokens")`; `refusal` → `TurnFailed("Antigravity refused the turn (agy uses this for quota exhaustion)", plus a reset time if the text gives one)` — the message does not claim quota for certain, because a real model refusal uses the same value; `max_turn_requests` → `TurnFailed("agent turn limit reached")`; JSON-RPC error → `TurnFailed` with the message | `EndReason` is shared; no new member. |
| Hidden API errors | The spike checks whether 1.5.x still returns API errors as text plus `end_turn`. Tradewind does not read `agy`'s private database. If they are still hidden, it is documented as a known limit | — |
| Usage | Turn usage from the `session/prompt` result: input = `inputTokens + cachedReadTokens`, output = `outputTokens + thoughtTokens` | T3 Code `adapter.ts:303-317`. Quota windows out of scope. |
| Tool-round cap | `supports_tool_round_cap=False` | Calls `agy` approves itself are invisible, so an exact cap is impossible. |

### Changes outside the adapter (owner, M12)

PR 1 (one ADR, REQUIREMENTS in the same PR): `permission_mode` /
engine-auto on Claude, Codex and the port (with a capability flag); slash
commands off on Claude and Codex (Codex: verify whether the app server
expands them; test either way); the shared broker vocabulary including
`native`; the result-tool seam; abandoned-call cancellation in `ToolHost`; a
fake backend that exercises the seam; `jsonschema` declared in
`pyproject.toml`; a pre-turn hook on the port, `async def prepare_turn(self,
ctx: TurnContext) -> None` (default no-op), which `turn_runner` awaits before
it starts the FR-6.6 watchdog and before `TurnStarted`, so slow preparation
(the Antigravity version check) does not use the turn's time.

**Result-tool seam (defined in the ADR):**
- A backend declares `uses_result_tool: ClassVar[bool] = False` (True on
  Antigravity). It is a backend attribute, not a capability flag.
- When it is True and the turn's effective `output_schema` is set,
  `turn_runner` builds `ToolHost(..., result_schema=<schema>)`.
- `ToolHost` then serves `tradewind_submit_result` (input schema = the
  schema), never asks the broker for it, refuses a caller tool with that
  name (`ConfigError`), validates each call with `jsonschema`, and counts
  attempts. It exposes `ToolHost.result_slot` with `attempts: int`,
  `last_valid: dict | None` and `clear() -> None`. A 4th call is answered
  "No more attempts." and not recorded.
- The adapter reads `result_slot` at the end of the turn, and calls
  `clear()` when the turn ends early (Structured output step 8).
- The name the model actually sees for the tool is confirmed in the spike.
  If `agy` puts the (per-turn, random) server name in front of MCP tool
  names, the instruction block and the reminder are built per turn after
  the name is chosen, and the non-schema sentence names the tool without
  the prefix ("no tool called tradewind_submit_result is available in this
  turn"). Caller tools then also change their visible names every turn;
  this is documented.

**Inactivity watchdog (owner, 2026-11-15):**
- Backends report activity through an internal channel on `TurnContext`,
  `ctx.report_activity(kind)` with `kind` one of `model` (text, reasoning or
  thinking arrived), `tool_started`, `tool_output` (a running tool printed
  output or progress), `tool_ended`, `background` (a background task
  started, progressed or ended). It is not an event; the frozen taxonomy
  does not change.
- `report_activity(kind, id)` carries an id for `tool_*` and `background`
  kinds. The runner keeps a set of **running work**: tools between
  `tool_started` and `tool_ended`, background tasks/subagents/swarm members
  between `background_started` and `background_ended`, and **pending broker
  questions**. The only source for these is the runner: it wraps the
  broker it puts on `TurnContext` (every backend and `ToolHost` call
  `ctx.broker.decide`), counts pending calls with a counter, and reports
  `approval_ended` in a `finally`, so a cancelled call never stays open.
  Backends do not report approvals themselves. The same wrapped broker is
  given to `ToolHost` (which gets its broker from its constructor) and to
  `TurnContext`, and the wrapper keeps the `is_default_allow_all` marker
  of the broker it wraps, so the no-broker → engine-auto rule still works. (The "broker error or
  timeout → deny" rule in the Permissions table refers to the broker's own
  error or its own timeout, not to these timers.) The kinds are: `model`,
  `tool_started`, `tool_output`, `tool_ended`, `background_started`,
  `background_progress`, `background_ended`, `approval_started`,
  `approval_ended`.
- Timers:
  - **pending approval**: while any broker question is pending, both
    silence timers are paused (a person may be deciding);
  - **model silence** (`TurnDefaults.model_silence_timeout_s`, default 300 s):
    no running work and no `model` activity for this time → timeout;
  - **tool silence** (`TurnDefaults.tool_silence_timeout_s`, default
    1800 s): running work exists and no `tool_output`/`background_progress`/
    start/end activity for this time → timeout, measured from the most
    recent such activity across all running work. On Claude, which streams
    no command output, this is the time since the latest tool start or end;
  - **hard limit** (`request_timeout_s`): type becomes `float | None`, and
    its config default becomes `None`, meaning "the backend's default": no
    hard limit on backends whose `supports_inactivity_watchdog` is True, and
    600 s on others (Cursor). An explicit number applies on every backend.
    The caller may set it per turn. FR-6.6 is reworded to "every backend has
    either the inactivity watchdog or a hard limit".
- All limits are overridable per turn.
- **Timeout record (all backends):** when a limit fires, the runner sets
  `ctx.timeout_reason` (limit name, value, elapsed) before it calls
  `backend.interrupt()`, and itself emits one `ItemCompleted(kind="event")`
  timeout notice, written to the store like any backend item, before the
  synthesized terminal event, so every backend records which limit fired.
  If the backend emits its own terminal event after the interrupt (a race),
  the notice is emitted before that terminal event, never after it.
  Antigravity's process report reads the same field.
- **Stop around `prepare_turn`:** the runner checks for a stop request
  before and after `prepare_turn`; if one is set, it records the turn as
  interrupted without calling `run()`. A terminal record without
  `TurnStarted` is allowed for this case only. The Antigravity backend
  registers the turn as in flight from the start of `prepare_turn` through
  the end of `run()`, so `interrupt()` reaches it at every point.
- Mapping notes: Claude's `StreamEvent`s (from `include_partial_messages`)
  are used only for `report_activity` and are never mapped to events or the
  mirror (a test proves a plain turn's events are identical with the flag on
  and off); Claude subagent messages (`parent_tool_use_id` set) are
  `background_progress`, not `model`; `TaskStartedMessage` is
  `background_started`, `TaskProgressMessage` is `background_progress`, and a
  terminal status (`TaskNotificationMessage`, or
  `TaskUpdatedMessage.patch.status`) is `background_ended`. Codex
  `item/autoApprovalReview/*` and `terminalInteraction`, and LangChain's
  in-turn compaction call, report activity too.
- Wiring: PR 1 — Claude (`include_partial_messages`, tool-use/tool-result
  blocks, `Task*Message`s), Codex (`item/started`, `item/completed`, the
  `outputDelta`/`progress` notifications, message and reasoning deltas),
  LangChain (its own loop). PR 2 — Antigravity (`agent_message_chunk`,
  thought chunks, `tool_call` status, `tool_call_update` content, plan and
  work-state updates).
- A backend declares `supports_inactivity_watchdog` (capability flag).

PR 2: `BackendName` gains `"antigravity"` and the backend registry entry;
`NativeStoreConfig.antigravity_dir`; the `on_workspace_customizations`
callback in `SessionOptions`; the verified-version record; `turn_runner`
passes the broker to `ToolHost` for `antigravity` as for `codex`.

REQUIREMENTS to update (in the PR that changes the behaviour): PR 1 — a new
FR for `permission_mode` / engine-auto, a new FR for "a prompt is always a
message" (slash commands off), the FR-4 broker vocabulary (`native`), the
result-tool seam, FR-6.6 (inactivity watchdog; hard limit optional where
supported), FR-8.1 (the engine-auto and inactivity-watchdog flags), the
`prepare_turn` hook; PR 2 — glossary ("four backends"), FR-1.3 (the Antigravity
reminder exchange), FR-3.1,
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
3. Each call is validated with `jsonschema` (owner-approved; declared in
   `pyproject.toml` in PR 1). Valid → recorded;
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
   thinking and tool calls (owner, M6) — except `tradewind_submit_result`
   calls, which stay hidden as in step 4. The reminder itself is recorded as a
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
| engine-auto (PR 1 flag) | True (checked at run time; needs Auto over ACP, expected in `agy` ≥ 1.5.4) |
| inactivity watchdog (PR 1 flag) | True (wired in PR 2) |

## Turn algorithm

1. `prepare_turn` (before the watchdog and `TurnStarted`): resolve the
   permission mode (broker, engine-auto, or no broker → engine-auto); in
   broker mode, check the `agy` version against the record (run the check if
   unrecorded, under the check lock); detect workspace customizations (C1)
   and get the caller's answer. No files are deleted here.
2. In `run()`: if the answer was `delete`, copy the files to the backup
   folder and delete them.
   Spawn `agy acp` in its own process group (Settings isolation, Environment
   rows), `initialize`, `authenticate`, `session/new` or `session/resume`
   with this turn's shim and the system prompt append. Set `mode`
   (`default` or Auto), then model, then effort.
3. Emit `TurnStarted`. Send `session/prompt` (plus the schema block if set).
4. Map `session/update`s; hide `tradewind_submit_result`; watch the mode (M7).
5. Answer `session/request_permission`s (Permissions and Tools tables).
6. On the prompt result, apply the Stop reasons row and Structured output.
7. Wait for idle (Turn end row). Record `agy`'s process group and its
   descendant PIDs with each process's start time (a PID whose start time
   differs later is a reused PID and is not reported), close the session and terminate `agy` (descendants are
   not killed). Then list the recorded processes that are still alive,
   leaving out the Tradewind shim and `agy`'s own helper processes (the
   spike lists them); if any remain, emit the process report. Then emit the
   terminal event.

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
  workspace-customization outcomes (stop, delete with backup and notice, proceed with notice, no callback → stop);
  slash-prefixed prompt; resume; interrupt with a surviving child process;
  turn waits for a background task; engine-auto and no-broker paths;
  version record; check timeout and its 1-hour backoff; `stop()` while a
  turn waits for the check; the timeout notice before a racing backend
  terminal event; PID reuse not reported; pending approval pauses the
  silence timers; background work keeps model silence from firing;
  capability flags.
- Live conformance (`tests/conformance/test_antigravity.py`, env gate
  `TRADEWIND_RUN_ANTIGRAVITY_INTEGRATION`, `integration` marker, 3-minute
  time box, cheapest model), run once by the operator: plain turn; structured
  output; a denied shell command never runs (marker file); a caller tool
  through the shim; resume continues context; engine-auto turn (only where
  the installed `agy` offers Auto over ACP; otherwise the test asserts the
  `ConfigError`).
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
