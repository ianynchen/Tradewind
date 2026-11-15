# Component: Antigravity Adapter (ACP)

Status: design. Approach approved by the owner (2026-11-15); revised after
an adversarial review and the go/no-go spike
(`docs/research/2026-11-15-antigravity-acp-spike.md`, verdict **GO for
broker mode, conditional**); restructured onto the shared mechanisms in
`05-turn-governance.md` (owner, 2026-11-15). Not implemented. Delivered in
PR 2, after PR 1 (`05`) is merged.

## Purpose

The fifth `Backend`: Google Antigravity through the ACP server in the
Antigravity CLI (`agy acp`, stdio), on the owner's subscription (Google AI
Pro) and every model it offers.

### Why

The owner wants Meridian to use Antigravity "for as many areas in Meridian
as possible": PRDs, FRDs, blueprints, work orders, issues through an MCP
server, code, pull requests (submit, review, comment, approve, merge),
deploy. This adapter gives Meridian structured output, caller tools and
MCP servers, the agent's own tools under the caller's policy, and sessions
that continue.

### Scope rule (owner)

Single user, the owner's machine, login and environment; multi-user uses
LangChain. Test for every safety decision: not worse than the owner's own
`agy` use.

## Goals, non-goals, anti-goals

- Goals: a complete Antigravity backend with structured output, caller
  tools and MCP servers, the agent's own tools under the caller's policy,
  and sessions that continue; honest capability flags.
- Non-goals: LiteLLM (deferred); `agy` print mode; the Meridian features
  themselves; API-key or Vertex auth; passing `agy` questions to the caller
  (later, for all backends); one `agy` process per session.
- Anti-goals: in broker mode, `git push`, a merge or a deploy that runs
  without the broker when the broker would deny it; a structured result
  that is prose or does not validate; `--dangerously-skip-permissions`;
  cutting off work that `agy` started (M10).

## Shape

The adapter **maps** ACP onto `05`: messages → actions (policy `gate` /
`observe`), activity → work signals, plus its default rules, its preflight
checks and its use of the result-tool seam. It makes no policy decision of
its own.

## Approaches (owner)

| | Approach | Verdict |
|---|---|---|
| 1 | **ACP; structured output through the Tradewind result tool** | **Chosen** |
| 2 | `agy` print mode (stream-json; native schema, no approval callback) | Rejected |
| 3 | Both | Rejected |

## Constraints

- **Terms of service** — the owner's decision before production use; the
  binary contains "Your account is blocked due to a Terms of Service
  violation" (risk: a blocked Google account).
- ACP has no output schema; `_meta.antigravity` offers only
  `systemPrompt.append` and `tier`.
- Live tests stay out of CI and run time-boxed on the owner's quota.

## Process, auth and settings

| Decision | Choice | Evidence / why |
|---|---|---|
| Client | A small in-repo ACP JSON-RPC client (no new dependency; a maintained official Python ACP library needs owner approval). Client capabilities `fs.readTextFile=false`, `fs.writeTextFile=false`, `terminal=false`: the agent uses its own tools | ACP is small |
| Parallel turns | Allowed | Spike turns 18–19: no lock or token errors |
| Process model | One `agy acp` process per turn, spawned in its own process group: `initialize` (protocol 1; accept a reply of 1 or 2), `authenticate {methodId: "oauth-personal"}`, `session/new` or `session/resume`, prompt, `session/close`, terminate | Per-turn `ToolHost`; 1.3–1.8 s to a ready session (1.5.3); `close` advertised (1.5.9) |
| Profile **(owner M1)** | `--gemini-dir <NativeStoreConfig.antigravity_dir>`, `--disable-telemetry`, `--include-default-customizations=false`, env `AGY_CLI_DISABLE_AUTO_UPDATE=1`. Tradewind owns `<dir>/antigravity-cli/settings.json` | Settings and conversations are per gemini-dir. The login token is the shared keychain item (`gemini`/`antigravity`), so Tradewind and the owner's `agy` share one login (accepted fallback) |
| Settings | Broker mode: `toolPermission: "strict"`, `enableTerminalSandbox: true`, `disableSlashCommands: true`, `useAICredits: false`, `agentMode: ""`. Engine-auto: the same with `toolPermission: "request-review"` (strict overrides Auto) | Spike turns 1–7, 17 |
| Environment **(owner M4)** | Inherit the owner's environment; remove only auth-switching variables (`GEMINI_API_KEY`, `GOOGLE_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`) | Scope rule |
| Auth errors | `ApiKeyAuth` → `ConfigError`; not logged in (stdout/stderr `Open the following link…`, or `-32000`) → `ConfigError` naming `agy acp --login --gemini-dir <dir>`; `SUBSCRIPTION_REQUIRED` → `ConfigError` | T3 Code |
| Model, effort, mode | `session/set_config_option`: `mode` first, then `model` (unknown → `ConfigError` "model unavailable for this Google account"), then the `thought_level`-category option (validated after the model; unknown → `Unsupported`) | Spike: options `mode`, `model`, `thought_level` |
| System prompt | `_meta.antigravity.systemPrompt.append` on every `session/new` and `session/resume` | Kept across processes on 1.5.9 (spike), resent anyway |
| Resume | `session/resume`; drop `_meta.isReplay` updates as a guard | Spike: works across processes, no replay |
| Shim readiness | After `session/new`/`resume`, wait until the shim has connected to `ToolHost` and listed its tools before `session/prompt` | Spike turn 8: the tool list is taken at the prompt; an early prompt sees no MCP tools |

## Mapping ACP onto actions (`05` §1)

| ACP | Action | Path |
|---|---|---|
| `request_permission`, `kind: execute`, `rawInput.CommandLine` | `shell {command, cwd, sandboxed = not BypassSandbox, interactive = IsDaemon}` | gate |
| `kind: read`, `rawInput.Path` | `file_read {path, in_workspace}` | gate |
| `kind: edit`, `rawInput.Path` (+`Content` or `Target`/`Replacement`) | `file_write {changes: [{path, change, in_workspace}]}` | gate |
| tool name `mcp_<shim>_<tool>` for this turn's shim | identified only if the name starts with this turn's exact `mcp_tradewind-<8 hex>_` prefix **and** the rest is exactly a tool in the shim's own list (the shim is the only MCP server the engine has: workspace MCP config is a preflight stop, and the profile has none) | structural pass-through at the ACP request (`05` §2.3); gated once, as `caller_tool`, inside `ToolHost` |
| any other `mcp_…` name | `native {unknown_tool: true}` (cannot be split reliably) | gate |
| `tool_call` for a tool that never asks: `search_web` (`kind: search`) | `web_search {query}` | observe |
| (none in the spike) a page read by `agy` itself | `fetch_url {url}` if a future version adds such a tool; until then unknown tools are `native` | gate |
| `tool_call` `invoke_subagent` | `subagent {type: TypeName, task: Prompt}` | observe |
| `tool_call` `manage_task` `send_input` | `input_to_running {target: TaskId, text: Input}` | observe |
| `tool_call` `schedule` | `native` (schedules a later prompt in the session) | observe |
| `current_mode_update` | `mode_change {from, to}` | observe |
| `request_permission` with `toolCallId` `interaction_*` | `question {title, choices}` | gate |
| `tool_call` for `manage_subagents`, `send_message`, `define_subagent`, `manage_task` (other actions) | `native` (known, observe default allow) | observe |
| any other tool | `native {unknown_tool: true}`: at the gate → rules, then the broker; never observed (a request may follow) | gate |

Every other `tool_call` is decided only when its `request_permission` arrives.

`toolSummary` is removed from `fields` (kept in `native_args`). The spike
found no `_meta.is_mcp_tool_call`, hence the exact prefix-plus-tool-list
test.

## Rules (`05` §2)

**Floor rules** (owner rulings; a caller can tighten, never loosen):

| id | rule | modes | Owner ruling |
|---|---|---|---|
| ag.no-escape | `shell` with `sandboxed=false` → **deny**, record | broker | d1: the shell is offline; `input_to_running` then stays in the sandbox |
| ag.no-browser | `subagent` with `type=browser` → **stop**, record | both | spike item 1 |
| ag.mode | `mode_change` with `to ne $turn.engine_mode` → **stop**, record | broker | M7 (also watched: config updates that change the mode option) |
| ag.question | `question` → **stop** ("Antigravity asked: '<title>' — questions are not supported yet") | both | M5 |

**Default rules** (a caller may override):

| id | rule | modes | Owner ruling |
|---|---|---|---|
| ag.search | `web_search` → allow, **record** ("Antigravity searched: '<query>'") | both | spike item 2 |
| ag.mode-auto | `mode_change` → allow, record | engine_auto | M7 |

`agy` built-in tools reach the broker under the shared names (`shell`, `apply_patch` for `file_write`, …; `05` §1.1).

Network work goes through MCP tools in the shim (GitHub, issue tracker,
fetch, browser automation), which runs outside `agy`'s sandbox and is
decided per call; or happens in Meridian around the turn (e.g. installing
dependencies). The system-prompt append tells the agent this, and to wait
for its subagents.

## Work signals (`05` §3)

| ACP | Signal |
|---|---|
| `agent_message_chunk`, thought chunks | `model_active` |
| `tool_call` (status `pending`/`in_progress`) | `started(toolCallId, "tool")` |
| `tool_call_update` with content | `active` |
| `tool_call_update` `completed`/`failed` | `ended` |
| `invoke_subagent` started | `started(<subagent id>, "subagent")`, no end signal over ACP |
| a write to `<dir>/antigravity-cli/brain/<subagent id>/.system_generated/logs/transcript*.jsonl` | `active(<subagent id>)` |

Subagents therefore close on the work-silence limit (owner guideline). In
broker mode they stall at their first approval (spike turns 12, 22, 24).

## Preflight checks (`05` §4)

Order matters: the workspace scan runs first, and checks that need a
running `agy` use a probe process in an empty temp folder, so no workspace
hook, skill or MCP config can load before the C1 answer (cost: one extra
`agy` start, about 1.5 s, once per turn).

1. **Workspace customizations (owner C1)** — the scan and question in
   item 4's text below; it runs first and is answered before any `agy`
   runs in the workspace.
2. **Login and Auto (probe in a temp folder):** the probe runs `agy acp`
   with Tradewind's gemini-dir in an empty temp cwd, does `initialize`,
   `authenticate`, `session/new` (to read the `mode` options) and
   `session/close`, then exits; the probe conversation stays in Tradewind's
   gemini-dir and is never resumed. auth errors as above; in
   engine_auto (including the no-broker case), if the `mode` options lack
   `auto` → `Stop` ("update agy or set a broker"); never allow-all (owner,
   M9 safety).
3. **Version check (owner M2):** `agy --version` against Tradewind's record
   of verified versions (pass, fail or timed out, with findings). Before a
   broker-mode turn on an unrecorded version, the safety suite
   `tradewind.adapters.antigravity_check` runs (the spike's traps,
   scripted). Its rules:
   - each trap needs positive evidence that it was attempted (a request
     reached Tradewind, or the trap fired); no evidence → inconclusive,
     retried twice, then the check **fails**;
   - the suite answers every request with allow, in a throwaway workspace,
     with local canaries only;
   - 15-minute cap; reaching it records "timed out" (not a fail): the
     waiting broker-mode turns `Stop` with "Antigravity safety check timed
     out for agy <version>; retry later", and the next broker-mode turn
     retries at most once per hour per version;
   - a path is **risky** if it writes or reads outside the workspace,
     reaches the network (loopback included) or runs a command without a
     request reaching Tradewind (reads under the temp folder and `agy`'s
     customization folder are expected);
   - one run at a time; it runs in its own task and records its result
     even if the waiting turn is stopped;
   - fail → `Stop` with the recorded findings, without rerunning; the owner
     reruns on demand;
   - engine-auto turns are not checked;
   - each run refreshes the built-in tool-name list (shipped fallback list
     from the spike for engine-auto on an unchecked version) and confirms
     the subagent transcript path.
4. **Workspace customizations (owner C1) — details of item 1:** scan the cwd, the repo root and
   the folders between for `.agents/` hooks, plugins, skills and MCP
   config, and `.gemini/` and `.agent/` content (empty skill folders
   ignored; nothing above the repo root — spike). Found → `Ask` with the
   file list as `details` and choices `stop` (default), `delete`,
   `proceed`. The change happens in `run()`, before `TurnStarted`:
   `delete` copies only the scanned files to
   `<data>/backups/<timestamp>-<turn id>/` (paths relative to the repo
   root), then deletes them (a file already gone is skipped; if any copy
   fails, nothing is deleted and `run()` raises `ConfigError`); never
   restored; tracked files show as deletions — the user's responsibility;
   a notice right after `TurnStarted` names the files and the backup path. `proceed` records
   "Antigravity customizations loaded without broker control: <files>". Evidence: a repo-root `hooks.json` ran its
   `SessionStart` and `PreInvocation` hooks at the first prompt with no
   question.
5. **Name collisions (C2):** a caller tool named like an `agy` built-in →
   `Stop`.

**Skills:** not separately blocked: prompt-triggered skills are off (prompt
rule), workspace skills fall under check 4, default customizations are off,
and a skill's actions are ordinary actions.

## Turn algorithm

1. `prepare_turn` (before the watchdog and `TurnStarted`; the turn is
   registered as in flight): resolve the mode; workspace scan and the
   caller's answer; probe `agy` in a temp folder (login, Auto); version
   check in broker mode; name collisions.
2. `run()`, before `TurnStarted`: apply a `delete` answer; spawn `agy` in
   the workspace in its own process group; `initialize`, `authenticate`,
   `session/new` or `session/resume` with this turn's shim and the system
   prompt append; set mode, model, effort; wait for the shim to connect
   (limit 30 s → `ConfigError`). Errors here raise `ConfigError`. After a
   `delete`, the file list and the backup path are logged at WARNING at
   once, and included in any later `ConfigError` message or interrupted
   record, so they are never lost.
3. Emit `TurnStarted`, then any preflight notice; send `session/prompt`
   (with the schema block if set).
4. Map updates to actions and work signals; answer requests through the
   gate.
5. On the prompt result: stop reasons, structured output.
6. Wait for open work (`05` §3.3). An interrupt during this wait ends the
   turn as interrupted and drops a recorded structured result.
7. Process report if needed, then the terminal event; close the session;
   terminate `agy` (descendants are not killed at a normal end).

## Structured output (owner way 1, M6; seam in `05` §6)

1. With an `output_schema`, the shim serves the result tool; the model sees
   it as `mcp_<shim>_tradewind_submit_result` (spike), so the texts below
   are built per turn with that name.
2. The prompt gets a trailing block: "When you have finished, call
   `<result tool>` exactly once with your final answer." The mirror keeps
   only the caller's prompt.
3. Calls are validated; the last valid result wins; at most 3 calls.
4. Result-tool calls are hidden from events and the mirror.
5. If the turn ends without a valid result and fewer than 3 calls were
   made, one reminder is sent: "Call `<result tool>` now with your final
   answer."
6. Everything after the reminder stays visible (except result-tool calls);
   the reminder is recorded as a notice; `final_text` is the main answer.
7. Still no result → `end_turn`, `structured_output=None`.
8. A result recorded before an early end is dropped.
9. Non-schema turns: the append says "no tool called
   tradewind_submit_result is available in this turn".

Follow-up (separate, small): make Claude's `final_text` rule the same when
its CLI reminder fires.

## Stop reasons

`end_turn` → structured output, then `05` §3.3; `cancelled` by the caller →
interrupted; `cancelled` by the adapter → `TurnFailed` with the action;
`max_tokens` → `TurnCompleted(max_tokens)`; `refusal` → `TurnFailed`
("Antigravity refused the turn; agy uses this for quota exhaustion");
`max_turn_requests` → `TurnFailed`; JSON-RPC error → `TurnFailed` with the
message. Usage: input = `inputTokens + cachedReadTokens`, output =
`outputTokens + thoughtTokens`.

## Capabilities

| Flag | Value |
|---|---|
| `supports_system_prompt` | True (appended) |
| `supports_structured_output` | True (result tool; may complete with `None`) |
| `supports_interactive_permissions` | True |
| `supports_in_process_tools` | False (MCP shim, as on Codex) |
| `supports_native_resume` | True |
| `supports_fork`, `supports_transcript_read`, `supports_turn_retry`, `supports_deny_reason`, `supports_tool_round_cap` | False |
| `supports_engine_auto` | True (`agy` ≥ 1.5.9 offers `auto` over ACP) |
| `supports_work_supervisor` | True |

## Interface changes (PR 2; ADR amendment)

`BackendName` gains `"antigravity"` and its registry entry;
`NativeStoreConfig.antigravity_dir`; the verified-version record;
`turn_runner` gives the broker to `ToolHost` for `antigravity` as for
`codex`. REQUIREMENTS: glossary and FR-9.1 ("four backends"), FR-1.3 (the
reminder exchange), FR-3.1, FR-4.1, FR-6.3/6.4 (isolated store; no
transcript reconcile), FR-8.2, NFR-4 (latest `agy` plus the automatic
check), a new FR for the C1 preflight check.

## Acceptance (PR 2)

- Unit tests against a fake ACP agent: every mapping row, every default
  rule, every preflight check, structured output (valid first call; invalid
  then valid; no call → reminder → valid; 3 invalid → no reminder → `None`;
  early end drops the result; reminder content visible; `final_text`),
  shim readiness wait, resume, interrupt, subagent work closing on work
  silence, process report.
- Live tests (`tests/conformance/test_antigravity.py`, env gate
  `TRADEWIND_RUN_ANTIGRAVITY_INTEGRATION`, `integration` marker, 3-minute
  box, cheapest model) **must pass before the backend is used**: plain
  turn; structured output; a denied shell command never runs; an escape is
  denied in broker mode; a caller MCP tool through the shim; resume; an
  engine-auto turn; a browser subagent stops the turn; a built-in
  `write_file` outside the workspace reaches the broker; an API error
  (invalid model) is reported as an error, not as text; `agy`'s MCP call
  timeout is measured and recorded in RUNBOOK.
- RUNBOOK: login, profile dir, the safety suite and how to rerun it,
  engine-auto strength per engine, ToS note. ARCHITECTURE §3.1, README,
  CHANGELOG.

## References

- Spike: `docs/research/2026-11-15-antigravity-acp-spike.md`.
- T3 Code (MIT, © 2026 T3 Tools Inc.), commit `2579c3c4`: provider and ACP
  sources listed in the spike doc. Copied logic or fixtures keep the MIT
  notice.
- Antigravity docs: /permissions, /sandbox, /settings, /hooks, /cli/acp.
- Upstream ACP schema `schema-v1.21.0`.
