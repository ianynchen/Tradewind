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

## Task 3 — settings (2 of 20 turns used)

- Settings file: `<gemini-dir>/antigravity-cli/settings.json`; a run before
  the file existed logged "Failed to load CLI settings, using defaults"; runs
  after it did not. Spike settings: `toolPermission: "strict"`,
  `agentMode: ""`, `enableTerminalSandbox: true`,
  `disableSlashCommands: true`, `useAICredits: false`.
- The owner's own setup is `~/.gemini/antigravity-cli/settings.json` and
  `~/.gemini/config/mcp_config.json`; with `--gemini-dir`, neither is read
  (the spike dir has no `config/mcp_config.json`).
- Slash commands (turns 1–2, **inconclusive**): `agy` still advertises 13
  commands (`goal`, `compact`, `swarm`, `teamwork`, `schedule`, …) with
  `disableSlashCommands: true`. `/help` (not a command) and `/compact` both
  produced a normal model answer and no compaction updates, which suggests
  no expansion over ACP (T3 Code notes the same for `/compact`), but this is
  not proven. Tradewind keeps both protections from the design
  (`disableSlashCommands` and the `Message:` prefix).

## Task 4 — permission traps (turns 3–4, all requests answered `allow_once`)

Every approval request offered exactly `allow_once` ("Allow") and
`reject_once` ("Deny"). Each request followed a `tool_call` update with the
same `toolCallId`, `kind: "execute"`, and `rawInput` with `CommandLine`,
`BypassSandbox`, `WaitMsBeforeAsync`, `toolSummary` (and `IsDaemon` for a
long-running shell).

| Trap | Request seen | Action happened | Evidence |
|---|---|---|---|
| Shell command in workspace (`touch marker-shell`) | yes | yes | marker file |
| Write outside workspace, under `/tmp` | yes | yes (temp dir is granted by default) | marker file |
| Read outside workspace, under `/tmp` | yes | yes | secret in the reply |
| Loopback network, in sandbox | yes | **blocked by the sandbox** (exit 7, no canary hit) | canary log empty |
| Non-loopback network, in sandbox | yes | blocked by the sandbox | canary log empty |
| Sandbox escape (`BypassSandbox: true`, loopback curl) | **yes** | yes after allow | canary hit `/escape` |
| Write outside workspace, in `$HOME` | yes | blocked by the sandbox | no marker file |
| Read outside workspace, in `$HOME` | yes | blocked by the sandbox | secret not in the log |
| Background command (`sleep 15 && touch marker-bg`) | yes | yes | marker file |
| Interactive shell start (`bash`, `IsDaemon: true`) | yes | yes | task id |
| **Input into the running shell (`manage_task` `send_input`)** | **no** | **yes** (`touch marker-input` ran) | marker file; `tool_call` kind `other`, title "Run manage_task", **no `session/request_permission`** |
| Scheduled follow-up (`schedule`, 15 s) | no | yes (the agent scheduled its own follow-up prompt) | `tool_call` "Run schedule" |

The sandbox message for a blocked command says: "you can run the command
again with `BypassSandbox` set to true to request explicit user permission".

**Risky path found (gate rule: stop and report to the owner).** Input sent
to an already-approved interactive shell runs **without any approval
request**. In this run the shell was inside the sandbox, so the input was
contained (workspace writes, no network). But if the approved shell had
been started with `BypassSandbox: true`, every later input line (e.g.
`git push`, a deploy command) would run outside the sandbox with no
request reaching the broker. The `schedule` tool also runs without a
request; it schedules a later prompt in the same session, whose own tool
calls then ask as usual.

Turns used: 4 of 20. The spike stopped here for the owner's decision.

### Owner ruling and follow-up probes (turns 5–7)

The owner first chose to deny escapes for interactive shells
(`IsDaemon: true` + `BypassSandbox: true`). That does not close the path:
any long-running command (e.g. a background `sh`) can receive input
through `manage_task` too. The owner then chose **(d): no escapes at all;
network through the sandbox's allow list**, subject to this test.

| Turn | Probe | Result |
|---|---|---|
| 5 | `session/new` `_meta.antigravity.disabledTools: ["manage_task"]`, then start `bash` and send input | **Ignored by agy 1.5.9.** `manage_task send_input` still ran (`marker-input2` created). No switch removes the input tool. |
| 6 | `toolPermission: "strict"` + `permissions.allow: ["read_url(example.com)"]`, sandboxed `curl https://example.com` and `https://www.iana.org` | Both asked; **both blocked** (exit 6, host not resolved). Matches the docs: "Strict mode automatically turns on sandboxing with network access denied." |
| 7 | Same with `toolPermission: "request-review"` | Every command still asked (good), but **both curls blocked again**, and `touch marker-rr` in the workspace now **failed** with a sandbox error (it succeeded in turns 3–4 without a `permissions` block). |

Documentation read (antigravity.google/docs/permissions, /sandbox,
/settings, /cli/reference, schemas/v1/settings.json): rules are
`permissions.{allow,ask,deny}` with `command(...)`, `read_file(...)`,
`write_file(...)`, `read_url(...)`, `execute_url(...)`, `mcp(...)`,
`unsandboxed(...)` (business/enterprise only); "Domains allowed under
`read_url` are added to the sandbox's outbound allowlist"; `toolPermission`
values `request-review` (default), `proceed-in-sandbox`, `strict`,
`always-proceed`.

**Result for (d):** on agy 1.5.9 with a personal Google AI Pro account over
ACP, the documented sandbox allow list did **not** give sandboxed commands
network access in either permission level tried, and a `permissions`
block also changed workspace write access. (d) as designed could not be
shown to work. Without an allow list, (d) means: no network from the
agent's own shell at all. Settings were restored to the turn 3–4 state.
Turns used: 7 of 20. Stopped for the owner's decision.

## Task 5 — MCP shim (turns 8–10) and built-in network tools (turns 11–12)

**Owner ruling d1 (2026-11-15):** the agent's shell is offline (strict,
sandbox on, every escape denied by Tradewind in broker mode); network work
goes through broker-checked MCP tools in the shim.

- Turns 8–9 were lost to a **harness bug** (the probe MCP server used the
  mcp 1.x `FastMCP` import; the installed `mcp` is 2.x, so the server
  crashed at start). Not an `agy` finding. Turn 8 still listed `agy`'s
  built-in tools: `read_file`, `edit_file`, `write_file`, `run_command`,
  `manage_task`, `send_message`, `invoke_subagent`, `define_subagent`,
  `manage_subagents`, `schedule`, `run_workflow`, `swarm`,
  `ask_question`, `search_web`.
- Turn 10 (fixed server, 8 s wait after `session/new`):
  - **The shim runs outside `agy`'s sandbox:** the MCP tool's own HTTP call
    reached the loopback canary (`/mcp`). Network via MCP works under
    strict.
  - **`agy` asks for MCP tools too:** `session/request_permission` with
    `allow_once`/`reject_once` for each MCP call.
  - **Visible tool name** is `mcp_<server name>_<tool>`, e.g.
    `mcp_tradewind-7f3a91c2_probe_echo`; the `tool_call` `kind` is `other`,
    the title `Run mcp_<server>_<tool>`.
  - **No `_meta.is_mcp_tool_call`** and no server field in `rawInput` on
    agy 1.5.9 (design C2 assumed them). Identification must use the exact
    per-turn prefix `mcp_<random shim name>_` in the tool name.
  - **Startup race:** `agy` logs "Preloading 1 MCP servers" at
    `session/new`, but the tool list is taken at the prompt; a prompt sent
    before the server is ready sees no MCP tools. Tradewind must wait until
    the shim has connected (and listed its tools) before `session/prompt`.
- Turn 11: **`search_web` ran with no approval request** (a server-side
  search; the query leaves the machine to Google, which already has the
  conversation). **`invoke_subagent` ran with no approval request**; the
  subagents run in the background and `session/prompt` returned `end_turn`
  while they were still starting.
- Turn 12 (agy kept alive 90 s after `end_turn`): each subagent's own
  transcript (`brain/<subagent id>/.system_generated/logs/transcript*.jsonl`)
  shows a planned `run_command touch marker-sub` and **nothing after it**:
  the command never ran, and no approval request reached the client in 90 s.
  The browser subagent produced no steps; the canary got no hit.
  **Result:** subagents do not bypass approval over ACP; they stall. In
  broker mode they cannot do approved work, and a stalled subagent must not
  hold the M10 idle wait open.

Turns used: 12 of 20.
