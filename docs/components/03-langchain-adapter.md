# Component: LangChain Adapter

## Purpose

The first `Backend` subclass (ARCHITECTURE §3.1) and the simplest full vertical:
the Anthropic API via `langchain-anthropic`, with Tradewind's own tool loop
(DR-1). Proves the port, the event model, the broker, and the store working
together before any SDK quirk enters the picture. On this backend the Session
Store is the system of record — there is no native store (FR-6.4 n/a).

## Owns

- The agentic loop: request → (tool calls → broker → execute → tool results) →
  request, until a final response or interruption.
- Message reconstruction: the request's messages array is rebuilt from the mirror
  each turn — the store is the conversation.
- Its capability declaration and its mapping onto the normalized event model.

## Depends on

`langchain-anthropic` (pinned; the only place it is imported), the `Backend`
port, `ToolHost` (direct in-process execution — no MCP shim needed here),
`PermissionBroker`.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Loop ownership | Tradewind's own loop, no AgentExecutor/LangGraph | DR-1; the broker check *is* the loop's middle, not a bolt-on. |
| History | rebuild full messages array from store per request | Store as source of truth (FR-5.1); makes REPLAY trivially lossless. |
| System prompt | `system` parameter every request | FR-8: `supports_system_prompt = True`, true system-channel authority. |
| Structured output | deferred | Tool-choice forcing with the caller's schema is the intended design, but where in the loop forcing applies (every iteration vs. only once caller tools are exhausted, and whether the forced call terminates the loop) is not yet decided; `supports_structured_output = False` until it is (task-8 fix round 1). |
| Interrupt | cancel the in-flight task / close the stream | Stateless API: nothing server-side to clean up; turn recorded `cancelled`/`interrupted` (FR-6.2). |
| Fork | none in the adapter | Flags describe *native* capability (R-1): `supports_fork = False`. Fork-by-copy is a client-level operation over the store (`Tradewind.fork`), available to any store-of-record session, decided above the port. |
| Thinking in rebuild | omitted | The API rejects replayed thinking blocks without signatures (signatures live in `raw_json`); thinking is legally droppable from history, so the rebuild excludes it. |

## Capabilities (FR-8)

```
supports_system_prompt          True
supports_structured_output      False   # deferred — tool-choice forcing design pending
supports_interactive_permissions True   # broker gates every tool execution
supports_in_process_tools       True
supports_native_resume          False   # resume is always mirror-rebuild (lossless here)
supports_fork                   False   # no native fork; Tradewind.fork covers it above the port
supports_transcript_read        False   # nothing native to read
supports_tool_round_cap         True    # the loop is Tradewind's own, so the cap is exact (FR-6.5)
```

## Turn algorithm

1. Load session options (merged per layering rule); rebuild messages from the
   mirror by awaiting `ctx.load_history()` — async and LAZY since FR-9.3, shaped
   by the turn's `history_scope` (`flat` default here; `tree` arrives with each
   descendant session pre-folded into one wrapped text block by the runner;
   `include_raw=False`; `tool_use`/`tool_result` kinds reconstruct real
   content blocks; thinking blocks are omitted — see Decisions).
   Before the request, the automatic compaction check runs (FR-5.8: gated on
   `CompactionSettings.auto` and the tier's `ModelMeta.context_window`; the
   summarizer is the tier's own model via `chat_model_factory`); the rebuild
   honors the latest `kind="compaction"` record — checkpoint-as-user-message
   plus rows `seq >= first_kept_seq`. Pure machinery lives in
   `domain/compaction.py`; the manual verb enters via `compact_history`
   (duck-typed from the runner, like `take_native_session_id`).
2. Bind caller tools (`ToolHost` schemas) to the model; send request with
   `system`, messages, and per-call options.
3. Stream: text/thinking deltas → normalized delta events (live only, I-3);
   completed blocks → mirror.
4. On tool calls: consult broker (`allow` → execute via ToolHost; `deny` →
   synthesized error tool_result; `ask` → emit `permission_request` event, await
   verdict). Append tool results; loop to 2's request step.
5. Each model call runs under the FR-6.6 retry policy (typed-first
   classification, exponential backoff, visible `retry_scheduled` items;
   a context-overflow error compacts once and retries — completed tool
   executions are NEVER re-run, the retry unit is one model call).
6. Terminate on final response (`end_reason="end_turn"`), provider
   truncation (`stop_reason="max_tokens"` → `end_reason="max_tokens"`; any
   tool calls on the truncated response are NOT executed), the caller's
   `max_tool_rounds` cap (honest partial, `end_reason="max_tool_rounds"` —
   FR-6.5), max-iteration guard, interrupt, or error; finalize the turn row.

MCP servers on this backend are client-side: `ToolHost` connects as an MCP client
and presents the servers' tools alongside caller tools (FR-3.1).

## Acceptance

- Conformance suite (§3.1 R-3) green for every case its flags include — this
  adapter defines the baseline matrix; notably: multi-tool turn with one `deny`
  and one `ask`; interrupt mid-loop leaves an `interrupted` turn with partial
  transcript; REPLAY of a 20-turn session reproduces byte-identical model input
  versus an uninterrupted run, thinking blocks excluded (they are omitted from
  every rebuild by design).
- Event taxonomy review checkpoint (P-1): after this adapter and the Claude
  adapter both pass, the event model freezes.
