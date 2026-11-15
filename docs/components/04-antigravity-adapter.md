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
| Process model | One `agy acp` process per turn, spawned in its own process group: `initialize` (protocol 1; accept a reply of 1 or 2), `authenticate {methodId: "oauth-personal"}`, `session/new` or `session/resume`, prompt, `session/close`, terminate | Per-turn `ToolHost`; 1.3–1.8 s to a ready session (1.5.3); `close` advertised (1.5.9) |
| Profile **(owner M1)** | `--gemini-dir <NativeStoreConfig.antigravity_dir>`, `--disable-telemetry`, `--include-default-customizations=false`, env `AGY_CLI_DISABLE_AUTO_UPDATE=1`. Tradewind owns `<dir>/antigravity-cli/settings.json` | Settings and conversations are per gemini-dir. The login token is the shared keychain item (`gemini`/`antigravity`), so Tradewind and the owner's `agy` share one login (accepted fallback) |
| Settings | Broker mode: `toolPermission: "strict"`, `enableTerminalSandbox: true`, `disableSlashCommands: true`, `useAICredits: false`, `agentMode: ""`. Engine-auto: the same with `toolPermission: "request-review"` (strict overrides Auto) | Spike turns 1–7, 17 |
| Environment **(owner M4)** | Inherit the owner's environment; remove only auth-switching variables (`GEMINI_API_KEY`, `GOOGLE_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`) | Scope rule |
| Auth errors | `ApiKeyAuth` → `ConfigError`; not logged in (stdout/stderr `Open the following link…`, or `-32000`) → `ConfigError` naming `agy acp --login --gemini-dir <dir>`; `SUBSCRIPTION_REQUIRED` → `ConfigError` | T3 Code |
| Model, effort, mode | `session/set_config_option`: `mode` first, then `model` (validated), then the `thought_level`-category option (validated after the model) | Spike: options `mode`, `model`, `thought_level` |
| System prompt | `_meta.antigravity.systemPrompt.append` on every `session/new` and `session/resume` | Kept across processes on 1.5.9 (spike), resent anyway |
| Resume | `session/resume`; drop `_meta.isReplay` updates as a guard | Spike: works across processes, no replay |
| Shim readiness | After `session/new`/`resume`, wait until the shim has connected to `ToolHost` and listed its tools before `session/prompt` | Spike turn 8: the tool list is taken at the prompt; an early prompt sees no MCP tools |

## Mapping ACP onto actions (`05` §1)

| ACP | Action | Path |
|---|---|---|
| `request_permission`, `kind: execute`, `rawInput.CommandLine` | `shell {command, cwd, sandboxed = not BypassSandbox, interactive = IsDaemon}` | gate |
| `kind: read`, `rawInput.Path` | `file_read {path, in_workspace}` | gate |
| `kind: edit`, `rawInput.Path` (+`Content` or `Target`/`Replacement`) | `file_write {path, in_workspace, change}` | gate |
| tool name `mcp_<server>_<tool>` | `mcp {server, tool, args}`; `server` = the shim only if the name starts with this turn's exact `mcp_tradewind-<8 hex>_` prefix **and** `<tool>` is in the shim's own tool list | gate |
| `tool_call` `search_web` (`kind: search`) | `web_search {query}` | observe |
| `tool_call` `invoke_subagent` | `subagent {type: TypeName, task: Prompt}` | observe |
| `tool_call` `manage_task` `send_input` | `input_to_running {target: TaskId, text: Input}` | observe |
| `tool_call` `schedule` | `native` (schedules a later prompt in the session) | observe |
| `current_mode_update` | `mode_change {from, to}` | observe |
| `request_permission` with `toolCallId` `interaction_*` | `question {title, choices}` | gate |
| anything else | `native {…, unrecognised: true}` | gate or observe |

`toolSummary` is removed from `fields` (kept in `native_args`). The spike
found no `_meta.is_mcp_tool_call`, hence the exact prefix-plus-tool-list
test.

## Default rules (`05` §2.2)

| id | rule | Owner ruling |
|---|---|---|
| ag.no-escape | `shell` with `sandboxed=false` → **deny**, record (broker mode only) | d1: the agent's shell is offline; `input_to_running` then stays in the sandbox |
| ag.no-browser | `subagent` with `type=browser` → **stop**, record | spike item 1 |
| ag.search | `web_search` → allow, **record** | spike item 2 |
| ag.mode | `mode_change` away from the set mode → **stop** (broker mode), record (engine-auto) | M7 |
| ag.question | `question` → **stop** ("Antigravity asked: '<title>' — questions are not supported yet") | M5 |
| tw.shim | `mcp` on this turn's shim → allow (the broker is asked inside `ToolHost`, as on Codex) | C2 |

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

1. Login (auth errors above).
2. **Version check (owner M2):** `agy --version` against Tradewind's record
   of verified versions (pass, fail or timed out, with findings). An
   unrecorded version, before a broker-mode turn, runs the safety suite
   `tradewind.adapters.antigravity_check` (the spike's traps, scripted;
   15-minute cap; one run at a time; "timed out" retries at most hourly).
   Fail → `Stop` with the findings for broker-mode turns; engine-auto turns
   are not checked. The suite also re-reads the built-in tool names and
   confirms the subagent transcript path.
3. **Workspace customizations (owner C1):** scan the cwd, the repo root and
   the folders between for `.agents/` hooks, plugins, skills and MCP
   config, and `.gemini/` and `.agent/` content (empty skill folders
   ignored; nothing above the repo root — spike). Found → `Ask` with
   choices `stop` (default), `delete` (copy to a timestamped backup in
   Tradewind's data folder, then delete; never restored; tracked files show
   as deletions — the user's responsibility), `proceed` (record "loaded
   without broker control"). Evidence: a repo-root `hooks.json` ran its
   `SessionStart` and `PreInvocation` hooks at the first prompt with no
   question.
4. **Name collisions (C2):** a caller tool named like an `agy` built-in →
   `Stop`.

## Structured output (owner way 1, M6; seam in `05` §6)

1. With an `output_schema`, the shim serves the result tool; the model sees
   it as `mcp_<shim>_tradewind_submit_result` (spike), so the instruction
   block and the reminder are built per turn with that name.
2. The prompt gets a trailing instruction block; the mirror keeps only the
   caller's prompt.
3. Calls are validated; the last valid result wins; at most 3 calls.
4. Result-tool calls are hidden from events and the mirror.
5. If the turn ends without a valid result and fewer than 3 calls were
   made, one reminder is sent.
6. Everything after the reminder stays visible (except result-tool calls);
   the reminder is recorded as a notice; `final_text` is the main answer.
7. Still no result → `end_turn`, `structured_output=None`.
8. A result recorded before an early end is dropped.
9. Non-schema turns: the append says the result tool is not available.

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
- Live tests (`tests/conformance/test_antigravity.py`, env gate, 3-minute
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
