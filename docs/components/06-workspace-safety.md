# Component: Workspace Safety (customizations, auth environment, engine version, safety suite)

Status: approved by the owner (2026-11-15), with decisions D1–D9. It was
revised after a first-principles adversarial review. Implemented in
0.13.0 (2026-11-15; ADR-0005). Live results: RUNBOOK, "Workspace safety
suite".

## Purpose

The stopped Antigravity design (`04`) found risks that are not specific to
Antigravity. This component applies those findings to the backends that
Tradewind ships: Claude, Codex and LangChain. The work-supervisor parts of
`04` (time limits, activity, force stop, process report) are already in
`05` and 0.12.0. They are not repeated here.

### Why

Meridian runs Claude and Codex in repositories that other people change,
for example a pull request branch. Such a repository can carry agent
configuration that the engine loads, and that configuration can run code
or loosen permissions with no broker question.

Example: a PR branch adds `.claude/settings.json` with a `SessionStart`
hook that runs `.claude/hooks/x.sh`. Tradewind's Claude adapter does not
set `setting_sources`. The bundled CLI therefore loads all sources,
including project settings. The `claude_agent_sdk` `types.py` docstring for
`setting_sources` says: "When `None`, all sources are loaded". In
non-interactive mode, the CLI skips the trust check for hooks. The script
runs before the broker sees anything.

### Scope rule (owner)

The protections must not leave Tradewind worse than the owner's own direct
`claude` or `codex` use. The owner accepts the risk of an engine upgrade
that the owner chooses. Tradewind does not remember decisions. The caller
(Meridian) remembers and decides.

## Goals, non-goals, anti-goals

- Goals:
  - The caller can find workspace configuration that can run code or
    loosen permissions, before a turn, and decide what happens to it.
  - A subscription profile cannot switch to another auth or provider by an
    environment variable.
  - Each turn records which engine version ran.
  - An on-demand live suite shows that the engine-dependent facts still
    hold after an upgrade.
- Non-goals: failover (deferred by the owner); Cursor (experimental, no
  governance features); flagging instruction files; a verified-version
  record; any automatic live run; Tradewind changing an engine's own trust
  settings.
- Anti-goals:
  - A turn that loads flagged workspace configuration with no decision
    from the caller.
  - Tradewind storing or remembering a decision.
  - A turn blocked because an engine version is new.
  - A subscription turn billed to an API key or another provider.

## Decisions (owner, 2026-11-15)

| # | Decision |
|---|---|
| D1 | Detect, then decide: one generic mechanism; each backend declares what to scan for (owner chose detect and decide over always-isolate) |
| D2 | Detection is a function call. The decision is a parameter of the turn call: `stop`, `delete` or `proceed`. No callback. Tradewind continues with what it is told. |
| D3 | Tradewind re-scans at turn start and compares with the caller's fingerprint. If it is different, or files are found with no decision, the turn stops. |
| D4 | Tradewind never remembers a decision. Meridian remembers and passes it. |
| D5 | Text is not flagged: instruction files (`CLAUDE.md`, `AGENTS.md`). The same reason covers skill, agent and command files whose actions go through the broker anyway (see §1). |
| D6 | Env scrub only for subscription profiles. API-key profiles keep their variables. |
| D7 | No version block, no warning gate, no automatic suite. The engine version is recorded on each turn for debugging. The safety suite runs only on demand. |
| D9 | `spawn()` gets a `customizations=` keyword, so the caller passes the decision for the child's first turn explicitly. The child never takes the parent's decision. |
| D8 | LangChain gets none of the engine parts: it loads no workspace files, an API key is its intended auth, and its library versions are pinned. |

## Approaches considered

| | Approach | Verdict |
|---|---|---|
| Customizations | A detect and decide / B always isolate (`setting_sources=["user"]`, isolated `CODEX_HOME`) / C both | **A** (owner). B drops the project `CLAUDE.md`, needs a second Codex login and loses the owner's Codex trust list and MCP servers. C is two mechanisms for one problem. |
| Where the decision lives | callback (`on_preflight`) / Tradewind memory / caller passes it with the turn | **Caller passes it** (owner): explicit, stateless, testable |
| Version safety | A automatic suite / B warn / C block | **None of these** (owner): record the version, and run the suite on demand only. A block adds nothing for a risk the owner chose, and it fails pipelines at a time the owner did not choose. |

## Design

### 1. What is flagged (security triage per engine)

Flag only what can run code or loosen permissions **without** going
through the broker. These are the flagged patterns, relative to each
scanned folder:

| Backend | Pattern | Why it is flagged |
|---|---|---|
| Claude | `.claude/settings.json`, `.claude/settings.local.json` | hooks (run code; non-interactive mode skips hook trust; shown live on CLI 2.1.258); MCP and plugin enablement. A `permissions.allow` rule is **not** a reason: live on CLI 2.1.258 a project allow rule did not bypass `can_use_tool`. A guard test in the safety suite watches for a future CLI that does. |
| Claude | `.claude/hooks/` (every file, recursively) | the scripts that hooks run; the bundled CLI protects this folder next to `settings.json` |
| Claude | `.mcp.json` | project MCP servers (commands that the engine starts) |
| Codex | `.codex/config.toml` | project config in a trusted repository. Flagged as a fail-closed ruling (the binary does not list the honored keys). The live run then confirmed that project `mcp_servers` are honored in a trusted folder (they start commands), so the flag is needed. |
| Codex | `.codex/rules/` (every file, recursively) | exec policies (`.rules` files, e.g. `default.rules`, in a `rules` folder of each config layer; the 0.147.0 binary: "Trusting the directory allows project-local config, hooks, and exec policies to load"). An exec-policy allow rule can let an escalated command run without an approval request. Flagged as a fail-closed ruling (final review). |

These are **not flagged**, after triage:

| What | Why it is a non-issue |
|---|---|
| `CLAUDE.md`, `AGENTS.md` | text (D5) |
| Claude `.claude/skills/`, `.claude/agents/`, `.claude/commands/`; plugin folders | built-in tools are off (`tools=[]` → `--tools ""`), so Skill and Agent cannot run. The prompt rule blocks slash commands. Plugins load only when a settings file enables them, and settings files are flagged. The absence of the Skill tool is verified in the bundle only; not checked live. |
| Codex `.agents/skills/` | text and scripts. A script runs only through Codex's shell, and the broker gates that shell. |
| Codex hooks | Codex runs a hook only after its hash is trusted ("New hook - review required"). The engine's gate is stronger than a scan. |

**What `proceed` covers.** The fingerprint covers the flagged files only.
A flagged file can point to other files. Example: an MCP command
`node scripts/server.js`, outside `.claude/hooks/`. `proceed` therefore
trusts the checkout as it is. Meridian should key its stored decision on
the commit or tree, not only on the fingerprint. Tradewind does not parse
command lines.

### 2. Scan API and the fingerprint

```python
scan = session.scan_customizations()
#   scan.root: Path               # the base for relative paths: the repo root, else the scanned folder
#   scan.files: list[FoundFile]   # FoundFile(path: Path relative to root, sha256: str, kind: str)
#   scan.fingerprint: str         # "" when no files
decision = CustomizationDecision(action="delete", fingerprint=scan.fingerprint)
result = await session.run(prompt, customizations=decision)
```

- **One folder.** The scan uses the engine's working folder: the session's
  `cwd`, or the process cwd when `cwd` is unset. `scan_customizations()`
  and the runner use the same rule, so they give the same fingerprint.
- **Scan area:** the working folder, the repository root, and every folder
  between them. A `.git` **file or folder** marks the root, so worktrees
  and submodules work. When there is no repository, the area is the
  working folder only. Nothing above the root is scanned. The folders
  above the root belong to the owner, so they are no worse than direct use.
- **Patterns:** each backend declares them. The method is
  `Backend.customization_patterns() -> list[CustomizationPattern]`, default
  `[]`. When a backend has no patterns (LangChain, Cursor), no scan runs
  and the fingerprint is `""`. The docs state that Cursor loads `.cursor/`
  config and that Tradewind does not check it.
- **Fingerprint:** sha256 over the backend name, the absolute root and
  the sorted `(relative path, content sha256)` pairs.
  - The hash is of the content the engine reads, so a symlink is followed.
  - Symlinked folders are not followed when the scan walks a folder
    pattern.
  - Two repositories with the same files do not share a fingerprint.

### 3. The decision at turn start

`CustomizationDecision(action: Literal["stop", "delete", "proceed"], fingerprint: str)`
is a per-turn keyword of `run`, `stream` and `spawn` (owner, D9). It
is never stored in the session snapshot (D4).

**Where the check runs:** in the runner, once, **before** `begin_turn`,
before the native reconcile, and before anything spawns an engine. This
amends `05` §4: this check is a runner check, not a backend `prepare_turn`
check, and it does not use the `on_preflight` callback. A refused turn
raises `ConfigError` and leaves no turn row and no mirrored prompt. A
`delete` happens just before `backend.run()`. The runner emits the notice
right after the backend's `TurnStarted`.

| Scan result | Decision passed | Outcome |
|---|---|---|
| no files | any or none | the turn runs |
| files | none | `ConfigError("workspace customizations found: <files>; scan and pass a decision")` |
| files | fingerprint differs | `ConfigError("customizations changed since the scan")` |
| files | `stop` | `ConfigError("stopped by the caller: <files>")` |
| files | `delete` | back up, then delete exactly the scanned files; WARNING log; notice after `TurnStarted` with the files and the backup path; the turn runs |
| files | `proceed` | WARNING log; notice after `TurnStarted`: "workspace customizations loaded without broker control: <files>"; the turn runs |

Delete rules (as in `04` C1, owner "delete means delete"):
- Copy each file to `<backup_dir>/<UTC yyyymmddTHHMMSSZ>-<turn id>/<path relative to root>`.
- Delete only after every copy succeeds. If any copy fails, nothing is
  deleted, the backup folder is kept, and the turn raises `ConfigError`.
- A file that is already gone is skipped. A symlink is removed as a link.
  There is no restore. Tracked files show as deletions in git, and that
  is the caller's responsibility.
- `backup_dir` is a new `TradewindConfig.customization_backup_dir: Path | None`.
  - `delete` without it → `ConfigError`. There is no hidden default.
  - A `backup_dir` inside the scan area → `ConfigError`.
  - Backup folders are created with mode 0700, because
    `settings.local.json` can hold secrets.

Race between the scan and the engine start: this is a non-issue. Only the
owner's own processes can write into that window, and direct use has the
same exposure. The docs say "one checkout per concurrent session". No
mechanism is added.

### 4. Auth environment scrub

Rule: with a subscription profile, the engine process gets the caller's
environment minus **every variable that selects a non-subscription auth or
provider**. Each backend declares its list, in the same shape as the
patterns: `Backend.auth_env_vars() -> frozenset[str]`, default empty. One
shared helper builds the child environment.

- **Claude** (CLI 2.1.258):
  - `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`;
  - `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`,
    `CLAUDE_CODE_USE_FOUNDRY`, `CLAUDE_CODE_USE_ANTHROPIC_AWS`,
    `CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD`, `CLAUDE_CODE_USE_MANTLE`,
    `CLAUDE_CODE_USE_GATEWAY`.
  - Explicit names only. There is no `CLAUDE_CODE_USE_*` prefix rule,
    because that prefix also matches feature switches.
- **Codex:** `OPENAI_API_KEY`, `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`.

API-key profiles are unchanged (D6). Each removal is logged at DEBUG by
name, never by value. The caller's own `os.environ` is never changed.
Neither SDK can remove a variable:
- Claude builds the env as `{**os.environ, …, **options.env}`.
- Codex does `os.environ.copy()` and then `update(config.env)`.

**As built:** the mechanism is an empty value (`NAME=""`) for both
engines. Both treat an empty value as unset (Claude: truthy and `trim() ||
void 0` checks; Codex: `read_non_empty_env_var`). A process that the engine
starts (a hook, an MCP server, a shell command) therefore sees `NAME=""`,
not a missing variable. This is a consequence of the mechanism, not a
live result. The live suite confirmed that a subscription turn
completes with the scrub and fails without it (RUNBOOK). Evidence:
`.superpowers/sdd/2026-11-15-workspace-safety/task-4-report.md`.

### 5. Engine version on each turn

- Claude: `claude_agent_sdk._cli_version.__cli_version__`. The adapter
  uses the bundled CLI.
- Codex: `importlib.metadata.version("openai-codex-cli-bin")`. **Ruling
  (replaces `serverInfo.version` from the `initialize` reply):** the
  package version is known before the engine starts, so the runner can log
  it at turn start as required below. Both gave 0.147.0.
- Read on each turn, with no cache (GUIDELINES §8). Logged at INFO **at
  turn start**, with the turn and session ids, so failed turns have it too.
  Also set on `TurnResult.engine_version: str | None` (`None` for LangChain
  and Cursor).
- No event is added, so plain turns keep the same events. The builder
  checks that no test compares a whole `TurnResult` for equality.

### 6. On-demand live safety suite

`tests/safety/`, with env gate `TRADEWIND_RUN_SAFETY=1`, marked
`integration`. It is never in `check.sh` or CI, and it runs only when the
owner starts it. Each test has a 3-minute cap and uses the cheapest model.

Live traps test only **facts that can change with an engine upgrade**.
Tradewind's own branches (stop, delete, fingerprint) are unit-tested. Each
trap has a **control run** that shows the effect without the protection.
If the control cannot show the effect, the trap is "inconclusive", not a
pass.

- **Claude:**
  - A project hook in `.claude/settings.json` runs on a `proceed` turn (the
    control: the marker is present). After `delete`, it does not run. This
    proves that the flagged file is really loaded.
  - A project `permissions.allow` rule must not make a caller MCP tool
    skip `can_use_tool`. Live result: it does not (CLI 2.1.258), so the
    test is a regression guard that fails if a future CLI bypasses the
    broker.
  - The env scrub: with a deliberately invalid `ANTHROPIC_API_KEY` and no
    scrub, the turn fails or reports API-key auth (the control). With the
    scrub, the turn succeeds on the subscription.
  - A caller MCP tool reaches the broker.
- **Codex:**
  - A shell command reaches the broker as `shell`, and a file write as
    `file_write` (PR #17 moves here).
  - The env scrub, with the same control pattern.
  - Whether project `.codex/config.toml` keys are honored. This decides
    the Codex pattern in §1, and it needs no change to the owner's trust
    settings when the test folder is already trusted. Otherwise it is
    "inconclusive". Result (2026-11-15, Codex 0.147.0): honored: yes;
    project `mcp_servers` started in a trusted folder.
- Each run prints the engine versions. The results go in RUNBOOK by hand.

## Interface changes (one ADR; amends `05` §4)

- `Session.scan_customizations()`, `CustomizationScan`, `FoundFile`,
  `CustomizationDecision`.
- The `customizations` keyword on `run`, `stream` and `spawn` (D9).
- `Backend.customization_patterns()`, `Backend.auth_env_vars()`.
- `TradewindConfig.customization_backup_dir`.
- `TurnResult.engine_version`.
- REQUIREMENTS: a new FR for scan and decide, and NFR updates for the env
  scrub and the version record.
- Docs: ARCHITECTURE, PROJECT, README (scan and decide, with a Meridian
  example), RUNBOOK, CHANGELOG.
- Release 0.13.0 (owner rule: minor bump after implementation).

## Workspace cases (owner)

Meridian's working folders can be anything: a repo root or a subfolder;
a clone, worktree or submodule; trusted by the engines or not. Meridian
narrows this down later. The design handles every case.

## Resolved in 0.13.0 (see ADR-0005 and RUNBOOK)

- Codex project-local keys: project `mcp_servers` are honored in a trusted
  folder (live), so `.codex/config.toml` stays flagged.
- Env removal mechanism: an empty value for both engines; an integration
  test inspects the child's environment (`tests/integration/test_engine_env_child.py`)
  and the live suite shows a subscription turn working with the scrub.
- `delete` never touches a path outside the scan area: a symlinked pattern
  folder that points outside the root raises `ConfigError` before any copy.
- Out of scope, still true (report separately): the Claude and Codex
  backends do not pass `ApiKeyAuth.api_key` to the engine, so API-key
  profiles depend on the ambient environment.

Open: that the Claude Skill tool is absent with `tools=[]` was verified in
the bundle only; it was not checked live.

## Acceptance

- Unit tests:
  - scan area: cwd, root, between; nothing above; no repository; a
    worktree with a `.git` file;
  - each backend's patterns, and the non-flagged list;
  - the fingerprint: stable; changes with content, root or backend;
    symlink rules;
  - every row of the decision table, with no turn row and no prompt
    mirrored on a refusal;
  - delete: copy failure deletes nothing; a gone file is skipped;
    `backup_dir` missing or inside the area → error; mode 0700;
  - the decision is not in the session snapshot;
  - no scan for backends without patterns;
  - the version is logged at turn start and set on `TurnResult`;
  - LangChain and Cursor are unchanged.
- Integration test: the child's environment, for subscription versus
  API-key profiles.
- The live safety suite is written. The owner runs it once before release,
  and the results go in RUNBOOK.
