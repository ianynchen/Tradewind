# Spike: Antigravity over ACP — go/no-go (design 04)

Date: 2026-11-15. Plan: `docs/superpowers/plans/2026-11-15-antigravity-acp-spike.md`.
Design: `docs/components/04-antigravity-adapter.md`. Machine: the owner's
macOS laptop. `agy --version`: **1.5.9** (the owner updated from 1.5.3 before
the spike). Budget: at most 20 prompt turns, stop on the first quota error.
Not production code: the probe harness lives in the scratch directory
`…/scratchpad/agy-spike` (never in Tradewind's tree); this document is the
durable artifact.

Spike profile: `agy acp --gemini-dir <scratch>/agy-spike/gd-spike
--disable-telemetry`, logged in by the owner with
`agy acp --login --gemini-dir <that dir>`.

## Task 1 — harness

`acp_probe.py` (scratch): spawns `agy acp` in a new session (process group),
sends `initialize` (protocol 1; `fs` read/write false; `terminal` false),
`authenticate {methodId: "oauth-personal"}`, `session/new` or
`session/resume`, optional `session/set_config_option`, optional
`session/prompt`; logs every JSON-RPC message as NDJSON; answers permission
requests with `allow_once`, `reject_once` or `cancelled`; counts prompts
against the 20-turn budget.

## Task 2 — static facts (no prompts)

### Capabilities (`initialize`, `session/new`, agy 1.5.9)

- `agentInfo`: `Antigravity ACP Server` 1.5.9. Reply `protocolVersion`: 1.
- `agentCapabilities`: `loadSession`; prompt `image`, `embeddedContext`
  (no audio); `sessionCapabilities`: `resume`, **`close`**, `load`, `list`,
  `additionalDirectories`; `auth.logout`; `_meta.antigravity`:
  `tier: true`, `systemPrompt.append: true`.
- `authenticate` result: `_meta.antigravity.tier` = `Google AI Pro`
  (`g1-pro-tier`).
- Modes: **`default`, `accept-edits`, `auto`, `plan`** — Auto over ACP is
  available on 1.5.9 (it was not on 1.5.3). Engine-auto is therefore
  possible.
- Config options: `model` (`gemini-4-argon`, `gemini-4-hydrogen`,
  `gemini-pro-orchestrator-g1-pro`, `gemini-3.8-flash`, `claude-opus-5-5`,
  `claude-sonnet-5-5`, `gpt-oss-120b-medium`), `thought_level`
  (`low`/`medium`/`high`), `mode` (as above).

### Login token (design M1)

- The login wrote no token file into the spike's gemini-dir; it wrote only
  `antigravity-cli/cache/acp-auth-state.json` (one field, `methodId`). The
  login log says `consumerOAuth: token acquired`.
- The macOS keychain has exactly one Antigravity item: service `gemini`,
  account `antigravity` (names only; the value was not read).
- **Result:** the token is in the shared login keychain, so Tradewind's
  profile and the owner's interactive `agy` share one login. This is the
  fallback the owner accepted in M1. `--gemini-dir` still separates
  settings and conversation data (`antigravity-cli/` under the given dir).

### Settings and switches (from the binary's JSON field tags and the changelog)

| Purpose | Name | Evidence |
|---|---|---|
| Tool permission level | `toolPermission` (value `strict` exists) | field tag `toolPermission,omitempty`; changelog: "A `Tool Permission` setting of `strict` now also takes precedence over Auto and accept-edits modes" |
| Agent mode default | `agentMode` (`""` = Default) | changelog: "saves `\"agentMode\": \"\"` to `settings.json`"; the default became Auto in 1.5.7 — Tradewind must set the mode explicitly every session |
| Terminal sandbox | `enableTerminalSandbox` | field tag |
| Artifact review | `artifactReview` / `artifactReviewMode` (`ALWAYS`, `AUTO`, `TURBO`) | field tags; enum `ARTIFACT_REVIEW_MODE_*` |
| Slash commands | `disableSlashCommands` | field tag |
| Built-in tools | `disableBuiltInTools` | field tag (not used: Tradewind needs them) |
| Default customizations | `disable_default_customizations`; flag `--include-default-customizations` | field tag; `agy acp --help` |
| AI credits | `useAICredits` (also `enableAiCredits`) | symbols `UseAICredits`, `useAICreditsOverride` |
| Auto-update off | env `AGY_CLI_DISABLE_AUTO_UPDATE` | binary string |
| Browser | `browserEnabled`, `browser_js_execution_policy` | field tags |

Open for Task 3: the exact settings-file path under `--gemini-dir`, and that
each key is honoured over ACP.

### Environment variables seen in the binary (auth-related, to verify in Task 3)

`GEMINI_API_KEY` (T3 Code scrubs it), `GOOGLE_API_KEY`,
`GOOGLE_APPLICATION_CREDENTIALS`, `ANTIGRAVITY_APP_DATA_DIR`,
`ANTIGRAVITY_CREDENTIAL_SCOPE`.
