# Component: Turn Governance (action policy, work supervisor, preflight, prompt rule)

Status: design (2026-11-15). Owner-approved restructuring of the Antigravity
design (`04-antigravity-adapter.md`) into backend-neutral mechanisms;
revised after an independent review. Implemented in PR 1 (0.12.0). The
Antigravity adapter (`04`) was stopped by the owner on 2026-11-15 because of
Google's terms; Antigravity rows below are history.

## Purpose

Four mechanisms that every backend uses, so that a safety or lifecycle rule
is written once and holds everywhere:

1. **Action policy** — every agent action, whether the engine asks first or
   Tradewind only sees it, is described in one shared vocabulary and decided
   by one rule table: allow, ask the broker, deny, or stop the turn, with an
   optional record.
2. **Work supervisor** — tracks all open work in a turn (tools, background
   tasks, subagents, broker questions) from activity signals and decides
   when a turn is idle, stuck or finished.
3. **Preflight checks** — an ordered list of checks before a turn starts;
   each returns ok, ask the caller, or stop.
4. **Prompt rule** — a prompt is always a message to the model.

A backend adapter only **maps** its engine's messages onto actions and work
signals and declares its rules; it makes no policy decision in code.

### Why

The owner asked for generic mechanisms over problem-specific fixes, "that
don't sacrifice performance, security and accessibility and … deliver the
same functionality if not more" (2026-11-15). The Antigravity design had
grown about 15 special rules; expressed as rules of shared mechanisms they
are:

- **more capable** — callers (Meridian) add their own rules on every
  backend that can intercept actions, e.g. `caller_tool name=merge_pull_request → ask`;
- **as safe** — owner safety rulings are floor rules that a caller can
  tighten but never loosen; one evaluation path to test;
- **as fast** — a rule-table lookup per action, no extra round trips, and
  no new events in a plain turn.

### Standing owner rules this component applies

- **Scope rule** for subscription-CLI backends (Antigravity): not worse than
  the owner's own CLI use; multi-user deployments use LangChain.
- **Security triage:** a real concern gets a rule; a non-issue takes the
  least-impact path.
- **Stopping:** time cap + activity detection + force stop when the cap
  passes with no activity.
- **Implement rulings as stated**; no mechanisms the owner did not ask for.
- Never `--dangerously-skip-permissions` or an equivalent on any engine.

## 1. Action model (shared vocabulary)

Every backend maps each agent action to one `Action`:

```
Action(kind, fields, backend, native_name, native_args, summary,
       unknown_tool=False, missing_critical_field=False)
```

| `kind` | `fields` (critical field in bold) | Examples |
|---|---|---|
| `caller_tool` | **`name`**, `args`, `source` (`local` / `mcp:<server>`) | a local Python `Tool`, or a tool of a caller MCP server — the tools Tradewind itself serves |
| `shell` | **`command`**, `cwd`, `sandboxed: bool \| None`, `interactive: bool` | `git status`; an `agy` escape has `sandboxed=False` |
| `file_read` | **`path`**, `in_workspace` | `read_file ~/.ssh/id_ed25519` |
| `file_write` | **`changes: [{path, change, in_workspace}]`** | Codex `apply_patch` (several files), `agy` `edit_file` (one) |
| `mcp` | **`server`**, **`tool`**, `args` | an MCP server the **engine** connects to itself (not through Tradewind) |
| `fetch_url` | **`url`** | a page read by the engine itself |
| `web_search` | `query` | `agy` `search_web` |
| `subagent` | `type`, `task` | `agy` `invoke_subagent` (`type: browser`) |
| `input_to_running` | `target`, `text` | `agy` `manage_task send_input` |
| `mode_change` | `from`, `to` | `agy` `current_mode_update` |
| `question` | `title`, `choices` | an `agy` `interaction_*` request |
| `native` | — | an engine tool without a shared meaning |

- `unknown_tool=True`: the adapter does not know this engine tool; the
  action is `native` and goes through the rules like any other (owner M8:
  unknown → `native`, Meridian's broker default "ask me").
- `missing_critical_field=True`: a known kind lacks its critical field; the
  floor denies it (owner M8).
- `native_name`, `native_args`, `summary` always carry the engine's own
  form; engine-injected noise (e.g. `agy`'s `toolSummary`) is removed from
  `fields` but kept in `native_args`.

### 1.1 Broker compatibility

The `PermissionBroker` contract does not change. On `ask`, the broker is
called with **today's name and arguments** for that path — the caller
tool's own name (`tool_host.py:438`, `claude_backend.py:656`,
`langchain_backend.py:723`), Codex's `"shell"`/`"apply_patch"`
(`codex_backend.py:751-757`) — plus one added argument key, `"tradewind"`,
holding `{kind, fields, backend, native_name, summary}`. Existing brokers
keep working; new brokers can use the shared vocabulary. The `tradewind` key is added only to the broker's copy of the arguments, never to the arguments the tool receives; if a caller tool has its own `tradewind` argument, the broker's copy uses the key `tradewind_action` instead. A new engine
(Antigravity) uses the shared kind as the name for its built-in tools
matching Codex's names: `shell`, `apply_patch` (`file_write`), `read_file` (`file_read`), and for `native` the engine's own tool name.

## 2. Action policy

### 2.1 Two entry points

- **`gate(action) -> Decision`** — the engine asks before it acts (Claude
  `can_use_tool`, Codex approval requests, ACP `session/request_permission`,
  LangChain's loop, and `ToolHost.call` only where `turn_runner` gives
  `ToolHost` a broker — Codex and Antigravity — so no call is gated twice). Outcomes: `allow`, `ask` (→ broker),
  `deny`, `stop`.
- **`observe(action) -> Observation`** — Tradewind only sees an action that
  the engine never asks about. Adapters observe **only** tools that never
  ask (listed per backend); a tool call that a request may follow is
  decided at the gate only. Outcomes: `allow` (the default) or `stop`. A
  matching rule whose outcome is `deny` or `ask` is applied as `stop`,
  because the action can no longer be prevented.

Every rule may set `record: true`, which emits one notice
(`ItemCompleted(kind="event")`) naming the action.

### 2.2 Rules

```
Rule(id, kind, where: {field: matcher}, outcome, record=False,
     modes={"broker", "engine_auto"}, reason)
matcher: equals | prefix | regex | glob (paths) | is_true | is_false | ne
turn variables usable in matchers: $turn.mode, $turn.engine_mode,
     $turn.shim_server, $turn.workspace, $turn.result_tool
```

### 2.3 Evaluation order

**Structural pass-throughs (before any rule):** the result tool
(`$turn.result_tool`: `tradewind_submit_result` through the shim, Claude's
`StructuredOutput`) is allowed and never asked; a call identified as this
turn's shim at the **engine layer** (ACP request, Codex elicitation) is
allowed there, because it is evaluated once, inside `ToolHost.call`. These
are not rules and cannot be overridden.

**Rules:**

1. **Floor rules** — Tradewind's (§2.4) and each backend's floor rules
   (owner safety rulings). Floor rules only `deny` or `stop` (plus
   `record`). `F` = the strongest matching floor outcome, or none.
2. **Chosen outcome `C`** = the first match among caller rules
   (`SessionOptions.policy_rules`), then backend default rules, then the
   mode default — gate: broker mode `ask`, engine_auto `ask` if a broker is
   set else `allow`; observe: `allow`.
3. **Final outcome** = the stronger of `F` and `C`
   (`stop > deny > ask > allow`). So a caller can tighten anything and can
   loosen only defaults, never a floor rule.

`record` is the OR of the `record` flags of every matching floor rule and of the rule that gave `C` (rules after the first match do not count). Only rules whose `modes`
contain the turn's mode match. Turn variables include `$turn.mode`
(`broker`/`engine_auto`) and `$turn.engine_mode` (the engine's own mode
name, e.g. `default`/`auto`).

### 2.4 Tradewind floor rules

- `missing_critical_field` → `deny`, record.
- Engine options that grant beyond this call are never chosen (§2.6, an
  adapter contract rather than a rule).

### 2.5 Permission modes (owner C2, M9)

| Mode | Selected by | What reaches the gate | Gate default |
|---|---|---|---|
| **broker** | a broker is set and `permission_mode` is not `engine_auto` | everything the engine asks about | `ask` |
| **engine_auto** | `permission_mode="engine_auto"` | only what the engine's auto review sends to the client | `ask` if a broker is set, else `allow` |
| no broker | runner's default allow-all broker (`is_default_allow_all`) | per backend: Antigravity → engine_auto (owner M9); Codex keeps its read-only sandbox; Claude, LangChain unchanged | as the resulting mode |

Engine_auto per backend:
- **Claude:** built-in tools stay off (`tools=[]`); only the engine-side
  mode changes; caller tools still go through `can_use_tool` and the
  policy, so only the mode default changes. If PR 1 finds that the SDK's
  auto mode skips `can_use_tool`, Claude declares
  `supports_engine_auto=False`.
- **Codex:** the auto-reviewer (`approvals_reviewer`, exact setting
  confirmed in PR 1) with the workspace-write sandbox.
- **Antigravity:** `auto` mode with `toolPermission: "request-review"`.
- **LangChain:** no engine of its own → `Unsupported`.

### 2.6 Answering an engine's options (adapter contract)

- `allow` → the one-time allow. If none exists: the narrowest allow scoped
  to this conversation, recorded (owner); **never** an option that persists
  to settings or switches mode. Every request's full option list is logged.
- `deny` → the one-time reject; if none, a conversation-scoped reject; else
  cancel and end the turn (`TurnFailed`, naming the action — owner M5).
- `stop` → cancel and end the turn with `TurnFailed(reason)`; an
  adapter-started cancel is never reported as a caller interrupt.
- Broker error or timeout → `deny`.
- FR-4.4 `terminate` → deny the call, then interrupt; the turn ends as
  `TurnCompleted(end_reason="broker_terminated")`. On a shim path where the
  engine cannot be told (Codex, Antigravity), terminate is not deliverable
  (`tool_host.py:443-446`); documented.
- A shim-path denial emits `PermissionRequested` (Codex's
  `_denied_shim_permission_event` pattern).

### 2.7 Backends that cannot intercept

Cursor cannot intercept tool calls (FR-4.2). It declares
`supports_action_policy=False`; setting `policy_rules` or
`permission_mode` on it raises `Unsupported`.

## 3. Work supervisor

### 3.1 Work items and signals

Backends report through `ctx.work`:

```
started(id, kind: "tool"|"background"|"subagent", label, has_end_signal=True)
active(id)          # output, progress, or a write to the item's own log
ended(id)
model_active()      # text, reasoning or thinking arrived
```

Broker questions are tracked by the runner: one counter-based wrapper
around the broker, given to both `TurnContext` and `ToolHost`, released in
a `finally`, and keeping the `is_default_allow_all` marker.

### 3.2 Limits (owner C-A, inactivity ruling, stopping guideline)

| Limit | Default | Fires when |
|---|---|---|
| model silence | 300 s | no open work and no `model_active` |
| work silence | 1800 s | open work exists and none of it reported anything |
| hard cap | `None` = backend default: none with the supervisor, 600 s without | total time; optional per turn |

All limits pause while a broker question is open; all are overridable per
turn. A backend that cannot report work declares
`supports_work_supervisor=False` and keeps the 600 s hard cap (Cursor).
When a limit fires, the runner sets `ctx.timeout_reason` (limit, value,
elapsed) before `interrupt()` and emits one timeout notice before any
terminal event, on every backend.

### 3.3 Turn end (owner M10)

The turn ends when the engine reports done **and** no work item is open.
Work with `has_end_signal=False` (e.g. `agy` subagents) is closed when it
reports no activity for the work-silence limit **after the engine has
reported done**, provided no work item **with** an end signal is still
open (if one is, the ordinary work-silence limit applies to the whole turn
and ends it as a timeout); this close is a **normal end**, not a timeout: the turn
completes, a structured result is kept, and the process report names the
closed items. The terminal event comes after this point.

### 3.4 Ending engine processes and the process report

| Situation | Engine process | Its descendants |
|---|---|---|
| normal end | terminated | **reported, not killed** (owner M10) |
| caller interrupt answered within 15 s | terminated | reported, not killed |
| interrupt unanswered, or a limit fired | process group, then tree walk | killed; survivors reported |

The process group step applies only when the engine has its own group;
an engine that shares the caller's group gets the tree walk only. The
report is emitted on a fired limit, on an interrupt, or when
descendants are alive at a normal end. Process listing uses `ps` (no new
dependency); a PID whose start time
changed is not reported; the shim and the engine's own helper processes
are excluded. **Process report:** one WARNING log entry and one
`ItemCompleted(kind="event")` before the terminal event, with turn and
session ids, backend, the limit and its value and elapsed time (if any),
open work items, live processes (name, PID, command line, start time), and
the last action. PR 1 applies this to backends that spawn engine processes
(Claude, Codex); LangChain has none.

## 4. Preflight checks

`Backend.prepare_turn(ctx)` runs an ordered list of checks before the
watchdog starts and before `TurnStarted`:

```
Check(id) -> Ok | Ask(question, details, choices, default_choice) | Stop(reason)
```

- `Ask` goes to the caller's `on_preflight(question, details, choices) ->
  Awaitable[choice]` (a live object like the broker; excluded from the
  session snapshot and supplied again at resume). No callback or an error →
  `default_choice`, always the safe one. `stop()` cancels a waiting
  question or check.
- The chosen answer is stored on `TurnContext.preflight`; **anything a
  check needs to change (e.g. deleting files) is done by `run()`, before
  `TurnStarted`**, so a stop between `prepare_turn` and `run()` changes
  nothing and a failure can still raise `ConfigError`; the notice about it
  is emitted right after `TurnStarted`.
- `Stop(reason)` raises `ConfigError(reason)` before `TurnStarted`; no new
  error class.
- A backend registers the turn as in flight from the start of
  `prepare_turn`, so `interrupt()` reaches a turn that is waiting in it.
- A stop request before or after `prepare_turn`, or a stop, limit or
  error while `run()` is still before `TurnStarted`, ends the turn with a
  terminal record and no `TurnStarted` (the runner's existing synthesized
  terminal, `turn_runner.py:720-745`). These are the only such cases
  (FR-2 and the port docstring change).
- Work that `run()` does before `TurnStarted` (e.g. applying a preflight
  answer, spawning the engine) may still raise `ConfigError`; after
  `TurnStarted`, failures end the turn with `TurnFailed`.

## 5. Prompt rule (owner M11)

A prompt is always treated as a message to the model, never expanded as a slash command, on every backend. The rule applies to an engine that expands slash commands: today Claude, which sends a prompt whose first non-whitespace character is `/` with the prefix `Message: ` (the CLI's own flag does not stop built-in commands). Codex's app-server (it passes text as-is; slash commands are a TUI feature) and LangChain (no slash parsing) do not expand slash commands, so they send the prompt unchanged; Cursor sends it unchanged too (tradewind adds no handling there, and the Cursor SDK's behaviour is not verified). The mirror keeps the caller's original prompt.

## 6. Result-tool seam

For engines with no output schema (Antigravity): a backend declares
`uses_result_tool = True`; with an `output_schema`, `turn_runner` builds
`ToolHost(..., result_schema=…)`, which serves `tradewind_submit_result`
(input schema = the caller's schema), validates with `jsonschema`
(owner-approved), allows at most 3 calls (the 4th is answered "No more
attempts." and not recorded), answers a valid call "Result recorded.",
refuses a caller tool of that name, and exposes `result_slot` (`attempts`,
`last_valid`, `clear()`).

## 7. PR 1 backend mappings

| | Gate | Observe | Work signals |
|---|---|---|---|
| **Claude** (`tools=[]`, built-ins off) | `can_use_tool` → `caller_tool`; `$turn.result_tool` = `StructuredOutput` | none (no built-in tools; work-silence on Claude is time since the last tool start or end, because it streams no tool output) | `include_partial_messages` `StreamEvent`s → `model_active` (never mapped to events or the mirror; a test proves plain-turn events are unchanged); tool-use/tool-result blocks → `started`/`ended`; `TaskStartedMessage` → `started(background)`, `TaskProgressMessage` → `active`, `TaskNotificationMessage` / `TaskUpdatedMessage.patch.status` terminal → `ended`; messages with `parent_tool_use_id` → `active` of that task |
| **Codex** | `commandExecution/requestApproval` → `shell`; `fileChange/requestApproval` → `file_write`; MCP elicitations for the shim → structural pass-through, then `caller_tool` inside `ToolHost`; other MCP elicitations → `mcp` (an unreadable tool name → `missing_critical_field`, keeping today's fail-closed rule) | `item/completed` of a command or file change that had **no** approval request → `shell`/`file_write` (record and after-the-fact stop only; `item/started` comes before a possible request, so it is never observed) | `item/started`/`item/completed`; `commandExecution/outputDelta`, `mcpToolCall/progress`, `autoApprovalReview/*`, `terminalInteraction` → `active`; message and reasoning deltas → `model_active` |
| **LangChain** | its own loop → `caller_tool` | none | model stream chunks → `model_active`; tool execution → `started`/`ended`; in-turn compaction call → `model_active` |

## Notes for REQUIREMENTS and RUNBOOK

- On Codex, observe sees only completed commands that had no request; a
  broad caller rule such as `shell → ask` therefore stops a turn after any
  sandboxed command Codex ran without asking (e.g. `ls`). Write narrow
  rules.
- Engine-auto strength per engine: on Claude (built-ins off) engine_auto
  changes only the mode default for caller tools; on Codex and
  Antigravity the engine's auto review decides its own tools inside its
  sandbox.

## Port and interface changes (one ADR)

- `Backend.prepare_turn(ctx)` (default no-op), `Backend.floor_rules()`,
  `Backend.default_rules()`, `uses_result_tool`.
- `TurnContext.policy` (`gate`, `observe`), `.work`, `.timeout_reason`,
  `.preflight`.
- `SessionOptions.permission_mode`, `.policy_rules`, `.on_preflight`.
- `TurnDefaults.model_silence_timeout_s`, `.work_silence_timeout_s`;
  `request_timeout_s: float | None` (`None` = backend default).
- `ToolHost(result_schema=…)`, `.result_slot`; cancellation of abandoned
  shim calls (a late "ask" answer must not run a call the engine gave up
  on).
- Capability flags: `supports_engine_auto`, `supports_work_supervisor`,
  `supports_action_policy`.
- `jsonschema` declared in `pyproject.toml`.

## Delivery

**PR 1 (this component):** the four mechanisms, the result-tool seam, the
§7 mappings, engine_auto and the prompt rule for Claude and Codex, and a
fake backend that exercises gate, observe, work signals with and without
end signals, preflight and the result tool as Antigravity will.
REQUIREMENTS in the same PR: new FRs for the action policy, permission
modes, preflight and the prompt rule; FR-4 (vocabulary, broker
compatibility); FR-6.6 (supervisor; hard cap optional; "every backend");
FR-8.1 (new flags); FR-2 and the port docstring (terminal record without
`TurnStarted`).

**PR 2:** the Antigravity adapter (`04`). Stopped by the owner, 2026-11-15 (Google's terms).

## Acceptance (PR 1)

- Policy: a floor outcome cannot be weakened by a caller rule; a caller
  can tighten; first-match order for caller and default rules; `modes`
  and turn variables; observe default allow and observed `deny`/`ask` →
  `stop`; unknown tools reach the broker as `native`; missing critical
  fields deny; existing brokers receive today's names and arguments plus
  the `tradewind` key; option mapping (§2.6) incl. never choosing
  persistent grants; `terminate` → `broker_terminated`; adapter-started
  cancels give `TurnFailed`; Cursor raises `Unsupported`.
- Supervisor: each limit; pause during a broker question; open background
  work holds the turn; work without an end signal closes as a normal end
  after the engine is done, keeping the result; timeout notice on every
  backend; process report before the terminal event; descendants not
  killed at a normal end; PID reuse.
- Preflight: Ok / Ask (with and without a callback) / Stop as
  `ConfigError`; changes happen in `run()`; stop before and after
  `prepare_turn`.
- Prompt rule on Claude and Codex.
- Result-tool seam through the fake backend; abandoned shim-call
  cancellation.
- Plain turns on Claude, Codex and LangChain produce exactly the same events
  as before.
- Codex: a rule with `ask` on a command that Codex asks about reaches the
  broker (never stopped at observe).
