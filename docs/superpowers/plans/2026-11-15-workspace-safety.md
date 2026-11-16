# Workspace Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `06-workspace-safety.md` for Claude and Codex: scan for workspace customizations and decide per turn, scrub auth-switching variables on subscription profiles, record the engine version on each turn, add an on-demand live safety suite, and release 0.13.0.

**Architecture:**
- One generic scan module. Each backend declares only data: its patterns (`customization_patterns()`) and its auth variables (`auth_env_vars()`).
- The runner checks the caller's decision once, before `begin_turn`. A `delete` happens just before `backend.run()`, and the runner adds the notice after `TurnStarted`.
- One shared helper builds the engine's environment.

**Tech Stack:** Python 3.12+, stdlib `hashlib` / `pathlib` / `shutil`, the existing runner, the existing adapters and the existing models. No new dependency.

**Spec:** `docs/components/06-workspace-safety.md` (approved 2026-11-15, D1–D9). It amends `docs/components/05-turn-governance.md` §4 for this check.

## Global Constraints

- GUIDELINES.md §1–4, §7, §8 (no new global mutable state), §10 (a test that spawns a process is an integration test), §11 and §13.5 bind. No CI change.
- No new runtime dependency.
- Plain turns emit exactly the same events as before. Notices appear only when files are found and a `delete` or `proceed` decision applies.
- Tradewind never stores a decision. `customizations` is a per-call keyword and is never in the session snapshot (D4).
- The `CustomizationDecision.action` values are exactly `"stop"`, `"delete"` and `"proceed"`.
- Exact message texts, quoted from the spec:
  - `"workspace customizations found: <files>; scan and pass a decision"`
  - `"customizations changed since the scan"`
  - `"stopped by the caller: <files>"`
  - the notice `"workspace customizations loaded without broker control: <files>"`
- Backup path: `<customization_backup_dir>/<UTC yyyymmddTHHMMSSZ>-<turn id>/<path relative to root>`, folders mode 0700.
- Auth variables: Claude `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`, `CLAUDE_CODE_USE_ANTHROPIC_AWS`, `CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD`, `CLAUDE_CODE_USE_MANTLE`, `CLAUDE_CODE_USE_GATEWAY`. Codex: `OPENAI_API_KEY`, `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`. Explicit names only; no prefix rule.
- Never run `claude`, `codex` or `agy` CLIs and never run live tests, except in Task 5 Step 3 with the owner's approval. Never read `~/.config/tradewind/live-tests.env`.
- Version (owner): 0.12.0 → 0.13.0, in Task 6 only. Change `uv.lock` by hand; its header stays `revision = 3`.
- Gate: `bash scripts/check.sh`, chained with `&&` to the commit. Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Branch `feat/workspace-safety` (design commits 359ff87, b75d098). One PR to `main`.

## Review Focus

1. **A flagged file that cannot be read** (no permission, or a broken symlink). Expected: `ConfigError` that names the file, never a crash and never a silent skip. Test in Task 1.
2. **A decision is passed, but the files are gone by turn start.** Expected: the turn runs normally (decision-table row "no files"). Test in Task 3.
3. **A `delete` of a flagged symlink that points outside the repo.** Expected: the backup holds the target's content, only the link is removed, and the target is untouched. Test in Task 2.
4. **A `stream()` consumer on a `delete` turn.** Expected: the notice comes right after `TurnStarted` and before any engine event. Test in Task 3.
5. **`cwd` is unset and the process cwd changes between the scan and the turn.** Expected: the fingerprint differs, so `ConfigError("customizations changed since the scan")`. Test in Task 3.

---

### Task 1: The scan

**Files:**
- Create: `src/tradewind/application/workspace_scan.py`
- Modify: `src/tradewind/application/ports.py` (`Backend.customization_patterns()`, default `[]`)
- Modify: `src/tradewind/adapters/claude_backend.py`, `src/tradewind/adapters/codex_backend.py` (declare patterns)
- Test: `tests/unit/test_workspace_scan.py`

**Interfaces:**
- Produces, in `workspace_scan.py`:
  - `CustomizationPattern(path: str, kind: str, is_dir: bool)`, a frozen dataclass. `path` is relative to a scanned folder. For a folder pattern, every regular file below it counts. Symlinked folders are not followed.
  - `FoundFile(path: Path, sha256: str, kind: str)`, a frozen dataclass. `path` is relative to `root`.
  - `CustomizationScan(root: Path, files: tuple[FoundFile, ...], fingerprint: str)`, a frozen dataclass. `fingerprint` is `""` when there are no files.
  - `find_root(folder: Path) -> Path | None`: the nearest folder, starting at `folder` itself, that holds a `.git` file or folder.
  - `scan(folder: Path, patterns: Sequence[CustomizationPattern], backend: str) -> CustomizationScan`.
    - The area is `folder`, the root, and the folders between them. When there is no root, the area is `folder` only.
    - `root` is the repo root, or else `folder`.
    - A file content hash follows symlinks.
    - An unreadable file raises `ConfigError` with its path.
    - Empty patterns → an empty scan, with no file system access.
  - `fingerprint = sha256` over `backend`, `str(root.resolve())` and the sorted `(relative path, sha256)` pairs. The encoding is one line per item, joined with `"\n"`.
- `Backend.customization_patterns() -> list[CustomizationPattern]`:
  - Claude: `.claude/settings.json`, `.claude/settings.local.json`, `.claude/hooks` (folder), `.mcp.json`.
  - Codex: `.codex/config.toml`, but **only if** Step 0 finds a project-local key in Codex 0.147.0 that runs code or loosens the sandbox or approvals. Otherwise `[]`, with a code comment that cites the evidence.
  - LangChain and Cursor: `[]`.

- [ ] **Step 0: Confirm two facts statically** (no live run). Record the evidence in the report.
  - (a) Claude: `tools=[]` reaches the CLI as `--tools ""` (`claude_agent_sdk/_internal/transport/subprocess_cli.py` `_build_command`), and the CLI's help text says this turns off all built-in tools, which includes Skill and Agent.
  - (b) Codex: run `strings` on `.venv/.../codex_cli_bin/bin/codex` and look for the project-local config handling ("Ignored unsupported project-local config keys" and the allowed key list). Decide whether `.codex/config.toml` is flagged.
- [ ] **Step 1: Write the failing tests**
  - `test_scan_area_is_folder_root_and_between`
  - `test_nothing_above_root_is_scanned`
  - `test_no_repo_scans_folder_only`
  - `test_git_file_marks_worktree_root`
  - `test_folder_pattern_counts_every_file_and_skips_symlinked_dirs`
  - `test_symlinked_file_hashes_target_content`
  - `test_unreadable_file_raises_config_error_with_path` (Review Focus 1)
  - `test_fingerprint_stable_and_changes_with_content_root_and_backend`
  - `test_no_files_gives_empty_fingerprint`
  - `test_empty_patterns_touch_no_files` (patch `Path.iterdir` / `os.scandir` to fail)
  - `test_instruction_files_not_flagged` (`CLAUDE.md`, `AGENTS.md`, `.claude/skills/x/SKILL.md`, `.claude/commands/x.md` are present; the scan is empty)
  - `test_backend_patterns` (the Claude list exactly; the Codex list as Step 0 decided; LangChain and Cursor `[]`)
- [ ] **Step 2:** Run `uv run pytest tests/unit/test_workspace_scan.py -q`. Expected: FAIL (import error).
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5:** Gate and commit: `feat: workspace customization scan`.

### Task 2: Decision and delete (pure logic)

**Files:**
- Create: `src/tradewind/application/workspace_decision.py`
- Modify: `src/tradewind/domain/models.py` (`CustomizationDecision`)
- Modify: `src/tradewind/application/config.py` (`TradewindConfig.customization_backup_dir: Path | None = None`)
- Test: `tests/unit/test_workspace_decision.py`

**Interfaces:**
- Consumes: `CustomizationScan` (Task 1).
- Produces:
  - `CustomizationDecision(action: Literal["stop", "delete", "proceed"], fingerprint: str)`, a frozen pydantic model in `domain/models.py`. It is exported from `tradewind`.
  - `DecisionOutcome`, a frozen dataclass with `run: bool`, `notice: str | None`, `delete: tuple[Path, ...]` (absolute paths) and `files_text: str`.
  - `resolve(scan: CustomizationScan, decision: CustomizationDecision | None) -> DecisionOutcome`. It implements every row of the spec's §3 table. A refusal raises `ConfigError` with the exact texts from Global Constraints. `<files>` is a comma-separated list of relative paths.
  - `apply_delete(scan: CustomizationScan, *, backup_dir: Path | None, turn_id: str, now: datetime) -> Path`. It returns the backup folder. Rules:
    - copy everything first, then delete;
    - if any copy fails, nothing is deleted, the backup is kept, and it raises `ConfigError`;
    - a file that is already gone is skipped;
    - a symlink is removed as a link;
    - `backup_dir` is None → `ConfigError`;
    - `backup_dir` inside the scan area (the root, or below it) → `ConfigError`;
    - folders are created with mode 0700;
    - a path outside `scan.root` → `AssertionError`.

- [ ] **Step 1: Write the failing tests**
  - `test_decision_table` (parametrized: one case for each of the 6 rows; assert the exact message or outcome)
  - `test_delete_copies_then_deletes_and_returns_backup`
  - `test_delete_copy_failure_deletes_nothing_and_keeps_backup`
  - `test_delete_skips_gone_file`
  - `test_delete_symlink_backs_up_target_removes_link_only` (Review Focus 3)
  - `test_backup_dir_missing_or_inside_area_raises`
  - `test_backup_folder_mode_0700`
  - `test_decision_model_values` (any other action string is rejected)
- [ ] **Step 2:** Run `uv run pytest tests/unit/test_workspace_decision.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5:** Gate and commit: `feat: workspace customization decision and delete`.

### Task 3: Wiring: Session API and the runner check

**Files:**
- Modify: `src/tradewind/application/client.py`:
  - `Session.scan_customizations() -> CustomizationScan`;
  - a `customizations: CustomizationDecision | None = None` keyword on `run`, `stream` and `spawn` (D9).
- Modify: `src/tradewind/application/turn_runner.py`:
  - the check runs after the existing validations that come before `begin_turn` (near line 652), and before `begin_turn`;
  - the delete runs just before `backend.run()`;
  - the notice comes after `TurnStarted`.
- Test: `tests/unit/test_workspace_runner.py`, using the existing fake backend with patterns set.

**Interfaces:**
- Consumes: `scan`, `resolve`, `apply_delete`, `CustomizationDecision`, `Backend.customization_patterns()`.
- Produces:
  - `customizations` travels like the other per-call overrides, but it is never written to the snapshot.
  - The scan folder rule: `Path(session_row.cwd)` if it is set, else `Path.cwd()`. `scan_customizations()` and the runner use the same rule, through one shared helper.
  - The notice is `ItemCompleted(kind="event")`, using the existing notice helper pattern from `domain/models.py`. After a `delete`, its text names the files and the backup path. After a `proceed`, it uses the exact text from Global Constraints.
  - WARNING logs carry the same facts.

- [ ] **Step 1: Write the failing tests**
  - `test_refusal_leaves_no_turn_row_and_no_prompt`: files present, no decision → `ConfigError`. The store has no new turn row and no mirrored prompt. `backend.prepare_turn` and the reconcile were never called.
  - `test_matching_decision_runs` (for `proceed` and `delete`)
  - `test_files_gone_by_turn_start_runs_normally` (Review Focus 2)
  - `test_cwd_unset_and_process_cwd_changed_raises_changed` (Review Focus 5)
  - `test_stream_delete_notice_right_after_turn_started` (Review Focus 4)
  - `test_plain_turn_without_files_emits_same_events`
  - `test_decision_not_in_snapshot`
  - `test_spawn_passes_customizations_to_child_first_turn`
  - `test_backend_without_patterns_never_scans`
- [ ] **Step 2:** Run `uv run pytest tests/unit/test_workspace_runner.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the same command, then `bash scripts/check.sh`. Expected: PASS. Existing tests are unchanged.
- [ ] **Step 5:** Gate and commit: `feat: scan and decide before a turn starts`.

### Task 4: Auth environment scrub and engine version

**Files:**
- Create: `src/tradewind/application/engine_env.py`
- Modify: `ports.py`:
  - `Backend.auth_env_vars() -> frozenset[str]`, default empty;
  - `Backend.engine_version() -> str | None`, default None.
- Modify: `claude_backend.py` and `codex_backend.py` (declare both; apply the env)
- Modify: `domain/models.py` (`TurnResult.engine_version: str | None = None`)
- Modify: `turn_runner.py` (an INFO log at turn start; set `engine_version` on the `TurnResult` it records)
- Test: `tests/unit/test_engine_env.py`, `tests/integration/test_engine_env_child.py` (it spawns a fake engine binary, so it is an integration test under GUIDELINES §10, but it is not gated: it uses no network and no CLI)

**Interfaces:**
- Produces:
  - `engine_env(profile_auth: object, auth_vars: frozenset[str], base: Mapping[str, str]) -> EngineEnv`. `EngineEnv` holds `removed: tuple[str, ...]`, plus whatever the chosen mechanism needs. It is a no-op unless the profile auth is `SubscriptionAuth`. It logs each removed name at DEBUG, never the value. It never changes `os.environ`.
  - The variable lists, exactly as in Global Constraints.
  - Version sources:
    - Claude: `claude_agent_sdk._cli_version.__cli_version__`.
    - Codex: `importlib.metadata.version("openai-codex-cli-bin")`. This is the bundled app-server package; it is 0.147.0 now, the same as `serverInfo.version`. Ruling: it is cheap and known before spawn, so the runner can log it at turn start.
    - The value is read on each call, with no cache.

- [ ] **Step 0: Choose the removal mechanism and record the evidence.** Neither SDK can delete a variable: Claude builds `{**os.environ, ..., **options.env}`, and Codex does `os.environ.copy()` then `update(env)`. The candidates, in order:
  - (a) An empty value through `options.env` / `CodexConfig.env`. Use it only if the engine treats `""` as unset. For the Claude CLI (JS), `""` is falsy. For Codex (Rust), check `strings` or source evidence that it filters empty values. If it does not, (a) is not allowed for Codex.
  - (b) A wrapper executable. `ClaudeAgentOptions.cli_path` or `CodexConfig.codex_bin` points to a small Tradewind-generated script that runs `env -u VAR ... exec <real binary> "$@"`. Do not use `CodexConfig.launch_args_override`, because it drops `config_overrides`, which carries the MCP shim.
  - Choose per backend. The integration test decides, not the SDK call.
- [ ] **Step 1: Write the failing tests**
  - Unit:
    - `test_subscription_profile_removes_listed_vars`
    - `test_api_key_profile_unchanged`
    - `test_values_never_logged`
    - `test_os_environ_untouched`
    - `test_version_on_turn_result_and_logged_at_turn_start` (with the fake backend)
    - `test_langchain_cursor_have_no_vars_and_no_version`
  - Integration: `test_child_env_has_no_auth_vars_claude` and `..._codex`. Point `cli_path` / `codex_bin` at a fake binary, or at the wrapper target. The fake writes its environment to a file. Assert the variables are absent, or empty if (a) was chosen for that backend, and that `PATH` and an unrelated variable are present.
- [ ] **Step 2:** Run `uv run pytest tests/unit/test_engine_env.py tests/integration/test_engine_env_child.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the same command, then `bash scripts/check.sh`. Expected: PASS.
- [ ] **Step 5:** Gate and commit: `feat: subscription env scrub and engine version per turn`.

### Task 5: On-demand live safety suite

**Files:**
- Create: `tests/safety/__init__.py`, `tests/safety/test_claude_safety.py`, `tests/safety/test_codex_safety.py`
- Move: the two tests from `tests/integration/test_codex_governance_live.py` into `test_codex_safety.py`, and delete the old file.

**Interfaces:**
- The gate is `TRADEWIND_RUN_SAFETY=1`, as a module-level skip. Each test is marked `integration` and wrapped in `anyio.fail_after(180)`.
- Models: `haiku` for Claude, `gpt-5.6-sol` for Codex.
- Each test prints the engine version.
- Each trap has a **control run**. If the control does not show the effect, the test fails with "inconclusive: <why>". It never passes in that case.

- [ ] **Step 1: Write the tests** (the spec's §6 list):
  - Claude:
    - `test_project_hook_runs_on_proceed_not_after_delete`
    - `test_project_permission_allow_skips_can_use_tool` (the control for the flag)
    - `test_env_scrub_keeps_subscription` (the control: no scrub with an invalid `ANTHROPIC_API_KEY`)
    - `test_caller_mcp_tool_reaches_broker`
  - Codex:
    - `test_shell_reaches_broker`
    - `test_file_write_reaches_broker_with_changes`
    - `test_env_scrub_keeps_subscription`
    - `test_project_config_keys_honored` ("inconclusive" when the test folder is not trusted; never edit `~/.codex/config.toml`)
- [ ] **Step 2:** Run `uv run pytest tests/safety -q -rs`. Expected: all skipped.
- [ ] **Step 3: STOP. Ask the owner** before the live run. When the owner approves, run `TRADEWIND_RUN_SAFETY=1 timeout 1500 uv run pytest tests/safety -v -m integration`. Record each result and the engine versions for Task 6's RUNBOOK. A failure is reported, and it is not rerun blindly.
- [ ] **Step 4:** Gate and commit: `test: on-demand live safety suite`.

### Task 6: Documents and release 0.13.0

**Files:**
- Create: `docs/adr/0005-workspace-safety.md` (it amends `05` §4 for this check)
- Modify: `docs/REQUIREMENTS.md` (a new FR for scan and decide; NFR updates for the env scrub and the version record), `docs/ARCHITECTURE.md` (§3.1 ports), `docs/PROJECT.md`, `README.md` (scan and decide, with a Meridian example that keys the stored decision on the commit and the fingerprint; Cursor's `.cursor/` is not checked), `docs/RUNBOOK.md` (Task 5 results; "one checkout per concurrent session"), `CHANGELOG.md`, `docs/components/05-turn-governance.md` (a one-line amendment note in §4), `docs/components/06-workspace-safety.md` (status: implemented)
- Modify: `pyproject.toml`, `src/tradewind/__init__.py` `__version__`, `uv.lock` (0.12.0 → 0.13.0; the lock header stays `revision = 3`)

- [ ] **Step 1:** Write the documents. Claim nothing that Task 5's live run did not show. Name any trap that was inconclusive.
- [ ] **Step 2:** Bump the version. Move CHANGELOG `[Unreleased]` to `## [0.13.0] - <date>`. Confirm `uv lock --check` passes and does not rewrite the file.
- [ ] **Step 3:** Gate and commit: `docs: workspace safety ADR and requirements; release 0.13.0`.
