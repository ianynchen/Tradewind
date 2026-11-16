# Turn Governance (PR 1) Implementation Plan

> **History.** The Antigravity adapter was stopped by the owner on 2026-11-15 because of Google's terms (`docs/components/04-antigravity-adapter.md`, Status). References to PR 2 below are not planned work.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every Tradewind backend one action policy, one work supervisor, one preflight step and one prompt rule, plus the result-tool seam, without changing plain-turn behaviour.

**Architecture:** Pure decision logic lives in `tradewind.domain` (action model, rule evaluation, limit arithmetic). Application-level objects (`PolicyGate`, `WorkSupervisor`, preflight runner, process report) live in `tradewind.application` and are wired by `turn_runner` onto `TurnContext`. Adapters only map engine messages onto `Action`s and work signals. The FR-6.6 wall-clock watchdog becomes the work supervisor.

**Tech Stack:** Python 3.12/3.13, pydantic, anyio/asyncio, pytest + pytest-asyncio, `jsonschema` (new declared dependency, owner-approved).

**Spec:** `docs/components/05-turn-governance.md` (commit `31af07b`); context `docs/components/04-antigravity-adapter.md`.

## Global Constraints

- GUIDELINES.md binds (§1–4, §7, §11, §13.5): no CI change, match existing patterns, no unrequested refactors.
- **Version (owner, 2026-11-15):** bump the minor version after implementation: `0.11.2` → `0.12.0` (Task 12).
- One ADR (`docs/adr/0004-turn-governance.md`); REQUIREMENTS updated in the same PR (05 §Delivery list).
- `jsonschema` is the only new runtime dependency. Process listing uses `ps`, not `psutil`.
- Plain turns on Claude, Codex and LangChain emit exactly the same events as before (verified by a test per backend).
- Existing brokers receive today's names and arguments; the shared vocabulary is added under the broker-copy key `"tradewind"` (`"tradewind_action"` if the tool already has a `tradewind` argument).
- Owner rules: floor rules only deny/stop; final outcome = stronger of floor and first match; observe default allow; never choose an engine option that grants beyond this call.
- Gate command: `bash scripts/check.sh`, chained with `&&` to commit; never `;` or a pipe that hides its exit code.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. A caller rule that would loosen a floor `deny`/`stop` — the final outcome must stay the floor's (test in Task 2).
2. A plain turn that gains or loses an event because of the new activity mapping (e.g. Claude `StreamEvent`s leaking into events) — per-backend equality tests (Tasks 8–10).
3. A broker question pending for a long time (a human deciding) — no silence limit may fire (Task 4).
4. A cancelled broker call (`stop()` during an "ask") that leaves the pending-approval counter above zero, so limits pause forever — counter released in `finally` (Task 3).
5. A stop between `prepare_turn` and `run()` — no backend side effect, terminal record without `TurnStarted` (Task 6).

---

### Task 1: Action model and rules (domain)

**Files:**
- Create: `src/tradewind/domain/policy.py`
- Test: `tests/unit/test_policy_rules.py`

**Interfaces:**
- Produces:
  - `ActionKind = Literal["caller_tool","shell","file_read","file_write","mcp","fetch_url","web_search","subagent","input_to_running","mode_change","question","native"]`
  - `@dataclass(frozen=True) class Action: kind: ActionKind; fields: dict[str, object]; backend: str; native_name: str; native_args: dict[str, object]; summary: str = ""; unknown_tool: bool = False; missing_critical_field: bool = False`
  - `Outcome = Literal["allow","ask","deny","stop"]`; `OUTCOME_RANK = {"allow":0,"ask":1,"deny":2,"stop":3}`
  - `Mode = Literal["broker","engine_auto"]`
  - `@dataclass(frozen=True) class Matcher: op: Literal["equals","ne","prefix","regex","glob","is_true","is_false"]; value: object = None`
  - `@dataclass(frozen=True) class Rule: id: str; kind: ActionKind | Literal["any"]; where: dict[str, Matcher]; outcome: Outcome; record: bool = False; modes: frozenset[Mode] = frozenset({"broker","engine_auto"}); reason: str = ""`
  - `@dataclass(frozen=True) class TurnVars: mode: Mode; engine_mode: str | None; shim_server: str | None; workspace: str | None; result_tool: str | None`
  - `@dataclass(frozen=True) class PolicyResult: outcome: Outcome; record: bool; rule_ids: tuple[str, ...]; reason: str`
  - `def evaluate(action: Action, *, floor: Sequence[Rule], caller: Sequence[Rule], defaults: Sequence[Rule], mode_default: Outcome, turn: TurnVars) -> PolicyResult`
  - `def floor_tradewind() -> list[Rule]` (one rule: `missing_critical_field` → deny, record)
- Matchers resolve string values that start with `$turn.` from `TurnVars`. `glob` uses `fnmatch` on paths. A missing field never matches (except `ne`, which matches a missing field).

- [ ] **Step 1: Write failing tests**
  - `test_floor_deny_beats_caller_allow`: floor `shell sandboxed is_false → deny`, caller `shell → allow` → `outcome == "deny"`, `rule_ids` contains the floor id.
  - `test_caller_can_tighten_default`: default `web_search → allow`, caller `web_search → stop` → `"stop"`.
  - `test_first_match_caller_then_defaults_then_mode_default`: no caller match, default match → default outcome; no matches → `mode_default`.
  - `test_record_is_floor_or_chosen_rule`: floor record=False, chosen caller rule record=True, a later default rule record=True → `record is True`; with chosen record=False and only a later default record=True → `record is False`.
  - `test_modes_filter`: rule with `modes={"broker"}` does not match in `engine_auto`.
  - `test_turn_variables`: `Matcher("ne", "$turn.engine_mode")` with `engine_mode="default"` matches `to="auto"`, not `to="default"`.
  - `test_missing_critical_field_denied_by_tradewind_floor`.
  - `test_floor_rules_reject_allow_and_ask`: `evaluate` raises `ValueError` if a floor rule's outcome is `allow` or `ask`.
- [ ] **Step 2:** `uv run pytest tests/unit/test_policy_rules.py -q` → FAIL (import error).
- [ ] **Step 3:** Implement `policy.py` to the interfaces above. Algorithm: filter rules by `modes` and match; `F = max(rank of matching floor)`; `C = first matching caller rule, else first matching default, else mode_default`; outcome = stronger of `F` and `C`; record = any matching floor `record` or the chosen rule's `record`.
- [ ] **Step 4:** Run the tests → PASS.
- [ ] **Step 5:** `bash scripts/check.sh > /tmp/gov1.log 2>&1 && git add -A && git commit -m "feat(domain): action model and policy rule evaluation"`

### Task 2: PolicyGate (gate, observe, broker compatibility, pass-throughs)

**Files:**
- Create: `src/tradewind/application/policy_gate.py`
- Modify: `src/tradewind/domain/models.py` (add `SessionOptions.permission_mode: Literal["broker","engine_auto"] | None = None`, `policy_rules: list[Rule] = []`; include both in `snapshot()` — rules are declarative)
- Test: `tests/unit/test_policy_gate.py`

**Interfaces:**
- Consumes: Task 1.
- Produces:
  - `@dataclass(frozen=True) class GateDecision: outcome: Literal["allow","deny","stop"]; reason: str | None; terminate: bool; record: bool; rule_ids: tuple[str, ...]`
  - `class PolicyGate:`
    - `__init__(self, *, broker: PermissionBroker, floor: Sequence[Rule], caller: Sequence[Rule], defaults: Sequence[Rule], turn: TurnVars, has_broker: bool, emit: Callable[[Event], None])`
    - `async def gate(self, action: Action, *, broker_name: str, broker_args: dict[str, object], pass_through: bool = False) -> GateDecision`
    - `def observe(self, action: Action) -> Literal["allow","stop"]`
    - `pending_approvals: int` (read-only property)
    - `on_approval_change: Callable[[int], None] | None` (set by the supervisor, Task 4)
  - `def broker_args_with_action(args: dict[str, object], action: Action) -> dict[str, object]` (adds `"tradewind"` or `"tradewind_action"`; never mutates `args`)
- Rules: `pass_through=True` → `allow` without rules or broker. `ask` → `broker.decide(broker_name, broker_args_with_action(...))`, normalised with `normalize_decision`; broker exception → `deny` with reason; `Denial(terminate=True)` → `deny` + `terminate=True`. Mode default: broker mode `ask`; engine_auto `ask` if `has_broker` else `allow`. `observe` maps `deny`/`ask` to `stop`. When `record`, `emit(ItemCompleted(...kind="event"...))` with action kind, summary and rule ids (follow the FR-6.6 notice form used by `retry_notice`).
- Pending counter: incremented before `broker.decide`, decremented in `finally`; calls `on_approval_change(count)` each time.

- [ ] **Step 1: Write failing tests**
  - `test_existing_broker_receives_todays_name_and_args`: a recording broker sees `name == "merge_pull_request"` and `args["pr"] == 7` and `args["tradewind"]["kind"] == "caller_tool"`; the args dict passed in is unchanged.
  - `test_tradewind_key_collision_uses_tradewind_action`.
  - `test_pass_through_never_asks` (result tool, shim at engine layer).
  - `test_observe_default_allow_and_deny_becomes_stop`.
  - `test_broker_exception_denies`; `test_denial_terminate_sets_terminate`.
  - `test_pending_counter_released_on_cancel`: cancel the task during a blocking broker → `pending_approvals == 0`.
  - `test_record_emits_one_notice`.
  - `test_snapshot_includes_permission_mode_and_rules`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5:** Gate && commit `feat(application): policy gate with broker compatibility`.

### Task 3: Turn context wiring and the one broker wrapper

**Files:**
- Modify: `src/tradewind/application/ports.py` (`TurnContext`: add `policy: PolicyGate | None = None`, `work: WorkSupervisor | None = None`, `timeout_reason: dict[str, object] | None = None`, `preflight: dict[str, str] = field(default_factory=dict)`; `Backend`: add `async def prepare_turn(self, ctx) -> None` (no-op), `def floor_rules(self) -> list[Rule]` and `def default_rules(self) -> list[Rule]` (both return `[]`), `uses_result_tool: ClassVar[bool] = False`; `Capabilities` additions in Task 11)
- Modify: `src/tradewind/application/turn_runner.py` (build one `PolicyGate` per turn and pass the same gate's broker view to `ToolHost` where `turn_runner` gives it a broker today, i.e. `backend.name in {"codex"}`; keep `is_default_allow_all` readable: `has_broker = not is_default_allow_all(broker)`)
- Test: `tests/unit/test_turn_runner.py` (extend)

**Interfaces:**
- Consumes: Task 2.
- Produces: `TurnContext.policy` is always set by the runner; adapters call `ctx.policy.gate(...)` / `ctx.policy.observe(...)`.

- [ ] **Step 1: Write failing tests**
  - `test_runner_sets_policy_on_context`.
  - `test_codex_toolhost_uses_gate_broker_once`: a counting broker is asked once per shim call.
  - `test_no_broker_detection_still_works` (`has_broker is False` with the default broker).
- [ ] **Steps 2–5:** run → fail → implement → pass → gate && commit `feat: policy gate on the turn context`.

### Task 4: Work supervisor (replaces the wall-clock watchdog)

**Files:**
- Create: `src/tradewind/domain/supervision.py` (pure limit arithmetic), `src/tradewind/application/supervisor.py`
- Modify: `src/tradewind/application/config.py` (`TurnDefaults.model_silence_timeout_s: float = 300.0`, `work_silence_timeout_s: float = 1800.0`, `request_timeout_s: float | None = None`), `turn_runner.py` (replace `_watchdog` with the supervisor; `_effective_request_timeout` accepts `None`)
- Test: `tests/unit/test_supervision.py`, `tests/unit/test_turn_runner.py`

**Interfaces:**
- Produces:
  - `domain.supervision.next_deadline(now: float, state: SupervisorState, limits: Limits) -> tuple[float | None, str | None]` returning the next firing time and the limit name (`"model_silence"`, `"work_silence"`, `"hard_cap"`), `None` while approvals are pending.
  - `class WorkSupervisor:` `started(id: str, kind: Literal["tool","background","subagent"], label: str, has_end_signal: bool = True)`, `active(id: str)`, `ended(id: str)`, `model_active()`, `engine_done()`, `open_items() -> list[WorkItem]`, `async def run(on_fire: Callable[[str, float, float], Awaitable[None]])`, `async def settled() -> bool` (wakes when `run()` ends: True = normal end, False = a limit fired or `run()` was cancelled; adapters await it before their terminal event while still reading the engine stream), `closed_after_done() -> list[WorkItem]` (read-only), `approvals_changed(count: int)` (wired to `PolicyGate.on_approval_change`).
  - Limits: `Limits(model_silence_s, work_silence_s, hard_cap_s | None)`; backend default for the hard cap: `None` when `capabilities().supports_work_supervisor` else `600.0`.
- On fire, the runner sets `ctx.timeout_reason = {"limit": name, "value": v, "elapsed": e}`, emits one timeout notice (`ItemCompleted(kind="event")`, written to the store) **before** any terminal event, then calls `backend.interrupt()`; the synthesized terminal keeps `end_reason="timeout"`.
- `engine_done()` + only unsignalled items open → those close after `work_silence_s` of no activity as a **normal end** (`run()` closes them; `closed_after_done()` returns them for the report).

- [ ] **Step 1: Write failing tests** (fake clock; no real sleeps)
  - `test_model_silence_fires_without_open_work`.
  - `test_open_work_uses_work_silence`; `test_active_resets_work_silence`.
  - `test_pending_approval_pauses_all_limits`.
  - `test_hard_cap_none_means_no_cap`; `test_cursor_keeps_600s_default`.
  - `test_unsignalled_work_after_engine_done_is_normal_end`.
  - `test_signalled_open_work_after_done_times_out_on_work_silence`.
  - `test_timeout_notice_before_terminal_on_every_backend` (runner test with a fake backend that emits its own terminal after interrupt).
  - `test_explicit_request_timeout_still_applies`.
- [ ] **Steps 2–5:** run → fail → implement → pass → gate && commit `feat: work supervisor replaces the wall-clock watchdog (FR-6.6)`.

### Task 5: Process report

**Files:**
- Create: `src/tradewind/application/process_report.py`
- Test: `tests/unit/test_process_report.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class ProcInfo: pid: int; ppid: int; pgid: int; start: str; command: str`
  - `def list_processes(run_ps: Callable[[], str] = _ps) -> list[ProcInfo]` (parses `ps -A -o pid=,ppid=,pgid=,lstart=,command=`)
  - `def descendants(root_pid: int, root_pgid: int, procs: list[ProcInfo]) -> list[ProcInfo]` (process group members plus the parent-tree walk)
  - `def still_alive(recorded: list[ProcInfo], now: list[ProcInfo], exclude: Callable[[ProcInfo], bool]) -> list[ProcInfo]` (same pid **and** same start)
  - `def kill_tree(procs: list[ProcInfo]) -> None` (SIGTERM, then SIGKILL after 3 s)
  - `def report_event(*, turn_id, session_id, backend, timeout_reason, open_items, alive, last_action) -> ItemCompleted` and `log_report(...)` (WARNING)
- [ ] **Step 1: Write failing tests** with canned `ps` output: parsing; tree walk finds a child outside the group; PID reuse (same pid, new start) not reported; exclusion predicate; report event fields.
- [ ] **Steps 2–5:** run → fail → implement → pass → gate && commit `feat: process listing and report`.

### Task 6: Preflight and stop around it

**Files:**
- Create: `src/tradewind/application/preflight.py`
- Modify: `domain/models.py` (`SessionOptions.on_preflight: Callable[[str, dict[str, object], list[str]], Awaitable[str]] | None = None`, excluded from `snapshot()` like `permission_broker`), `ports.py` (port docstring: terminal record without `TurnStarted` for stops/errors before `TurnStarted`), `turn_runner.py` (await `backend.prepare_turn(ctx)` after building `ctx` and **before** starting the supervisor and iterating `run()`; check the stop flag before and after)
- Test: `tests/unit/test_preflight.py`, `tests/unit/test_turn_runner.py`

**Interfaces:**
- Produces:
  - `Ok`, `Ask(question: str, details: dict[str, object], choices: list[str], default_choice: str)`, `Stop(reason: str)` result types; `Check = Callable[[TurnContext], Awaitable[Ok | Ask | Stop]]`
  - `async def run_checks(ctx: TurnContext, checks: Sequence[tuple[str, Check]], ask: Callable[..., Awaitable[str]] | None) -> None` — stores answers in `ctx.preflight[check_id]`; `Stop` raises `ConfigError(reason)`; no callback or callback error → `default_choice`.
- [ ] **Step 1: Write failing tests**
  - `test_ask_without_callback_uses_default`; `test_callback_error_uses_default`; `test_stop_raises_config_error`.
  - `test_on_preflight_not_in_snapshot`.
  - `test_stop_before_prepare_turn_records_interrupted_without_run`; `test_stop_after_prepare_turn_records_interrupted_without_run`; `test_run_not_called_and_no_side_effect`.
  - `test_prepare_turn_time_not_counted_by_supervisor`.
- [ ] **Steps 2–5:** run → fail → implement → pass → gate && commit `feat: preflight checks and the prepare_turn hook`.

### Task 7: Result-tool seam and abandoned shim calls

**Files:**
- Modify: `pyproject.toml` (add `jsonschema` with a version range; `uv lock`), `src/tradewind/application/tool_host.py`, `turn_runner.py` (pass `result_schema` when `backend.uses_result_tool` and the effective `output_schema` is set)
- Test: `tests/unit/test_tool_host.py`

**Interfaces:**
- Produces: `ToolHost(..., result_schema: dict[str, object] | None = None)`; tool name `RESULT_TOOL_NAME = "tradewind_submit_result"`; `ToolHost.result_slot` with `attempts: int`, `last_valid: dict | None`, `clear() -> None`. Replies: valid → `"Result recorded."`; invalid → the `jsonschema` error messages; 4th call → `"No more attempts."` (not recorded). A caller tool named `tradewind_submit_result` → `ConfigError`. The result tool never consults the broker.
- Abandoned calls: when the shim's socket connection for a call closes before the reply, `_handle_socket_line` cancels the pending call (including a pending broker question).
- [ ] **Step 1: Write failing tests**: valid, invalid-then-valid, last valid wins, 4th call refused, name collision, broker never asked, `clear()`, abandoned call cancels a blocking broker and the tool handler never runs.
- [ ] **Steps 2–5:** run → fail → implement → pass → gate && commit `feat(tool_host): result-tool seam and abandoned-call cancellation`.

### Task 8: Claude mapping

**Files:**
- Modify: `src/tradewind/adapters/claude_backend.py`
- Test: `tests/unit/test_claude_backend.py`, `tests/unit/test_claude_mapping.py`

**Interfaces:**
- Consumes: Tasks 2–6.
- Mapping (05 §7): `can_use_tool` → `ctx.policy.gate(Action(kind="caller_tool", fields={"name":…, "args":…, "source":…}), broker_name=<tool name as today>, broker_args=<input as today>)`; `StructuredOutput` → `pass_through=True`. `include_partial_messages=True`; `StreamEvent` → `ctx.work.model_active()` only. Tool-use block → `started(id,"tool")`, tool-result block → `ended(id)`; `TaskStartedMessage` → `started(task_id,"background")`; `TaskProgressMessage` and messages with `parent_tool_use_id` → `active`; `TaskNotificationMessage` / terminal `TaskUpdatedMessage.patch.status` → `ended`. Result message → `engine_done()`.
- engine_auto: set the SDK's auto permission mode; keep `tools=[]` and `can_use_tool`. If the SDK skips `can_use_tool` in that mode (verify against the installed SDK source and record in RUNBOOK), set `supports_engine_auto=False` instead.
- Prompt rule: use the SDK's slash-command off setting if one exists; else prefix `Message: ` when the first non-whitespace character is `/`; the mirror keeps the original.
- Process report: record the CLI process tree at turn end; report survivors (Task 5).
- [ ] **Step 1: Write failing tests**: `test_plain_turn_events_identical_with_partial_messages` (compare event lists with the flag on vs a recorded baseline); `test_can_use_tool_goes_through_policy_with_todays_name`; `test_structured_output_pass_through`; `test_task_messages_drive_work_signals`; `test_engine_auto_mode_set`; `test_slash_prompt_is_message`.
- [ ] **Steps 2–5:** run → fail → implement → pass → gate && commit `feat(claude): governance mapping`.

### Task 9: Codex mapping

**Files:**
- Modify: `src/tradewind/adapters/codex_backend.py`
- Test: `tests/unit/test_codex_backend.py`, `tests/unit/test_codex_mapping.py`

**Interfaces:**
- Mapping (05 §7): `commandExecution/requestApproval` → `gate(shell, broker_name="shell", broker_args={"command":…})`; `fileChange/requestApproval` → `gate(file_write {changes:[…]}, broker_name="apply_patch", broker_args={"changes":…})`; shim elicitations → `pass_through=True` (gated inside `ToolHost`); other MCP elicitations → `gate(mcp …)`, unreadable tool name → `missing_critical_field=True`. Observe only `item/completed` of command/file items that had no approval request (record / after-the-fact stop). Work signals: `item/started`/`item/completed`; `outputDelta`, `mcpToolCall/progress`, `autoApprovalReview/*`, `terminalInteraction` → `active`; message/reasoning deltas → `model_active`; `turn/completed` → `engine_done()`.
- engine_auto: auto-reviewer setting (confirm the exact `approvals_reviewer` value from the installed SDK) + workspace-write sandbox. No-broker behaviour (read-only sandbox) unchanged.
- Prompt rule: confirm from the app-server source whether `/` is expanded; if not, no change and a test proves `/clear` reaches the model as text; if yes, the prefix rule.
- [ ] **Step 1: Write failing tests**: broker names and args unchanged for shell and patch; `test_ask_rule_on_requested_command_reaches_broker` (never stopped at observe); `test_observe_only_completed_without_request`; non-shim MCP unknown name denied; shim asked once; plain-turn events identical; engine_auto settings; slash behaviour.
- [ ] **Steps 2–5:** run → fail → implement → pass → gate && commit `feat(codex): governance mapping`.

### Task 10: LangChain mapping

**Files:**
- Modify: `src/tradewind/adapters/langchain_backend.py`
- Test: `tests/unit/test_langchain_adapter.py`

**Interfaces:**
- The loop's broker call becomes `ctx.policy.gate(caller_tool …, broker_name=<tool name>, broker_args=<input>)`; the forced structured-output call is not a tool. Stream chunks and the in-turn compaction call → `model_active`; tool execution → `started`/`ended`; final response → `engine_done()`. `permission_mode="engine_auto"` → `Unsupported`.
- [ ] **Step 1: Write failing tests**: broker name unchanged; plain-turn events identical; engine_auto raises `Unsupported`; work signals around tool execution.
- [ ] **Steps 2–5:** run → fail → implement → pass → gate && commit `feat(langchain): governance mapping`.

### Task 11: Capabilities, Cursor and the fake backend

**Files:**
- Modify: `src/tradewind/domain/models.py` (`Capabilities.supports_engine_auto`, `supports_work_supervisor`, `supports_action_policy`, all `bool`), each adapter's `capabilities()`, `cursor_backend.py` (`policy_rules` or `permission_mode` set → `Unsupported`)
- Create: `tests/unit/fake_governed_backend.py`, `tests/unit/test_governance_fake_backend.py`

**Interfaces:**
- Values: Claude `True/True/True` (engine_auto per Task 8 finding); Codex `True/True/True`; LangChain `False/True/True`; Cursor `False/False/False`.
- The fake backend declares `uses_result_tool=True`, floor and default rules, two preflight checks, and drives gate, observe, work signals with and without end signals, and the result tool the way the Antigravity adapter would have (it was stopped) (04 §Turn algorithm).
- [ ] **Step 1: Write failing tests**: capability values per backend; Cursor `Unsupported`; fake backend end-to-end: floor deny, caller tighten, observe stop, unsignalled subagent closes as normal end keeping the structured result, preflight ask/stop, result tool via shim.
- [ ] **Steps 2–5:** run → fail → implement → pass → gate && commit `feat: governance capabilities, Cursor guard and fake backend`.

### Task 12: Documents

**Files:**
- Create: `docs/adr/0004-turn-governance.md`
- Modify: `docs/REQUIREMENTS.md` (new FRs: action policy, permission modes, preflight, prompt rule; FR-4 vocabulary and broker compatibility; FR-6.6 supervisor, optional hard cap, "every backend"; FR-8.1 new flags; FR-2 terminal record without `TurnStarted`), `docs/ARCHITECTURE.md` (§3.1 port additions), `README.md` (capability matrix, engine-auto strength per engine), `docs/RUNBOOK.md` (05 §Notes; Claude engine_auto finding; Codex slash finding), `CHANGELOG.md`
- [ ] **Step 1:** Write the ADR (context, decision = 05, consequences, alternatives: per-backend special rules).
- [ ] **Step 2:** Update the documents; `uv run` any doc checks the repo has.
- [ ] **Step 3:** Bump `version` in `pyproject.toml` from `0.11.2` to `0.12.0` (owner); move the CHANGELOG `[Unreleased]` entries under `## [0.12.0] - <date>`; `uv lock`.
- [ ] **Step 4:** Gate && commit `docs: turn governance ADR and requirements; release 0.12.0`.

### Task 13: Whole-branch review and PR

- [ ] **Step 1:** Dispatch an independent reviewer on the whole branch diff against `05` and this plan's Review Focus.
- [ ] **Step 2:** Fix findings (small ones directly; design-sized ones back to the owner).
- [ ] **Step 3:** Gate && push the branch; open the PR to `main` (body ends with the Claude Code line); link it.

### Task 14: Process report and kill on Claude and Codex (owner ruling B, 2026-11-15)

**Files:** `application/process_report.py` (caller guard; `EngineWatch`), `application/ports.py` (`TurnContext.engine_started` / `engine_closing`), `application/turn_runner.py` (snapshot before every interrupt, 15 s grace, kill on a fired limit or an unanswered interrupt, report before the terminal), `adapters/claude_backend.py`, `adapters/codex_backend.py` (one guarded PID accessor each), fake backend, docs.
- [ ] **Step 1: Write failing tests**: fake-`ps` caller guard (same group, caller's sibling excluded, engine's grandchild included); runner end to end with the fake backend (normal end reports a live descendant without a kill; a plain turn with none alive is unchanged; a fired limit kills the tree and reports before the terminal; interrupt answered vs unanswered); each adapter reports the SDK's PID; SDK attribute pins.
- [ ] **Steps 2–5:** run → fail → implement → pass → gate && commit `feat: process report and kill on Claude and Codex`.
