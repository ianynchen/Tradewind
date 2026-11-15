# Component: Turn Governance (action policy, work supervisor, preflight, prompt rule)

Status: design (2026-11-15). Owner-approved restructuring of the Antigravity
design (`04-antigravity-adapter.md`) into backend-neutral mechanisms. Not
implemented. Delivered in PR 1 (Delivery, below).

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
   each returns ok, ask the caller, fix, or stop.
4. **Prompt rule** — a prompt is always a message to the model.

A backend adapter only **maps** its engine's messages onto actions and work
signals; it makes no policy decisions of its own.

### Why

The owner asked for generic mechanisms over problem-specific fixes, "that
don't sacrifice performance, security and accessibility and … deliver the
same functionality if not more" (2026-11-15). The Antigravity design had
grown about 15 special rules (escape denial, browser ban, search record,
mode watch, slash guard, hook detection, shim auto-accept, timers, turn-end
waits, interrupt rules…), most of them the same few mechanisms. Expressed as
rules of shared mechanisms they are:

- **more capable** — callers (Meridian) add their own rules on every
  backend, e.g. `mcp github.merge_pull_request → ask`;
- **safer** — one evaluation path to test; no forgotten special case;
- **as fast** — a rule-table lookup per action, no extra round trips.

### Standing owner rules this component applies

- **Scope rule** for subscription-CLI backends (Antigravity): Tradewind must
  not make things worse than the owner's own use of the CLI; multi-user
  deployments use LangChain.
- **Security triage:** a real concern gets a rule; a non-issue takes the
  least-impact path.
- **Stopping:** time cap + activity detection + force stop when the cap
  passes with no activity.
- **Implement rulings as stated**; no mechanisms the owner did not ask for.

## 1. Action model (shared vocabulary)

Every backend maps each agent action to one `Action`:

```
Action(kind, fields, backend, native_tool, native_args, summary, unrecognised=False)
```

| `kind` | `fields` | Examples |
|---|---|---|
| `shell` | `command`, `cwd`, `sandboxed: bool \| None`, `interactive: bool` | `git status`; an `agy` escape has `sandboxed=False` |
| `file_read` | `path`, `in_workspace: bool` | `read_file ~/.ssh/id_ed25519` |
| `file_write` | `path`, `in_workspace: bool`, `change` (`create`/`edit`/`delete`) | Codex `apply_patch`, `agy` `edit_file` |
| `mcp` | `server`, `tool`, `args` | the Tradewind shim, a GitHub MCP server |
| `fetch_url` | `url` | a page read by the engine itself |
| `web_search` | `query` | `agy` `search_web` |
| `subagent` | `type`, `task` | `agy` `invoke_subagent` (`type: browser`) |
| `input_to_running` | `target`, `text` | `agy` `manage_task send_input` |
| `mode_change` | `from`, `to` | `agy` `current_mode_update` |
| `question` | `title`, `choices` | an `agy` `interaction_*` request |
| `native` | — | anything without a shared meaning; `unrecognised=True` when the adapter does not know the tool |

Rules for adapters:
- `native_tool`, `native_args` and `summary` always carry the engine's own
  form (engine-injected noise such as `agy`'s `toolSummary` is removed from
  `fields` but kept in `native_args`).
- If an action of a known kind lacks its **critical field** (`command`,
  `path`, `url`, `server`/`tool`), the adapter sets `unrecognised=True`; the
  floor rules deny it (§2.4).
- The `PermissionBroker` contract does not change: an `ask` calls
  `broker.decide(name=action.kind, args={**fields, "native": {...}})`, so
  existing brokers keep working and gain the shared names (owner M8).

## 2. Action policy

### 2.1 Two entry points

- **`gate(action) -> Decision`** — the engine asks before it acts (Claude
  `can_use_tool`, Codex approval requests, ACP `session/request_permission`,
  LangChain's loop, `ToolHost.call`). Possible outcomes: `allow`, `ask`
  (→ broker), `deny`, `stop`.
- **`observe(action) -> Observation`** — Tradewind only sees the action (an
  ACP `tool_call` with no request, a mode change, an engine-approved
  command in auto mode). Possible outcomes: `allow`, `stop`. A rule whose
  outcome is `deny` or `ask` on an observed action is applied as `stop`,
  because the action can no longer be prevented.

Every rule may also set `record: true`, which emits a notice
(`ItemCompleted(kind="event")`) naming the action, whatever the outcome.

### 2.2 Rules

```
Rule(id, kind, where: {field: matcher}, outcome, record=False, reason)
matcher: equals | prefix | regex | glob (paths) | in_workspace | is_true/is_false
```

Example rule table (Meridian, Antigravity, broker mode):

| id | kind | where | outcome | record |
|---|---|---|---|---|
| floor.unrecognised | any | `unrecognised` is true | deny | yes |
| ag.no-escape | shell | `sandboxed` is false | deny | yes |
| ag.no-browser | subagent | `type = browser` | stop | yes |
| ag.search | web_search | — | allow | yes |
| ag.mode | mode_change | `to ≠ <set mode>` | stop | yes |
| ag.question | question | — | stop | yes |
| tw.shim | mcp | `server = <this turn's shim>` | allow | no |
| meridian.merge | mcp | `tool = merge_pull_request` | ask | yes |

### 2.3 Evaluation order

1. **Floor rules** (Tradewind's own, not overridable). All matching floor
   rules apply; the strongest outcome wins (`stop > deny > ask > allow`).
   A caller rule can never weaken a floor outcome.
2. **Caller rules** (`SessionOptions.policy_rules`), first match wins.
3. **Backend default rules** (each adapter's table, e.g. §2.2's `ag.*`),
   first match wins.
4. **Mode default:** broker mode → `ask`; engine-auto → see §2.5.

`record` is the OR of every matching rule's flag.

### 2.4 Floor rules

- `unrecognised` actions of a known kind → `deny` (owner M8).
- The result tool (`tradewind_submit_result`, §6) → `allow`, never asks.
- A caller tool or MCP server whose name collides with an engine built-in
  tool name → preflight `stop` (owner C2), not a runtime rule.

### 2.5 Permission modes (owner C2, M9)

| Mode | Selected by | What reaches the gate | Mode default |
|---|---|---|---|
| **broker** | a broker is set | everything the engine asks about | `ask` |
| **engine_auto** | `permission_mode="engine_auto"` | only what the engine's own auto review sends to the client (e.g. `agy` escapes) | `ask` if a broker is set, else `allow` |
| no broker | runner's default allow-all broker (`is_default_allow_all`) | per backend: Antigravity → engine_auto (owner M9); Codex keeps its read-only sandbox; others unchanged | as above |

Engine settings per backend for engine_auto (PR 1 discovers the exact
names): Claude's auto permission mode; Codex's auto-reviewer
(`approvals_reviewer`) with the workspace-write sandbox; Antigravity's
`auto` mode with `toolPermission: "request-review"` (the spike showed
`strict` overrides Auto). Observed actions still go through `observe` in
every mode.

### 2.6 Answering an engine's options (adapter contract)

When the engine offers choices (ACP options, Codex accept/reject):
- `allow` → the one-time allow option. If none exists, the narrowest allow
  scoped to this conversation, with a record (owner); **never** an option
  that persists to settings or switches mode.
- `deny` → the one-time reject; if none, a conversation-scoped reject;
  else cancel.
- `stop`, or no usable option → cancel and end the turn with
  `TurnFailed(reason)` naming the action (owner M5); an adapter-started
  cancel is never reported as a caller interrupt.
- Broker error or timeout → `deny`.
- FR-4.4 `terminate` → deny, then stop (`end_reason="broker_terminated"`).

## 3. Work supervisor

### 3.1 Work items and signals

Backends report through `ctx.work`:

```
started(id, kind: "tool"|"background"|"subagent", label)
active(id)          # output, progress, or a write to the item's own log
ended(id)
model_active()      # text, reasoning or thinking arrived
```

Broker questions are tracked by the runner itself: it wraps the broker on
`TurnContext` and on `ToolHost` (one wrapper, counter-based, `finally`
releases; the wrapper keeps the `is_default_allow_all` marker).

### 3.2 Limits (owner C-A, inactivity ruling, stopping guideline)

| Limit | Default | Fires when |
|---|---|---|
| model silence | 300 s | no open work and no `model_active` |
| work silence | 1800 s | open work exists and none of it reported `started`/`active`/`ended` |
| hard cap | none on backends with the supervisor; 600 s otherwise | total time (optional, per turn) |

All limits pause while a broker question is open. All are overridable per
turn. A backend that cannot report work declares
`supports_work_supervisor=False` and keeps the hard cap (today: Cursor).

### 3.3 Turn end (owner M10)

The engine's own "done" (e.g. ACP `end_turn`) is necessary but not
sufficient: the turn ends when the engine is done **and** no work item is
open, or when a limit fires. Work that starts during the turn and has no
end signal (e.g. `agy` subagents) is tracked by `active` signals only; the
work-silence limit closes it (owner guideline). The terminal event comes
after this point, so the caller cannot start the next turn while work runs.

### 3.4 Stop and report

On a limit, an interrupt, or a turn end with live engine-started
processes:
1. Interrupt the engine its own way (ACP `session/cancel`, SDK interrupt);
   wait up to 15 s.
2. Normal end and answered interrupt: terminate the engine process only;
   descendants are **reported, not killed**. Unanswered interrupt or a
   fired limit: stop the whole process tree (process group, then a tree
   walk, because sandboxed commands may sit outside the engine's group —
   spike), then report survivors.
3. **Process report** — one WARNING log entry and one
   `ItemCompleted(kind="event")`, before the terminal event: turn and
   session ids, backend, which limit fired with its value and the elapsed
   time, each open work item, each live process (name, PID, command line,
   start time; a PID whose start time changed is not reported), the last
   action.
4. The runner sets `ctx.timeout_reason` before `interrupt()`, and emits the
   timeout notice before any terminal event, for every backend.

## 4. Preflight checks

`Backend.prepare_turn(ctx)` runs an ordered list of checks before the
watchdog starts and before `TurnStarted`:

```
Check(id) -> Ok | Ask(question, choices, default_choice) | Fix(action) | Stop(reason)
```

- `Ask` goes to the caller's `on_preflight(question) -> Awaitable[choice]`
  (a live object like the broker; excluded from the session snapshot; no
  callback or an error → the question's `default_choice`, which is always
  the safe one). `stop()` cancels a waiting question.
- A stop request before or after `prepare_turn` records the turn as
  interrupted without calling `run()`; this is the only case where a
  terminal record has no `TurnStarted`.
- Checks shipped in PR 2 for Antigravity: login, engine version (the
  safety-check record, `04` §Version check), workspace customizations
  (owner C1: choices `stop` / `delete` / `proceed`, default `stop`),
  tool-name collisions (C2).

## 5. Prompt rule (owner M11)

A prompt is always a message to the model. Each adapter turns off its
engine's slash-command expansion where a setting exists, and otherwise
sends a prompt whose first non-whitespace character is `/` with the prefix
`Message: `. The mirror keeps the caller's original prompt. PR 1 wires
Claude and Codex (Codex: verify expansion; test either way).

## 6. Result-tool seam (structured output through the shim)

For backends whose engine has no output schema (Antigravity), structured
output is a tool. A backend declares `uses_result_tool = True`; when the
turn has an `output_schema`, `turn_runner` builds
`ToolHost(..., result_schema=…)`, which serves `tradewind_submit_result`
(input schema = the caller's schema), validates calls with `jsonschema`
(owner-approved), counts attempts, refuses a caller tool of that name, and
exposes `result_slot` (`attempts`, `last_valid`, `clear()`). The reminder,
visibility and `final_text` rules are the adapter's (`04` §Structured
output).

## Port and interface changes (one ADR)

- `Backend.prepare_turn(ctx)` (default no-op); `Backend.default_rules()`;
  `uses_result_tool`.
- `TurnContext.policy` (`gate`, `observe`), `TurnContext.work`,
  `TurnContext.timeout_reason`.
- `SessionOptions.permission_mode`, `policy_rules`, `on_preflight`.
- `TurnDefaults`: `model_silence_timeout_s`, `work_silence_timeout_s`;
  `request_timeout_s: float | None` with `None` = backend default.
- `ToolHost(result_schema=…)`, `result_slot`; cancellation of abandoned
  shim calls (a late "ask" answer must not run a call the engine gave up
  on).
- Capability flags: `supports_engine_auto`, `supports_work_supervisor`.
- `jsonschema` declared in `pyproject.toml`.

## Delivery

**PR 1 (this component):** all four mechanisms and the result-tool seam;
the vocabulary mapping and work signals for Claude, Codex and LangChain;
engine_auto for Claude and Codex; the prompt rule for Claude and Codex; a
fake backend that exercises gate, observe, work signals, preflight and the
result tool as Antigravity will. REQUIREMENTS in the same PR: new FRs for
the action policy, permission modes, preflight and the prompt rule; FR-4
(vocabulary), FR-6.6 (supervisor; hard cap optional; "every backend"),
FR-8.1 (new flags), FR-2 and the port docstring (terminal record without
`TurnStarted` for a stop around `prepare_turn`).

**PR 2:** the Antigravity adapter (`04`), which only maps ACP onto this
component and adds its default rules and preflight checks.

## Acceptance (PR 1)

- Policy: floor outcomes cannot be weakened by caller rules; first-match
  order for caller and default rules; `record` emits one notice; observed
  `deny`/`ask` become `stop`; unrecognised critical fields deny; the
  broker receives the shared names; options mapping (§2.6) incl. never
  choosing persistent grants; adapter-started cancels give `TurnFailed`
  with the action.
- Supervisor: model silence, work silence, hard cap, pause during a broker
  question, open background work holds the turn, work without an end
  signal closes on work silence, process report before the terminal event,
  timeout notice on every backend, PID reuse.
- Preflight: Ok/Ask/Fix/Stop, default choice without a callback, stop
  before and after `prepare_turn`.
- Prompt rule on Claude and Codex.
- Result-tool seam through the fake backend.
- Plain turns on Claude, Codex and LangChain produce the same events as
  before (the vocabulary and supervisor add no events unless a rule
  records or a limit fires).
