---
id: "ADR-0005"
title: "Workspace safety: scan and decide, subscription env scrub, engine version record"
status: accepted
date: 2026-11-15
deciders: [human:ianynchen]
---

# ADR-0005: Workspace safety: scan and decide, subscription env scrub, engine version record

Date: 2026-11-15 · Status: Accepted · Spec: `docs/components/06-workspace-safety.md` · Amends: `05-turn-governance.md` §4 (for the customization check)

## Context

Meridian runs Claude and Codex in repositories that other people change,
for example a pull request branch. Such a repository can carry agent
configuration that the engine loads and that runs code or loosens
permissions with no broker question. Example: a branch adds
`.claude/settings.json` with a `SessionStart` hook; in non-interactive
mode the CLI skips the hook trust check, so the script runs before the
broker sees anything. Two related risks: an environment variable can switch
a subscription profile to an API key or another provider, and nothing
recorded which engine version ran a turn. The stopped Antigravity design
(`04`) had found these risks. They are not specific to one engine.

## Decision

1. **Detect, then decide (D1, D2).** Each backend declares what to flag
   (`Backend.customization_patterns()`). `session.scan_customizations()`
   returns the files and a fingerprint. The caller passes a
   `CustomizationDecision(action="stop" | "delete" | "proceed", fingerprint=...)` as the
   `customizations=` keyword of `run`, `stream` and `spawn` (D9). Tradewind
   re-scans at turn start. Files with no decision, or a different
   fingerprint, raise `ConfigError`. `delete` backs files up (to
   `TradewindConfig.customization_backup_dir`, mode 0700) and then deletes
   exactly the scanned files. `proceed` loads them and emits a notice.
2. **The check is a runner check (amends `05` §4).** It runs once, before
   `begin_turn` and before any engine starts, so a refused turn leaves no
   turn row and no mirrored prompt. It is not a `prepare_turn` check and
   does not use `on_preflight`: a preflight `Ask` is a callback and
   Tradewind must not hold a decision (D4).
3. **Tradewind never stores a decision (D4).** Meridian keeps it and passes
   it. It is a per-call keyword and is not in the session snapshot.
4. **Env scrub for subscription profiles only (D6).** Each backend declares
   `Backend.auth_env_vars()`. The engine gets the caller's environment with
   those names set to the empty value. Both engines treat an empty value as
   unset. A process the engine starts therefore sees `NAME=""`
   (a consequence of the mechanism, not a live result). API-key profiles are
   unchanged. `os.environ` is never changed.
5. **Engine version on each turn (D7).** `Backend.engine_version()` is read
   on each turn, logged at INFO at turn start, and set on
   `TurnResult.engine_version`. Claude: the bundled CLI version. Codex: the
   `openai-codex-cli-bin` package version. No block, no warning gate.
6. **On-demand live safety suite (D7).** `tests/safety/`, gate
   `TRADEWIND_RUN_SAFETY=1`. Never in `check.sh` or CI. Results go in
   RUNBOOK.

Flagged files: Claude `.claude/settings.json`, `.claude/settings.local.json`,
`.claude/hooks/**`, `.mcp.json`; Codex `.codex/config.toml` and `.codex/rules/**`
(exec policies: an allow rule can let an escalated command run without an
approval request; flagged fail-closed). Not flagged:
instruction files and skill/agent/command folders (D5; their actions go
through the broker), and Codex hooks (the engine's own hash trust is
stronger).

## Consequences

- Behaviour changes (all in CHANGELOG 0.13.0): on Claude and Codex a turn
  in a folder with flagged files raises `ConfigError` unless the caller
  passes a matching decision; subscription profiles have the auth
  variables blanked; a backend-factory error now raises before a turn row
  exists.
- `proceed` trusts the checkout as it is. The fingerprint covers the
  flagged files only, and a flagged file can point to others (an MCP
  command `node scripts/server.js`). Meridian should key its stored
  decision on the commit or tree plus the fingerprint. Tradewind does not
  parse command lines.
- Cursor loads `.cursor/` config and Tradewind does not check it
  (experimental backend, no governance features). LangChain loads no
  workspace files (D8).
- Live facts (2026-11-15, Claude CLI 2.1.258, Codex 0.147.0): a project
  hook runs on `proceed` and not after `delete`; a project
  `permissions.allow` rule did not bypass `can_use_tool` (a guard test
  watches it; `.claude/settings.json` stays flagged because of hooks);
  Codex project `mcp_servers` are honored in a trusted folder; the scrub
  keeps a subscription turn working while an invalid key breaks the
  control turn, on both engines.
- The scan area is the working folder up to the repository root. Folders
  above the root belong to the owner. Concurrent sessions need one
  checkout each: a write between scan and engine start is the owner's own
  exposure, and no mechanism guards it.
- No new runtime dependency.

## Alternatives rejected

- **Always isolate (`setting_sources=["user"]`, isolated `CODEX_HOME`).**
  Drops the project `CLAUDE.md`, needs a second Codex login, and loses the
  owner's Codex trust list and MCP servers.
- **Both detect-and-decide and isolate.** Two mechanisms for one problem.
- **A decision callback (`on_preflight`) or a decision stored by
  Tradewind.** A callback is implicit and hard to test; stored state
  breaks D4.
- **Block or warn on a new engine version, or run the suite automatically.**
  The owner accepts the risk of an upgrade the owner chooses. A block fails
  pipelines at a time the owner did not choose.
- **A launch wrapper to remove variables (`cli_path`, `codex_bin`).** The
  empty value works on both engines. A Codex wrapper would also drop the
  bundled `codex-path/` from PATH.
