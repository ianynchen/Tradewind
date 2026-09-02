# Agent backend research: claude-agent-sdk, openai-codex, cursor-sdk, langchain-anthropic

Date: 2026-09-01. Researched against primary sources only: official docs and SDK source. Each claim carries a citation. Undocumented/unverified items are collected at the end.

Sources used repeatedly (abbreviated in citations):

- **[CC-PY]** https://code.claude.com/docs/en/agent-sdk/python (Python SDK reference)
- **[CC-SUB]** https://code.claude.com/docs/en/agent-sdk/subagents
- **[CX-API]** `sdk/python/src/openai_codex/api.py` in github.com/openai/codex (local copy: `codex_api.py` in session scratchpad)
- **[CX-CLIENT]** `sdk/python/src/openai_codex/client.py` (local copy: `codex_client.py`)
- **[CX-MODELS]** `sdk/python/src/openai_codex/generated/v2_all.py` (fetched from raw.githubusercontent.com/openai/codex/main)
- **[CX-NOTIF]** `sdk/python/src/openai_codex/generated/notification_registry.py`
- **[CX-REF]** https://github.com/openai/codex/blob/main/sdk/python/docs/api-reference.md
- **[CX-MCP]** https://learn.chatgpt.com/docs/extend/mcp?surface=cli (developers.openai.com/codex/mcp redirects here)
- **[CU-PY]** https://cursor.com/docs/sdk/python
- **[CU-SUB]** https://cursor.com/docs/subagents
- **[LC-ANTH]** https://docs.langchain.com/oss/python/integrations/chat/anthropic (python.langchain.com 308-redirects here)
- **[LC-MCP]** https://github.com/langchain-ai/langchain-mcp-adapters (README)
- **[LOCAL-CC]** live transcripts at `~/.claude/projects/<encoded-cwd>/*.jsonl` (this machine, Claude Code v2.1.247–2.1.255)
- **[LOCAL-CX]** live rollouts at `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (this machine, Codex CLI 0.148.0-alpha.21)

## Summary table

| Capability | Claude Agent SDK | Codex Python SDK | Cursor Python SDK | langchain-anthropic (raw API) |
|---|---|---|---|---|
| Call shape | `query(prompt, options)` async iter, or `ClaudeSDKClient.query()` + `receive_response()` [CC-PY] | `Codex().thread_start()` → `thread.run(input)` (blocking) or `thread.turn(input)` → `TurnHandle.stream()` [CX-API] | `Agent.create(...)` → `agent.send(prompt, SendOptions)` → `run.wait()` / `run.messages()` [CU-PY] | `ChatAnthropic(...).invoke(messages)` / `.astream(messages)`; you own the loop [LC-ANTH] |
| Prompt input | str or async iterable of message dicts | str or `list[InputItem]` (Text/Image/LocalImage/Skill/Mention) [CX-REF] | str (+ SendOptions) | list of messages (`HumanMessage`/tuples) |
| In-process Python tools | Yes: `@tool` + `create_sdk_mcp_server` (in-process MCP) [CC-PY] | **No SDK API** (protocol has "dynamic tools" types but nothing exposed) [CX-MODELS, CX-API] | Yes: `local.custom_tools` dict of `CustomTool` [CU-PY] | Yes: `bind_tools()` + your own executor loop [LC-ANTH] |
| MCP stdio/HTTP | `mcp_servers={name: {type: stdio\|http\|sse,...}}` option [CC-PY] | config.toml `[mcp_servers.X]` (stdio + streamable HTTP), settable via `CodexConfig.config_overrides` / per-thread `config` [CX-MCP, CX-CLIENT] | inline `StdioMcpServerConfig`/`HttpMcpServerConfig` + `.cursor/mcp.json` [CU-PY] | `langchain-mcp-adapters` `MultiServerMCPClient` (client-side) or Anthropic MCP connector `mcp_servers=` (URL-only, server-side) [LC-MCP, LC-ANTH] |
| System prompt | `system_prompt` str / preset+append / file [CC-PY] | `base_instructions` (replaces built-in) + `developer_instructions` on thread_start/resume [CX-API] | **None for top-level agent** (only rules files, subagent `prompt`) [CU-PY] | system message in the messages list [LC-ANTH] |
| Structured output | `output_format={"type":"json_schema","schema":...}` [CC-PY] | `output_schema=` (JSON Schema) per `run()`/`turn()` [CX-API, CX-MODELS] | **Not documented / absent** [CU-PY] | `with_structured_output(schema, method="json_schema")` [LC-ANTH] |
| History | native resume: `resume=session_id`, `fork_session`, `continue_conversation` | native: `thread_resume(id)` / `thread_fork(id)` | native: `Agent.resume(agent_id)`; follow-up `send()` continues conversation | **client-owned messages array only** |
| Stop/cancel | `client.interrupt()` (streaming mode only) [CC-PY] | `TurnHandle.interrupt()` → `turn/interrupt` RPC; also `steer()` [CX-API] | `run.cancel()` [CU-PY] | break/close the stream; no server session to cancel |
| Native subagents | `agents={name: AgentDefinition}`; fresh context; parallel; nest to depth 3 default [CC-SUB] | collab tools (spawnAgent/sendInput/wait/closeAgent) exist at protocol level, **not exposed in Python SDK**; `thread_fork` only [CX-MODELS, CX-API] | `agents={name: AgentDefinition}` + `.cursor/agents/*.md`; fresh context; parallel; 2 levels [CU-PY, CU-SUB] | none (roll your own) |
| Native persistence | `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl` [LOCAL-CC] | `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl` [LOCAL-CX] | pluggable `LocalAgentStore` (Sqlite/Jsonl built-ins), "per-workspace state root on disk by default" [CU-PY] | none |

---

## 1. Making an agent call: history + prompt + tools + MCP

### 1.1 Claude Agent SDK (claude-agent-sdk)

**Prompt.** `query(prompt=..., options=...)` where prompt is a `str` or `AsyncIterable[dict]` of `{"type":"user","message":{"role":"user","content":...}}` dicts (streaming input). For multi-turn interactive use, `ClaudeSDKClient` with `await client.query(prompt)` then `async for msg in client.receive_response()`. [CC-PY]

**Python tools.** `@tool(name, description, input_schema)` decorating an async function returning `{"content":[{"type":"text","text":...}]}`; bundle with `create_sdk_mcp_server(name, version, tools=[...])` and pass as a value in `mcp_servers`. Tool names become `mcp__<server>__<tool>`; add to `allowed_tools` to auto-approve. In-process — no subprocess. [CC-PY]

**MCP.** `ClaudeAgentOptions.mcp_servers: dict[str, McpServerConfig] | str | Path`. Config shapes: `{"type":"stdio","command":...,"args":[...],"env":{...}}`, `{"type":"http","url":...}`, `{"type":"sse","url":...}`, or an SDK server object. `strict_mcp_config=True` ignores `.mcp.json`/user settings/plugins. Runtime control: `get_mcp_status()`, `reconnect_mcp_server()`, `toggle_mcp_server()`. [CC-PY]

**History.** Native resume only: `resume=<session_id>` (+ `fork_session=True` to branch, `resume_session_at=<message-uuid>` to branch from mid-conversation, `continue_conversation=True` for most recent). There is **no messages-array injection API** — history lives in the CLI-managed session transcript. Session id arrives on messages (`ResultMessage.session_id`). [CC-PY; verified earlier this session]

**System prompt.** `system_prompt` accepts a raw string (replaces the Claude Code prompt), `{"type":"preset","preset":"claude_code","append":...}`, or `{"type":"file","path":...}`. [CC-PY]

**Structured output.** `output_format={"type":"json_schema","schema":{...}}`. [CC-PY]

**Streaming events.** The iterator yields typed dataclasses: `SystemMessage` (init), `UserMessage` (content: text/tool_result blocks), `AssistantMessage` (content: `TextBlock`/`ThinkingBlock`/`ToolUseBlock`), `ResultMessage` (subtype success/error, `terminal_reason` e.g. `"end_turn"`, `"aborted_streaming"`, usage, result str), plus opt-in `StreamEvent` (raw API deltas, `include_partial_messages=True`), `TaskNotificationMessage` (background subagents), hook events (`include_hook_events=True`). [CC-PY]

### 1.2 Codex Python SDK (openai-codex)

**Prompt.** `thread = codex.thread_start(...)`; `thread.run(input)` (collects `TurnResult`) or `thread.turn(input)` (returns `TurnHandle` immediately). `input: RunInput` = `str` or list of `TextInput | ImageInput(url) | LocalImageInput(path) | SkillInput(name,path) | MentionInput(name,path)`; a bare string becomes `[{"type":"text","text":...}]`. [CX-API, CX-CLIENT `_normalize_input_items`, CX-REF]

**Python tools.** **Not supported by the SDK.** `api.py` exposes no tool registration; the generated protocol contains `FunctionDynamicToolSpec` / `DynamicToolCallThreadItem` types, but no SDK method or `ThreadStartParams` field references dynamic tools ([CX-MODELS]: `dynamic_tools` appears in no params model). The only inbound extension point is `CodexClient(approval_handler=...)`, a callback answering server→client approval requests (`item/commandExecution/requestApproval`, `item/fileChange/requestApproval`; default accepts both). [CX-CLIENT] ⇒ tradewind must expose user Python tools to Codex **via an MCP stdio server it hosts itself**.

**MCP.** Configured through Codex config, not per-call API: config.toml `[mcp_servers.<name>]` with stdio (`command`, `args`, `env`) or streamable HTTP (`url`, `bearer_token_env_var`, `http_headers`), plus `enabled`, `enabled_tools`/`disabled_tools`. [CX-MCP] From the SDK, inject via `CodexConfig(config_overrides=("key=value", ...))` which becomes repeated `--config kv` CLI flags on the `codex app-server` launch [CX-CLIENT `start()`], or per-thread `config: JsonObject` on `thread_start`/`thread_resume` (field exists in `ThreadStartParams`; its accepted keys are not documented) [CX-MODELS].

**History.** Native: `thread.id` is available immediately; `codex.thread_resume(thread_id)` / `thread_fork(thread_id)`; `thread.read(include_turns=True)` returns transcript (`ThreadReadResponse` with `ThreadItemEntry{item, turn_id}` list). No messages-array injection. [CX-API, CX-MODELS]

**System prompt.** `thread_start(base_instructions=...)` (replaces the built-in "You are Codex..." prompt — the default text is visible in rollout `session_meta.base_instructions` [LOCAL-CX]) and `developer_instructions=...` (additional developer-level guidance); both also on `thread_resume`/`thread_fork`. The precise semantic difference is not documented [CX-REF says "Both parameters appear ... without documented distinction"].

**Structured output.** `output_schema: JsonObject` on `run()`/`turn()`: "Optional JSON Schema used to constrain the final assistant message for this turn." [CX-MODELS `TurnStartParams.output_schema`]

**Streaming events.** `TurnHandle.stream()` yields `Notification{method, payload}` routed per turn until `turn/completed`. The full registry (NOTIFICATION_MODELS) includes, most relevantly: `turn/started`, `turn/completed`, `turn/diff/updated`, `turn/plan/updated`, `item/started`, `item/completed`, `item/agentMessage/delta`, `item/reasoning/textDelta`, `item/reasoning/summaryTextDelta`, `item/commandExecution/outputDelta`, `item/fileChange/outputDelta`, `item/fileChange/patchUpdated`, `item/mcpToolCall/progress`, `item/plan/delta`, `thread/started`, `thread/compacted`, `thread/tokenUsage/updated`, `error`, `warning`. [CX-NOTIF] `run()` folds the stream: items collected from `item/completed` (`ItemCompletedNotification`), usage from `thread/tokenUsage/updated`, `final_response` = last `AgentMessageThreadItem` with `phase == final_answer`. [`_run.py` in sdk/python] `TurnResult` = `{id, status, error, started_at, completed_at, duration_ms, final_response, items, usage}`. [CX-REF]

**Item taxonomy** (`ThreadItem` union): `userMessage`, `agentMessage` (text, phase), `reasoning`, `plan`, `commandExecution` (command, cwd, exit_code, aggregated_output, status), `fileChange` (changes, status), `mcpToolCall`, `dynamicToolCall`, `collabAgentToolCall`, `subAgentActivity`, `webSearch`, `imageView`, `imageGeneration`, `sleep`, `hookPrompt`, `enteredReviewMode`/`exitedReviewMode`, `contextCompaction`. [CX-MODELS]

**Per-turn overrides.** `run()/turn()` accept `approval_mode, cwd, effort, model, output_schema, personality, sandbox, service_tier, summary` — each documented as "Override ... for this turn and subsequent turns". [CX-API, CX-MODELS]

### 1.3 Cursor Python SDK (cursor-sdk)

**Prompt.** `Agent.create(model=..., local=LocalAgentOptions(cwd=...))` (or `cloud=CloudAgentOptions`); `run = agent.send("prompt", SendOptions(...))`; `run.wait()` for final `result`, or stream. `SendOptions` fields: `model`, `mode` ("agent"|"plan"), `mcp_servers` (inline, "fully replaces creation-time servers for this run"), `cloud.env_vars`, `local.force`, `idempotency_key`, `on_step`, `on_delta`. [CU-PY]

**Python tools.** `LocalAgentOptions.custom_tools: dict[str, CustomTool(description, input_schema, execute)]` — `execute(args, context: CustomToolContext)` is a plain Python callable; local agents only. Not persisted: "pass them again on resume". Tool allow/deny: `tools=[...]`, `disallowed_tools=[...]` (local only, also not persisted). [CU-PY]

**MCP.** Inline `HttpMcpServerConfig(url, auth=McpAuth(...)/headers)` and `StdioMcpServerConfig(command, args, env)`; plus file-based `.cursor/mcp.json` / `~/.cursor/mcp.json` loaded per `setting_sources`. "Inline MCP servers are not persisted across resume ... Pass them again on resume, or use file-based MCP config." [CU-PY]

**History.** Native: follow-up `agent.send()` continues the conversation; `Agent.resume(agent_id, options=None, *, client=None)` reconnects (runtime auto-detected from id prefix: `"agent-<uuid>"` local, `"bc-<uuid>"` cloud). `agent.agent_id` is "populated immediately after creation". `agent.model` is `None` on resume unless re-passed. No messages-array injection. [CU-PY]

**System prompt.** **No programmatic top-level system prompt field is documented** on `AgentOptions`/`LocalAgentOptions`; behavior customization is via rules files/setting sources and subagent `AgentDefinition.prompt`. [CU-PY]

**Structured output.** **Not documented anywhere in the SDK docs** — no schema/JSON output option on `SendOptions` or elsewhere. [CU-PY]

**Streaming events.** Three layers: (a) `run.messages()` typed `SDKMessage`s — `system` (subtype, model, tools), `user`, `assistant` (content TextBlock/ToolUseBlock), `thinking` (text, thinking_duration_ms), `tool_call` (call_id, name, status, args, result, truncated), `status`, `task`, `request`, `usage`; (b) `on_delta` callback `InteractionUpdate`s — `TextDeltaUpdate`, `ThinkingDeltaUpdate/Completed`, `ToolCallStartedUpdate/CompletedUpdate/PartialToolCallUpdate`, `TokenDeltaUpdate`, `StepStarted/CompletedUpdate`, `TurnEndedUpdate`, `UserMessageAppendedUpdate`, `Summary*Update`, `ShellOutputDeltaUpdate`; (c) `run.events()` low-level `RunStreamEvent` envelopes with `.kind`/`.offset`. One underlying stream: consuming `messages()`, `iter_text()`, or `events()` advances the same stream. [CU-PY]

### 1.4 langchain-anthropic (raw Anthropic API, no LangGraph)

**Prompt + history.** Fully client-owned: `model.invoke([...])` with a list of messages — system as first `("system", ...)`/`SystemMessage`, then `HumanMessage`, `AIMessage` (may carry `tool_calls`), `ToolMessage(content, tool_call_id=...)`. Tradewind must persist and replay this list itself — this is the only backend where tradewind's store is the *source of truth* rather than a mirror. [LC-ANTH]

**Python tools.** `model.bind_tools([fn_or_pydantic_or_dict, ...])`; after `invoke`, read `AIMessage.tool_calls` (`[{name, args, id}]`), execute yourself, append `ToolMessage`s, re-invoke — the self-owned tool loop. [LC-ANTH]

**MCP.** Two routes: (a) client-side via `langchain-mcp-adapters`: `MultiServerMCPClient({name: {"transport":"stdio","command":...,"args":[...]} | {"transport":"http","url":...}})` → `await client.get_tools()` → standard LangChain tools usable with `bind_tools()` + `tool.ainvoke(args)` in your own loop (no LangGraph required) [LC-MCP]; (b) server-side via Anthropic's MCP connector: `ChatAnthropic(mcp_servers=[{"type":"url","url":...,"name":...}])` (URL servers only — no stdio) [LC-ANTH].

**Structured output.** `model.with_structured_output(Schema, method="json_schema")`. [LC-ANTH]

**Streaming.** `.stream()`/`.astream()` yield `AIMessageChunk`s (aggregate content); `stream_events(..., version="v3")` for finer-grained events; thinking via `thinking={"type":"enabled","budget_tokens":N}` and `response.content_blocks`. [LC-ANTH]

### 1.5 Synthesis: common core vs. per-backend adapters

**Common across all four (safe to unify):**

1. *Session/handle lifecycle*: create-or-resume → send prompt → stream events → final result. All four fit `session = backend.start(opts)` / `backend.resume(id)`; only LangChain's "resume" is replay-from-store.
2. *Text prompt in, final text out*: every backend takes a string prompt and produces a final assistant text (`ResultMessage.result` / `TurnResult.final_response` / `run.wait().result` / `AIMessage.content`).
3. *Tool-call/tool-result event pair*: all four surface (name, id/call_id, input/args) and a result — Claude `ToolUseBlock`/`ToolResultBlock`, Codex item types (`mcpToolCall`, `commandExecution`, `fileChange` are tool-shaped), Cursor `SDKToolUseMessage` (carries args *and* result in one), LangChain `tool_calls`/`ToolMessage`.
4. *Usage*: token usage objects everywhere (field names differ).
5. *Model override per call*: Claude `model`/`set_model`, Codex per-turn `model`, Cursor `SendOptions.model`, LangChain constructor/`bind`.

**Unified event model (proposed mapping):** a small vocabulary covers all four — `turn_started`, `text_delta`, `thinking_delta`, `tool_call_started`, `tool_call_completed`, `item_completed(kind, payload)`, `turn_completed(status, final_text, usage)`, `error`. Mapping: Claude `StreamEvent`+message dataclasses → deltas + item_completed per content block, `ResultMessage` → turn_completed; Codex `item/agentMessage/delta` → text_delta, `item/reasoning/*Delta` → thinking_delta, `item/started`/`item/completed` → tool_call events, `turn/completed` → turn_completed; Cursor `InteractionUpdate`s map nearly 1:1 (`TextDeltaUpdate`, `ThinkingDeltaUpdate`, `ToolCallStartedUpdate/CompletedUpdate`, `TurnEndedUpdate`); LangChain chunk stream → text_delta, `tool_calls` on final chunk → tool_call_started (execution then happens inside tradewind's own loop, which emits tool_call_completed itself). Keep an `extra`/`raw` field on every unified event — each backend has events with no counterpart (see §4 misfits).

**Needs per-backend special treatment:**

- *Tool registration*: three different mechanisms (in-process MCP server / MCP stdio subprocess / native dict / bind_tools+self-loop). Recommended tradewind design: one `Tool` abstraction; adapters render it as (Claude) SDK MCP server, (Codex) a tradewind-hosted MCP stdio server registered via config override, (Cursor) `custom_tools`, (LangChain) `bind_tools` + executor. Codex is the outlier: tool calls cross a process boundary and results are attributed to an MCP server name.
- *System prompt*: Cursor has none at top level — tradewind can only approximate via rules files or by prefixing the first user prompt; must be documented as a capability gap.
- *Structured output*: absent on Cursor; unify as optional capability with `supports_structured_output` flag.
- *History*: LangChain alone needs message replay; the other three need only an id. Tradewind's interface should be `resume(session_ref)` where `session_ref` carries either a native id or a message log.
- *Permissions*: Claude has a programmatic `can_use_tool` callback; Codex has `approval_handler` + `approval_mode`/sandbox; Cursor has **file-based hooks.json only** (verified earlier this session); LangChain n/a (you own the loop, so you are the permission system).

## 2. Resume and stop/cancel

### Resume gaps filled

- **Cursor**: `Agent.resume(agent_id, options=None, *, client=None) -> Agent`; `agent_id` is on the handle immediately after `Agent.create()` (`agent.agent_id`, `"agent-<uuid>"` local / `"bc-<uuid>"` cloud) and on metadata snapshots. Must re-pass on resume: inline MCP servers, `custom_tools`, `tools`/`disallowed_tools`, `model` (else `agent.model is None`), and (implied, same pattern) inline `agents`. "Local agents persist conversation state and run metadata through the bridge, so follow-ups and `Agent.resume()` survive a process restart." [CU-PY]
- **Claude**: (verified earlier) `resume=session_id`, `continue_conversation`, `fork_session`; additionally `resume_session_at=<message-uuid>` truncates/branches from a mid-session point, with `resume_drops_turn` naming the discarded turn. [CC-PY]
- **Codex**: `thread_resume(thread_id, ...)` accepts fresh `approval_mode/base_instructions/config/cwd/developer_instructions/model/...` overrides, so a resume can re-configure the thread. [CX-API]

### Stop/cancel per backend

- **Claude**: `await client.interrupt()` — "Send interrupt signal (streaming mode only)" (i.e., requires `ClaudeSDKClient` streaming-input mode, not one-shot `query()`); afterwards drain `receive_response()` until a `ResultMessage` whose `terminal_reason` is e.g. `"aborted_streaming"`. `disconnect()` tears down the client. Also `stop_task(task_id)` for background subagent tasks. [CC-PY] *Partial-turn persistence*: not explicitly documented; local transcripts show records are appended to the session .jsonl incrementally as the turn runs (assistant/tool_use/tool_result records carry their own timestamps within a turn), so messages emitted before the interrupt are on disk and a subsequent `resume` sees them. [LOCAL-CC — observed behavior, not a doc guarantee]
- **Codex**: `TurnHandle.interrupt()` → JSON-RPC `turn/interrupt {threadId, turnId}` → `TurnInterruptResponse`. `TurnStatus` has an explicit `interrupted` member, so an interrupted turn is a first-class terminal state recorded on the turn. Mid-turn *redirection* without killing: `TurnHandle.steer(input)` → `turn/steer {threadId, expectedTurnId, input}` ("Send additional input to this active turn"). [CX-API, CX-CLIENT, CX-MODELS] *Persistence*: rollout .jsonl is appended per event/item during the turn (observed [LOCAL-CX]); items completed before the interrupt are in the thread and visible to `thread_read`/resume.
- **Cursor**: `run.cancel()` — "The status moves to `\"cancelled\"`, the live stream stops, in-flight tool calls stop, and `run.wait()` resolves with `status: \"cancelled\"`. Partial output (assistant text written so far) stays on the `Run` object." Calling it on a terminal run (`finished`/`error`/`cancelled`/`expired`) raises `UnsupportedRunOperationError` — check `run.status` first. For a stuck local run, `SendOptions(local={"force": True})` expires the previous run before a new send. [CU-PY] Whether the cancelled partial turn is written to the store is not documented.
- **LangChain/Anthropic**: no server-side session exists; a "turn" is your HTTP request. Cancel = stop consuming: break out of the `.stream()`/`.astream()` loop (closing the generator closes the underlying HTTP stream) or cancel the asyncio task. State impact is entirely yours: the partial `AIMessageChunk` aggregate exists only in your process; tradewind decides whether to persist a partial AIMessage (recommendation: persist it flagged `interrupted=true` but do **not** replay a dangling `tool_use` without a matching `ToolMessage` — the Anthropic API rejects unmatched tool_use blocks). [derived from LC-ANTH streaming docs + API contract; flagged as design reasoning, not a doc citation]

## 3. Subagent semantics — verdict on the "spawn a fresh session" theory

**User's theory**: subagent = fresh agent, own prompt, no parent conversation context, optional different model ⇒ emulation by spawning a new session loses nothing.

**Claude Agent SDK.** `agents={name: AgentDefinition(description, prompt, tools, disallowedTools, model, skills, memory, mcpServers, maxTurns, background, effort, permissionMode)}`. Context: "Unless the subagent is a fork, its context window starts fresh, with no parent conversation ... The only content you pass from parent to subagent is the Agent tool's prompt string." The subagent receives: its own system prompt + the Agent tool prompt, project CLAUDE.md, tool definitions (inherited or the `tools` subset); it does **not** receive the parent's conversation history, tool results, parent system prompt, or preloaded skills. "The parent receives the subagent's final message as the Agent tool result." So the *isolation* part of the theory is exactly right. [CC-SUB]

But native support adds things emulation does not get for free:
1. **Model-driven scheduling**: Claude itself decides when/which subagent to invoke (description matching), issues parallel Agent tool calls, and runs them concurrently (default cap 20, `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`); background subagents (`background: true`, `TaskNotificationMessage`, `stop_task`). [CC-SUB, CC-PY]
2. **Automatic result reintegration**: the final message returns as the Agent tool result inside the parent's turn, is sanitized ("scans the final message for instruction-shaped patterns", v2.1.210+), and partial results are marked when `maxTurns` is hit so the parent knows to resume. [CC-SUB]
3. **Nesting + governance**: subagents spawn subagents (depth default 3 via `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`), shared budget enforcement (`max_budget_usd` counts subagent spend, stops background subagents at the cap). [CC-SUB]
4. **Fork mode**: a subagent can instead be a *fork* that inherits the parent conversation — something a fresh session cannot emulate at all without transcript copying. [CC-SUB]
5. **Resumability**: subagent transcripts persist separately; `agentId` in the tool result + parent `resume` lets you continue a specific subagent. [CC-SUB]

**Cursor SDK.** Same fresh-context model: "Subagents begin with a clean context and don't access prior conversation history"; they "inherit all tools from the parent, including MCP tools"; results "return a final message with its results"; foreground blocks, background returns immediately; parallel ("Agent sends multiple Task tool calls in a single message, so subagents run simultaneously"); nesting limited: "a subagent launched by another subagent can't launch further ones" (i.e., two subagent levels — note this contradicts any "nest to any depth" reading of the 2.4-era changelog; current docs cap it). SDK `AgentDefinition` fields: `description`, `prompt`, `model` (`"inherit"`/None = parent), `mcp_servers` (names referencing the parent's servers). [CU-SUB, CU-PY]

**Codex.** No public SDK subagent API. The protocol reveals a native *collab* mechanism: `CollabAgentToolCallThreadItem` with `tool ∈ {spawnAgent, sendInput, resumeAgent, wait, closeAgent}`, `sender_thread_id`, `receiver_thread_ids` ("In case of spawn operation, this corresponds to the newly spawned agent"), per-spawn `model` and `reasoning_effort`; plus `SubAgentActivityThreadItem{agent_thread_id, kind ∈ started|interacted|interrupted}` and `task_started.collaboration_mode_kind` in rollouts. These are things the *agent runtime* does; the Python SDK exposes no method to configure or invoke them, and the SDK docs don't mention them. What the SDK does give you is `thread_fork` — the opposite of a fresh subagent (it *copies* history). So for Codex, spawn-a-new-thread emulation (`thread_start` with its own `base_instructions`/`model`, run, feed `final_response` back) is the only available route and matches the collab model structurally (spawned agents are separate threads anyway). [CX-MODELS, CX-API, LOCAL-CX]

**LangChain.** Nothing native; a "subagent" is just another messages array + model + tools — pure emulation, and nothing is sacrificed because there is nothing to sacrifice.

**Verdict.** The user's context model is *correct* on all backends that define subagents: fresh context, own system prompt, optional model override, only the final message returns. Emulation-by-new-session therefore reproduces the **data flow** faithfully. What it sacrifices is the **orchestration layer**, not the semantics:

- the parent *model* choosing autonomously when to delegate (description-based tool dispatch) — under emulation, tradewind's caller decides, or tradewind must expose a "spawn_subagent" tool to the model itself (which is exactly re-implementing the native feature);
- parallel scheduling, concurrency caps, and shared budget accounting;
- automatic in-turn result splicing (tool_result in the parent transcript) with output sanitization and partial/`maxTurns` markers;
- fork-mode subagents (inherit history) and nested delegation;
- permission/tool inheritance defaults (subagent inherits the parent's toolset unless narrowed).

Caveat for the "without sacrificing anything" claim: it holds **iff** tradewind's unified subagent feature is caller-orchestrated (tradewind code decides to spawn, waits, and injects the result as a normal prompt/tool result). If tradewind wants *model-initiated* delegation on all backends, it must register its own "spawn agent" tool in each backend's tool mechanism and run the child session itself — feasible everywhere (all four support custom tools or an owned loop, with Codex going through MCP), but tradewind then owns parallelism, budget, and result-sanitization concerns the native implementations already solve.

## 4. Unified session schema

### 4.1 Native formats observed

**Claude `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`** [LOCAL-CC — sampled across 5 recent sessions, v2.1.247+]: one JSON object per line. Record `type` inventory observed: `user`, `assistant`, `system`, `attachment`, `queue-operation`, `last-prompt`, `custom-title`, `bridge-session`, `atis-latch`, `mode`, `pr-link`, `frame-link`, `artifact-comment-monitor`, `artifact-autoreact-ledger` (the latter ~10 are harness bookkeeping). Conversation records:
- `user`: `{parentUuid, isSidechain, promptId, type:"user", message:{role, content}, uuid, timestamp, permissionMode, cwd, sessionId, version, gitBranch, userType, entrypoint, ...}`; `message.content` is a string (typed prompt) or block list (`tool_result` blocks with `tool_use_id`, `content`, `is_error`; `text` blocks for attachments/skills).
- `assistant`: same envelope plus `requestId`, `effort`; `message` is a full API message: `{model, id, type, role, content, stop_reason, stop_sequence, stop_details, usage, diagnostics}` with content blocks `text`, `thinking` (with opaque `signature`), `tool_use` (`id`, `name`, `input`, `caller`).
- `system`: subtypes like `stop_hook_summary` (hook telemetry).
- Threading: `uuid`/`parentUuid` form a DAG; `isSidechain` marks subagent branches; `sessionId` on every line. (Note: no explicit `result` record type appeared in sampled files; the `ResultMessage` is an SDK-level synthesis.)

**Codex `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl`** [LOCAL-CX]: lines `{timestamp, type, payload}` with `type ∈ {session_meta, response_item, event_msg, ...}`. `session_meta.payload` = `{session_id/id, timestamp, cwd, originator, cli_version, source, model_provider, base_instructions:{text}, ...}`. `response_item.payload` = raw model-API items (`{type:"message", role, content:[{type:"input_text"|...}]}`, reasoning, function calls). `event_msg.payload.type` observed: `task_started` (turn_id, collaboration_mode_kind), `user_message`, `agent_message` (message, phase, memory_citation), `task_complete` (turn_id, last_agent_message), `token_count` (total/last token usage, rate limits). The SDK-level view of the same data is `thread_read(include_turns=True)` → `ThreadItemEntry{turn_id, item: ThreadItem}` with the 18-member item union of §1.2 — prefer consuming the SDK view over parsing rollouts. [CX-MODELS]

**Cursor store**: pluggable `local.store` (`LocalAgentStoreConfig`); built-ins `SqliteLocalAgentStore` / `JsonlLocalAgentStore` (verified earlier this session); docs say only that the bridge "persists conversation state and run metadata ... under a per-workspace state root on disk by default". **The table/record schema is not documented** [CU-PY]; treat it as opaque and capture tradewind's copy from the `run.messages()` stream instead.

**LangChain**: no persistence; canonical types `HumanMessage`, `AIMessage` (`content`, `tool_calls: [{name,args,id}]`, `usage_metadata`), `ToolMessage` (`content`, `tool_call_id`), `SystemMessage`. [LC-ANTH]

### 4.2 Proposed tradewind SQLite schema

Design stance: for claude/codex/cursor the native store is **authoritative** (resume by native id); tradewind's DB is a *mirror* for querying/UI/analytics plus the *system of record* for langchain sessions. Mirror rows are built from the live event stream (all four expose everything needed at stream time), never by parsing native files — with native-file import as an optional backfill.

```sql
CREATE TABLE sessions (
  session_id        TEXT PRIMARY KEY,          -- tradewind UUID
  backend           TEXT NOT NULL,             -- 'claude' | 'codex' | 'cursor' | 'langchain'
  native_session_id TEXT,                      -- Claude session_id | Codex thread.id | Cursor agent_id | NULL
  parent_session_id TEXT REFERENCES sessions(session_id),  -- fork/subagent lineage
  spawn_kind        TEXT,                      -- NULL | 'fork' | 'subagent'
  title             TEXT,
  cwd               TEXT,
  model             TEXT,
  system_prompt     TEXT,                      -- as configured by tradewind (Cursor: NULL)
  options_json      TEXT,                      -- full backend options snapshot (tools/MCP names, approval mode, ...)
  status            TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'archived'
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  native_meta_json  TEXT                       -- Codex session_meta / Claude init SystemMessage / Cursor metadata snapshot
);

CREATE TABLE turns (
  turn_id     TEXT PRIMARY KEY,               -- Codex turn.id | Cursor run id | Claude promptId (fallback: tradewind UUID) | tradewind UUID (langchain)
  session_id  TEXT NOT NULL REFERENCES sessions(session_id),
  seq         INTEGER NOT NULL,
  status      TEXT NOT NULL,                  -- 'completed' | 'interrupted' | 'cancelled' | 'failed' | 'in_progress'
  final_text  TEXT,                           -- ResultMessage.result | TurnResult.final_response | run.wait().result | AIMessage text
  usage_json  TEXT,                           -- normalized {input_tokens, output_tokens, cache_read_tokens, thinking_tokens, total_tokens}
  cost_usd    REAL,                           -- Claude total_cost_usd / Cursor charged_cents; NULL elsewhere
  started_at  TEXT, completed_at TEXT,
  error_json  TEXT
);

CREATE TABLE messages (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id    TEXT NOT NULL REFERENCES sessions(session_id),
  turn_id       TEXT REFERENCES turns(turn_id),
  seq           INTEGER NOT NULL,             -- total order within session
  role          TEXT NOT NULL,                -- 'user' | 'assistant' | 'tool' | 'system'
  kind          TEXT NOT NULL,                -- normalized: 'text' | 'thinking' | 'tool_use' | 'tool_result'
                                              --   | 'command_execution' | 'file_change' | 'plan' | 'web_search'
                                              --   | 'compaction' | 'event'
  content_json  TEXT NOT NULL,                -- normalized block: see mapping below
  native_id     TEXT,                         -- Claude uuid | Codex item.id | Cursor call_id/message id | LC message.id
  parent_native_id TEXT,                      -- Claude parentUuid; NULL elsewhere
  agent_path    TEXT,                         -- subagent attribution: Claude parent_tool_use_id/isSidechain lineage,
                                              --   Codex subAgentActivity.agent_thread_id, else NULL
  model         TEXT,
  created_at    TEXT,
  raw_json      TEXT                          -- verbatim native record/event payload (see misfits)
);
CREATE INDEX idx_messages_session_seq ON messages(session_id, seq);
CREATE INDEX idx_sessions_native ON sessions(backend, native_session_id);
```

**Normalized `content_json` and per-backend field mapping:**

| kind | content_json shape | Claude | Codex | Cursor | LangChain |
|---|---|---|---|---|---|
| text | `{text}` | `TextBlock.text` / user string content | `agentMessage.text` (+`phase`→raw), `userMessage.content` | `SDKAssistantMessage.message.content` text blocks / user event | `AIMessage`/`HumanMessage` content |
| thinking | `{text}` | `ThinkingBlock.thinking` (signature→raw) | `reasoning` item | `SDKThinkingMessage.text` | thinking content blocks |
| tool_use | `{tool_use_id, name, input}` | `ToolUseBlock{id,name,input}` | `mcpToolCall`/`dynamicToolCall{id,tool,arguments}` | `SDKToolUseMessage{call_id,name,args}` | `AIMessage.tool_calls[i]{id,name,args}` |
| tool_result | `{tool_use_id, content, is_error}` | `ToolResultBlock` | mcp/dynamic item `status`+output content_items | `SDKToolUseMessage.result` (same record as the call — split into two rows) | `ToolMessage{tool_call_id, content}` |
| command_execution | `{command, cwd, exit_code, output, status}` | (Bash tool_use/result pair also stored as tool_use/tool_result) | `commandExecution` item — native fit | shell tool_call | n/a (user-tool) |
| file_change | `{changes, status}` | Edit/Write tool pairs | `fileChange` item — native fit | edit tool_call | n/a |

**Turn boundaries**: Codex/Cursor have explicit ids (turn.id / run). Claude: group by `promptId` on user records (observed in [LOCAL-CC]) or by SDK stream position between user prompt and `ResultMessage`. LangChain: one invoke-loop = one turn (tradewind-generated id).

**What does NOT fit — keep in `raw_json`, don't normalize (recommendation):**

- Claude: `thinking.signature` (opaque, required only for API-level replay — irrelevant since resume is native), `attachment`/hook/`queue-operation`/`bridge-session` bookkeeping lines, `stop_details`, `diagnostics`, `caller` on tool_use, sidechain DAG details beyond `agent_path`.
- Codex: sandbox/approval flow (approval requests are JSON-RPC server-requests, not thread items), `plan`, `webSearch.results` ("opaque JSON" per the model docstring), `collabAgentToolCall`/`subAgentActivity`, review-mode and `contextCompaction` items, delta notifications (don't store deltas at all — store completed items), `token_count.rate_limits`.
- Cursor: `InteractionUpdate` deltas (skip), `SDKStatusMessage`/`SDKTaskMessage`/`SDKRequestMessage` (store as `kind='event'` with raw payload), store internals (opaque — do not attempt to mirror the Sqlite/Jsonl store schema).
- LangChain: `usage_metadata`, cache-control markers, citations blocks → raw.

A `schema_version` pragma plus the `raw_json` column makes the mirror lossy-tolerant: anything unrecognized round-trips.

## Unverified / undocumented

1. **Codex `base_instructions` vs `developer_instructions` semantics** — both parameters exist on thread start/resume/fork; the api-reference "provides no documented distinction". Inference from rollouts (base_instructions holds the full built-in persona prompt) is observational. [CX-REF, LOCAL-CX]
2. **Codex per-thread `config: JsonObject` accepted keys** (e.g. whether `mcp_servers` can be injected per-thread vs only via `--config` overrides at process launch) — undocumented. [CX-MODELS]
3. **Codex dynamic tools** — protocol types exist (`FunctionDynamicToolSpec`, `DynamicToolCallThreadItem`) but no Python SDK registration API; whether/when this becomes available is unknown. [CX-MODELS]
4. **Codex collab tools (spawnAgent etc.)** — visible only in generated models/rollouts; no SDK method, no docs. [CX-MODELS]
5. **Claude: partial-turn persistence after `interrupt()`** — not stated in docs; incremental transcript appends are locally observed behavior only. [LOCAL-CC]
6. **Cursor: whether a cancelled run's partial output is written to the persistent store** (vs only kept on the in-memory `Run`) — undocumented. [CU-PY]
7. **Cursor `SqliteLocalAgentStore`/`JsonlLocalAgentStore` internal schema** (tables/record layout, default path) — undocumented; treat as opaque. [CU-PY]
8. **Cursor structured output** — no doc mention anywhere; assumed absent.
9. **Cursor top-level system prompt option** — no documented field; only rules files and subagent `prompt`.
10. **Cursor subagent parallel-spawn limits** (max concurrent) — parallelism documented, caps not. [CU-SUB]
11. **Cursor "nesting to any depth"** — current docs say a subagent launched by another subagent can't launch further ones (two subagent levels); if a changelog claimed unlimited depth, current docs supersede it. [CU-SUB]
12. **Claude jsonl format stability** — the transcript format carries `version` per line and is not a documented public contract; prefer `get_session_messages()` over raw file parsing.
13. **LangChain stream-cancellation side effects** — closing the stream mid-tool_use and the do-not-replay-dangling-tool_use recommendation are design reasoning from the API contract, not a documented procedure.
