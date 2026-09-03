# Pi implementation notes: five features, source-verified

Date: 2026-09-03. Follow-up to `docs/research/2026-09-03-pi-sdk-survey.md`
(doc-level survey). This document is implementation-grade: **Pi's source is
public** at https://github.com/earendil-works/pi (MIT; the npm package
`@earendil-works/pi-coding-agent` links to it), so every claim below was read
from the actual TypeScript, not the docs. Pinned revision: commit
`e44d75c20a51142abc056c243b13c1d7bb4be687`, package version 0.84.4
(npm `latest` as of 2026-09-03).

Citation form: `[src: <path>]` = file in the pi repo at that commit, all
paths relative to repo root. Line numbers are from that revision. Nothing in
this document is docs-only; the few judgment calls are marked INFERRED.

Package layout that matters here: `packages/ai` (`@earendil-works/pi-ai`,
the provider/model layer — model metadata, cost, low-level retry),
`packages/agent` (`@earendil-works/pi-agent-core`, the bare agent loop —
tool-hook verdict application), `packages/coding-agent`
(`@earendil-works/pi-coding-agent`, the product — sessions, compaction,
branch summaries, settings, the extension runner, the agent-level retry
policy).

---

## 1. Compaction

Files: `packages/coding-agent/src/core/compaction/compaction.ts` (the whole
algorithm, ~1000 lines of pure functions), `compaction/utils.ts`
(serialization + prompts + file-op tracking), `core/session-manager.ts`
(entry schema, context rebuild), `core/agent-session.ts` (trigger logic and
events).

### 1.1 Settings and trigger

```ts
interface CompactionSettings { enabled: boolean; reserveTokens: number; keepRecentTokens: number; }
// defaults: enabled=true, reserveTokens=16384, keepRecentTokens=20000
```
[src: compaction/compaction.ts:126–136]

Trigger predicate, exactly:

```ts
shouldCompact(contextTokens, contextWindow, settings) :=
  settings.enabled && contextTokens > contextWindow - settings.reserveTokens
```
[src: compaction/compaction.ts:235–238]

`contextTokens` comes from **provider-reported usage, not estimation, when
available**: `calculateContextTokens(usage) = usage.totalTokens ||
(input + output + cacheRead + cacheWrite)` on the *last valid assistant
message* (skipping aborted/error/all-zero-usage messages). Messages *after*
the last usage-bearing assistant message are estimated and added
(`estimateContextTokens` returns `{tokens, usageTokens, trailingTokens,
lastUsageIndex}`) [src: compaction/compaction.ts:146–230]. The estimator is
a deliberate chars/4 heuristic ("conservative — overestimates"), with images
counted as 4800 chars, covering every message role including toolCall
name+JSON-args length [src: compaction/compaction.ts:244–306].

There are **three automatic trigger cases**, dispatched in
`AgentSession._checkCompaction()` after each agent run (and also
pre-prompt): (1) *overflow with retry* — a context-overflow error or a
recoverable `length` stop: remove the failed assistant message from agent
state (it **stays in session history**), compact, retry the turn **once**
(`_overflowRecoveryAttempted` guard; a second overflow emits a terminal
`compaction_end` with `errorMessage`); (2) *overflow without retry* — a
*successful* response that nonetheless exceeded the window: compact,
preserve the response; (3) *threshold* — `shouldCompact` on the reported (or
estimated, for error/zero-usage responses) context tokens [src:
core/agent-session.ts:2111–2236]. Guards worth copying: an overflow from a
*different* model than the currently selected one is ignored (user may have
switched to a bigger-context model), and an assistant message *older than
the latest compaction entry's timestamp* never triggers (stale
pre-compaction usage would immediately re-trigger right after compacting)
[src: core/agent-session.ts:2141–2156]. There is also a **pre-request
check** (`_compactBeforeNextAssistantResponse`) that estimates the context
about to be sent and compacts before the call if it would trip the threshold
[src: core/agent-session.ts:542–559]. Manual `/compact [instructions]`
enters through a separate `compact()` method with reason `"manual"` but the
same lower-level machinery.

### 1.2 Cut-point selection (the never-split rule)

Cut-point rules are per-message-role, not heuristic [src:
compaction/compaction.ts:308–343]:

- **Valid cut points** (`isCutPointMessage`): `user`, `assistant`,
  `bashExecution`, `custom`, `branchSummary`, `compactionSummary` — i.e.
  everything **except `toolResult`**. "Never cut at tool results (they must
  follow their tool call). When we cut at an assistant message with tool
  calls, its tool results follow it and will be kept."
- **Turn-start messages** (`isTurnStartMessage`): `user`, `bashExecution`,
  `custom`, `branchSummary`, `compactionSummary` — *not* `assistant`, not
  `toolResult`. Used to detect whether a cut lands mid-turn.

`findCutPoint(entries, startIndex, endIndex, keepRecentTokens)` [src:
compaction/compaction.ts:403–461]:

1. Collect all valid cut-point indices in `[startIndex, endIndex)`.
2. Walk **backwards from newest**, accumulating per-entry estimated tokens
   (chars/4). When `accumulated >= keepRecentTokens`, stop; the cut index is
   the **closest valid cut point at or after** that entry. If the budget is
   never reached, default is the *first* valid cut point (keep everything).
3. Scan backwards past adjacent metadata entries that contribute nothing to
   context (model changes, labels, …), stopping at compaction boundaries or
   context-visible entries — so metadata riding just before the cut is kept
   with it.
4. Classify: if the cut entry does not start a turn, find the turn-start
   index backwards (`findTurnStartIndex`); result is
   `{ firstKeptEntryIndex, turnStartIndex, isSplitTurn }`.

So `keepRecentTokens` is a **soft floor** on the retained tail: the tail is
at least that big (by estimate) and extends to the nearest safe boundary.
There is no upper-bound trimming of the tail.

### 1.3 Split turns → two summaries, merged

When `isSplitTurn`, the span `[turnStart, firstKept)` — the *prefix of the
turn being split* — is summarized **separately** with a dedicated prompt
(`TURN_PREFIX_SUMMARIZATION_PROMPT`: "This is the PREFIX of a turn that was
too large to keep. The SUFFIX (recent work) is retained…" with sections
`## Original Request / ## Early Progress / ## Context for Suffix`), then
merged into one summary string:

```
`${historySummary}\n\n---\n\n**Turn Context (split turn):**\n\n${turnPrefixSummary}`
```
with the two LLM calls' usages combined [src:
compaction/compaction.ts:835–964]. The turn-prefix call gets a smaller
output budget: `min(0.5 * reserveTokens, model.maxTokens)` vs
`min(0.8 * reserveTokens, model.maxTokens)` for the main summary [src:
compaction/compaction.ts:672–675, 983–986].

### 1.4 Boundary chaining across repeated compactions

`prepareCompaction(pathEntries, settings)` [src:
compaction/compaction.ts:750–829]:

- No-op if the path's *last* entry is already a compaction.
- Find the **previous** compaction entry on the path. If present:
  `previousSummary = prevCompaction.summary`, and the summarization window
  starts at the index of `prevCompaction.firstKeptEntryId` (falling back to
  `prevCompactionIndex + 1` if that id is gone). So repeated compaction
  re-summarizes **the previously-kept tail plus everything since**, never
  its own summary text as conversation — the old summary instead feeds the
  LLM as `<previous-summary>` with an *update* prompt (§1.5).
- `tokensBefore` = estimated tokens of the **current full rebuilt context**
  (`estimateContextTokens(buildSessionContext(pathEntries).messages).tokens`).
- Compute the cut point within `[boundaryStart, len)`; capture
  `firstKeptEntryId` (the entry's 8-hex-char id; bail out `undefined` if the
  entry has no id — "session needs migration").
- Partition into `messagesToSummarize` (boundaryStart → turnStart or
  firstKept) and `turnPrefixMessages` (turnStart → firstKept, when
  splitting). Compaction entries themselves are excluded from the
  summarize-input (`getMessageFromEntryForCompaction` returns undefined for
  them).
- Cumulative **file-operation tracking**: read/written/edited path sets are
  extracted from `read`/`write`/`edit` toolCall arguments in summarized
  messages, *merged with the previous compaction entry's persisted
  `details.readFiles/modifiedFiles`* (only if that compaction was
  pi-generated, `!fromHook`), and appended to the summary text as
  `<read-files>…</read-files>` / `<modified-files>…</modified-files>` blocks
  [src: compaction/compaction.ts:42–70, 949–951; compaction/utils.ts:12–82].

### 1.5 Summary generation — prompt and model

- **Model**: the session's currently selected model (no separate cheap
  summarizer model), with auth resolved through the same runtime; requests
  set `cacheRetention: "none"` so one-off summaries don't write prompt
  cache [src: core/agent-session.ts:2258; compaction/compaction.ts:579–599].
- **System prompt**: "You are a context summarization assistant… Do NOT
  continue the conversation… ONLY output the structured summary." [src:
  compaction/utils.ts:156–158].
- **User message**: the conversation is **serialized to plain text** (not
  passed as chat messages) precisely "so model doesn't try to continue it":
  `[User]: …`, `[Assistant thinking]: …`, `[Assistant]: …`,
  `[Assistant tool calls]: name(k=v, …)`, `[Tool result]: …` with tool
  results truncated to 2000 chars each; wrapped in
  `<conversation>…</conversation>`, plus
  `<previous-summary>…</previous-summary>` when chaining [src:
  compaction/utils.ts:88–150; compaction/compaction.ts:683–693].
- **Instructions**: a fixed structured-checkpoint format — `## Goal`,
  `## Constraints & Preferences`, `## Progress` (Done / In Progress /
  Blocked), `## Key Decisions`, `## Next Steps`, `## Critical Context`,
  "Preserve exact file paths, function names, and error messages" — with a
  distinct *update* variant when a previous summary exists ("PRESERVE all
  existing information… move items from In Progress to Done…"); manual
  `/compact <text>` appends `Additional focus: <text>` [src:
  compaction/compaction.ts:467–539, 677–681].
- **Output budget**: `maxTokens = min(0.8 * reserveTokens,
  model.maxTokens)`.
- **Failure honesty**: a `length` stop is a hard failure ("generation hit
  the token cap and the summary is incomplete" — a truncated summary must
  not become a checkpoint), as is any toolCall in the response [src:
  compaction/compaction.ts:545–553, 715–721].
- The summarization call itself runs through the shared retry choke point
  `completeSummarization` → `retryAssistantCall` with the same retry policy
  as agent turns (§2), emitting dedicated
  `summarization_retry_scheduled/attempt_start/finished` events [src:
  compaction/compaction.ts:579–599; core/agent-session.ts:2860–2888].

### 1.6 The transcript record — full schema

Session files are JSONL, tree-structured; every entry extends:

```ts
interface SessionEntryBase { type: string; id: string; parentId: string | null; timestamp: string; }
// id: 8-char hex (randomUUID().slice(0,8), collision-checked, full UUID fallback)
```
[src: core/session-manager.ts:46–51, 221–228]

```ts
interface CompactionEntry<T = unknown> extends SessionEntryBase {
  type: "compaction";
  summary: string;              // the merged summary text incl. file-op tags
  firstKeptEntryId: string;     // id of the first retained entry (the boundary)
  tokensBefore: number;         // estimated context tokens before compaction
  details?: T;                  // pi default: { readFiles: string[]; modifiedFiles: string[] }
  usage?: Usage;                // LLM usage of the summarization call(s) (both, combined, if split-turn)
  fromHook?: boolean;           // true if an extension generated it
}
```
[src: core/session-manager.ts:69–80; CompactionDetails at
compaction/compaction.ts:34–37]

Appended via `appendCompaction(...)` as a child of the current leaf [src:
core/session-manager.ts:1110–1133]. The in-flight result type additionally
carries `estimatedTokensAfter?` (computed post-rebuild, event-only, not
persisted) [src: compaction/compaction.ts:88–97;
core/agent-session.ts:2360, 2377–2384].

**Context rebuild** (`buildContextEntries`): walk the leaf→root path; find
the **latest** compaction on it; emit `[compactionEntry, entries from
firstKeptEntryId up to the compaction's position, entries after the
compaction]` — older summarized entries are simply omitted. The compaction
entry itself renders into context as a **user message**:
`"The conversation history before this point was compacted into the
following summary:\n\n<summary>\n" + summary + "\n</summary>"` [src:
core/session-manager.ts:404–470; core/messages.ts:11–17, 176–183].

**Events**: `compaction_start {reason: "manual"|"threshold"|"overflow"}`
and `compaction_end {reason, result?: CompactionResult, aborted, willRetry,
errorMessage?}` [src: core/agent-session.ts:157–168]. Extensions get
veto/replace power via `session_before_compact` (may `cancel` or supply a
whole `CompactionResult`) and a post-hoc `session_compact` notification
[src: core/agent-session.ts:2273–2305, 2367–2375].

**Tradewind mapping sketch.** Lives in the Turn Runner / history-loading
layer (`application/turn_runner.py`, next to where `history_scope` folding
already happens via `TurnContext.load_history`), applied only to the two
mirror-fed paths (langchain backend, future REPLAY — ARCH §7 P-7); adapters
untouched. The mirror record maps onto the existing
`Kind = "compaction"` literal (`domain/models.py:29`) as a
`NormalizedMessage` whose `content` carries the summary and whose
`raw`/extra fields carry `first_kept_message_seq` (tradewind's analogue of
`firstKeptEntryId` — a `seq` in the session's message table, since
tradewind's mirror is flat rows, not an entry tree), `tokens_before`, and
the summarizer-call usage; rebuild logic = "emit compaction message, then
messages with `seq >= first_kept_seq`". The never-split rule maps directly:
tradewind's mirror stores `tool_use`/`tool_result` kinds (ARCH §4), so
valid cut points are any non-`tool_result` message. The trigger needs a
per-tier `contextWindow` (feature 3); absent metadata → no auto-trigger
(honest). New `CompactionStarted/CompactionCompleted` events would extend
the frozen taxonomy (`domain/events.py`, ARCH §3.2) and therefore need a
P-decision; alternatively the compaction record can surface as a plain
`ItemCompleted` with `kind="compaction"`, which the taxonomy already
permits with zero new members. The summarizer (prompt set, serialized-text
input, `<previous-summary>` update variant, length-stop-is-failure rule) is
one shared function reused by REPLAY and fork-summary (feature 5).

---

## 2. Auto-retry

Two independent layers, exactly as the docs claimed, both source-verified.

### 2.1 Agent-level retry (the one worth copying)

Settings [src: core/settings-manager.ts:24–35, 882–888]:

```ts
interface RetrySettings { enabled?: boolean /*true*/; maxRetries?: number /*3*/;
                          baseDelayMs?: number /*2000*/; provider?: ProviderRetrySettings; }
```

**Which errors retry**: classification is *string-pattern matching on the
error message* of an assistant message with `stopReason === "error"`
(`isRetryableAssistantError`) [src: packages/ai/src/utils/retry.ts:1–90,
224–229]:

- **Never retried** (checked first): quota/billing/subscription exhaustion —
  `insufficient_quota`, `out of budget`, `quota exceeded`, `billing`,
  OpenCode Go/free-tier limit error names, "Monthly usage limit reached",
  "available balance".
- **Retried**: overload/rate-limit/HTTP (`overloaded`, `rate limit`,
  `too many requests`, `429`, `500`, `502`, `503`, `504`, `524`,
  `service unavailable`, `server error`, `internal error`), transport
  (`network error`, `connection refused/lost`, `fetch failed`,
  `getaddrinfo`, `ENOTFOUND`, `EAI_AGAIN`, `socket hang up`, `timed out`,
  `timeout`, `terminated`, websocket close/error), premature stream endings
  (`ended without`, `stream ended before message_stop`, `http2 request did
  not get a response`), explicit provider retry guidance ("you can retry
  your request", …), gRPC `ResourceExhausted`.
- **Additionally excluded at the session layer**: context-overflow errors —
  those are routed to compaction instead
  (`_isRetryableError` first tests `isContextOverflow`) [src:
  core/agent-session.ts:2853–2857]. `stopReason === "aborted"` is terminal,
  never retried [src: ai/utils/retry.ts:176–180].

**Schedule**: `delayMs = baseDelayMs * 2^(attempt-1)` → 2s, 4s, 8s at
defaults; max 3 retry attempts (initial call doesn't count); the backoff
sleep is abortable (`abortRetry()`) [src: core/agent-session.ts:2894–2944].
No jitter at this layer.

**Events** [src: core/agent-session.ts:169–170]:

```ts
{ type: "auto_retry_start"; attempt: number; maxAttempts: number; delayMs: number; errorMessage: string }
{ type: "auto_retry_end";   success: boolean; attempt: number; finalError?: string }
```

Emission discipline: `auto_retry_start` fires *before* each backoff sleep
(per attempt). `auto_retry_end {success:true, attempt}` fires as soon as a
subsequent assistant message completes with non-error stopReason — the
counter resets immediately "to prevent accumulation across multiple LLM
calls within a turn" [src: core/agent-session.ts:702–711].
`auto_retry_end {success:false, finalError}` fires when a non-retryable
error arrives while attempts were outstanding, or when the budget is
exhausted, or with `finalError:"Retry cancelled"` when aborted mid-sleep
[src: core/agent-session.ts:1131–1139, 2930–2938]. The `agent_end` event is
enriched with `willRetry` so UIs know the failure isn't final [src:
core/agent-session.ts:143–150, 670, 725–729].

**Partial streamed output interaction** — the precise mechanism: the failed
assistant message (with whatever partial content streamed before the error)
is **persisted to the session file on `message_end` like any message**, and
then, in `_prepareRetry`, **removed from the in-memory agent context**
(`agent.state.messages.slice(0, -1)`) before re-calling — "Remove error
message from agent state (keep in session for history)" [src:
core/agent-session.ts:673–695, 2918–2922]. So the transcript is honest (the
partial attempt is visible) but the model never sees its own failed partial
answer. Retry then re-runs from the same context — position: after each
agent run, `_handlePostAgentRun` returns true and the loop calls
`agent.continue()` [src: core/agent-session.ts:1105–1148]. Retry granularity
is therefore the *last LLM call within the turn*, not the whole turn: tool
calls already executed and mirrored stay executed.

**The same policy object is reused** for compaction/branch-summary
summarization calls via `retryAssistantCall(produce, policy, signal,
callbacks)` — a generic bounded-retry wrapper in `pi-ai` with the same
2^(n-1) backoff, whose callbacks surface as separate
`summarization_retry_*` events so UI can distinguish [src:
ai/utils/retry.ts:145–213; core/agent-session.ts:171–184, 2860–2888].

### 2.2 Provider-level retry (inner layer)

`retry.provider = { timeoutMs?, maxRetries? /*default 0*/,
maxRetryDelayMs? /*default 60000*/ }`. Implementation
`retryProviderRequest` deliberately reproduces the OpenAI/Anthropic SDK
policy but with an *interruptible* sleep (SDKs are invoked with
`maxRetries: 0` and wrapped): honors `x-should-retry`, retries 408/409/429/
≥500/undefined-status, honors `retry-after(-ms)` headers but **fails
immediately if the server requests a delay above `maxRetryDelayMs`**, else
exponential `min(0.5 * 2^retryIndex, 8)s` with 0–25% downward jitter [src:
ai/utils/provider-retry.ts]. Default-off at this layer; the agent-level
layer is the primary one.

**Tradewind mapping sketch.** Turn-Runner-level (`application/
turn_runner.py`), above the Backend port — adapters stay thin. The error
classifier belongs in `domain/errors.py` as a predicate over the backend
error surfaced (tradewind should classify on typed error data where
adapters have it, falling back to Pi-style message patterns only for the
langchain path — string matching is Pi's necessity across 25 providers, not
a virtue). Scope guard per the survey: only retry when no output has been
mirrored for the current model-call round, or where resubmission is
idempotent per backend — on native-resume backends a failed turn's re-run
re-enters the provider's own loop, so retry likely needs a capability flag
(`supports_turn_retry`) or restriction to before-first-event failures.
`AutoRetryStarted`/`AutoRetryEnded` events (attempt, max_attempts,
delay_ms, error) are new members of the frozen event taxonomy
(`domain/events.py`, ARCH §3.2) → requires a P-decision, exactly as the
survey flagged. Config lives in `TurnDefaults`
(`application/config.py:37`): `retry_enabled: bool = True`,
`retry_max_attempts: int = 3`, `retry_base_delay_s: float = 2.0`. Pi's
partial-output rule translates as: the mirrored partial items stay in the
mirror (honest transcript), and the retried request must rebuild context
*without* the failed partial assistant output — trivially true on
mirror-rebuilding backends if the failed turn's items are excluded from
`load_history` by turn status.

### 2.3 Overflow-recovery retry (compaction-owned, distinct from 2.1)

Not the same counter: a context-overflow or recoverable-`length` failure
triggers *compact-then-retry-once* (§1.1 case 1), guarded by
`_overflowRecoveryAttempted`, which resets on the next clean response
[src: core/agent-session.ts:694–700, 2163–2201]. Tradewind should keep
these two retry reasons distinct in events (Pi encodes it as
`compaction_end.willRetry` rather than an `auto_retry_*` pair).

---

## 3. Model metadata

File: `packages/ai/src/types.ts` (schema), `packages/ai/src/models.ts`
(cost math), generated catalog `packages/ai/src/models.generated.ts`.

### 3.1 Full schema

```ts
interface ModelCostRates { input: number; output: number; cacheRead: number; cacheWrite: number; } // $/Mtok
interface ModelCostTier extends ModelCostRates {
  inputTokensAbove: number;   // tier applies when total input usage exceeds this
}
interface ModelCost extends ModelCostRates {
  tiers?: ModelCostTier[];    // "Request-wide pricing tiers. The highest matching
                              //  input threshold applies to the full request."
}

interface Model<TApi extends Api> {
  id: string; name: string; api: TApi; provider: ProviderId; baseUrl: string;
  reasoning: boolean;
  thinkingLevelMap?: ThinkingLevelMap;  // pi level -> provider value; null = unsupported level
  input: ("text" | "image")[];
  cost: ModelCost;
  contextWindow: number;
  maxTokens: number;
  samplingParams?: Record<string, unknown>;
  headers?: Record<string, string>;
  compat?: ...;                          // per-API compatibility overrides
}
```
[src: ai/src/types.ts:825–872]

Usage record (what providers fill per response) — note cost is embedded *in*
usage:

```ts
interface Usage {
  input: number; output: number; cacheRead: number; cacheWrite: number;
  cacheWrite1h?: number;   // subset of cacheWrite at 1h retention; Anthropic-only split
  reasoning?: number;      // subset of output, when reported
  totalTokens: number;
  cost: { input: number; output: number; cacheRead: number; cacheWrite: number; total: number };
}
```
[src: ai/src/types.ts:383–404]

### 3.2 Cost accounting — exactly how it consumes the table

`calculateCost(model, usage)` is called by **each provider implementation
at stream-end** (e.g. `anthropic-messages.ts:616,778`), mutating
`usage.cost` in place [src: ai/src/models.ts:891–911]:

1. **Tier selection**: `inputTokens = usage.input + usage.cacheRead +
   usage.cacheWrite`; scan `cost.tiers`, pick the tier with the highest
   `inputTokensAbove` that `inputTokens` exceeds; else base rates. The
   chosen rates apply to the **whole request** (no marginal/blended
   pricing).
2. **1h-cache-write special case**: `shortWrite = cacheWrite -
   (cacheWrite1h ?? 0)`; cost.cacheWrite = `(rates.cacheWrite * shortWrite
   + rates.input * 2 * longWrite) / 1e6` — "Anthropic charges 2x base input
   for 1h cache writes".
3. Everything else is `rate/1e6 * tokens`; `total` is the sum.

Per-session accounting simply **sums `usage.cost.total` over persisted
entries**: `UsageTotals {input, output, cacheRead, cacheWrite, cost}` +
`addUsageToTotals`; the cost breakdown groups assistant usage by
`provider/model` and buckets compaction/branch-summary/tool-summary usage
under a `"Tools/summaries"` key — i.e. **summarization spend is accounted,
attached to the compaction/branch_summary entries' own `usage` field**
[src: coding-agent/src/core/usage-totals.ts:1–62].

Thinking levels: seven symbolic levels
`off/minimal/low/medium/high/xhigh/max`; `getSupportedThinkingLevels`
filters by `thinkingLevelMap[level] === null` (unsupported) and requires
explicit mapping for `xhigh`/`max`; `clampThinkingLevel` picks the nearest
supported level upward-then-downward [src: ai/src/models.ts:913–945].

`contextWindow` default 128000 / `maxTokens` default 16384 apply to
*user-defined* models in `models.json` (docs claim, consistent with
generated catalog carrying explicit values per model; the defaults
themselves live in the models-store parsing path — not re-verified line-by-
line: INFERRED-minor).

**Tradewind mapping sketch.** Pure domain data: an optional `ModelMeta`
(context_window, max_tokens, cost table with tiers) on `ModelSpec`
(`domain/models.py:75`, per tier in `Profile.models`). Consumption points:
(a) the langchain adapter (`adapters/langchain_backend.py`) computes
`TurnResult.cost_usd` (`domain/models.py`, turns table) from the token
usage it already receives — port Pi's `calculateCost` including
whole-request tier selection; skip the `cacheWrite1h` 2x rule unless/until
tradewind surfaces 1h cache writes (langchain usage doesn't split them —
document the omission rather than guessing); (b) the compaction trigger
(feature 1) reads `context_window - reserve_tokens` in the turn runner.
Absent metadata → `cost_usd` stays `None` and auto-compaction stays off —
both honest per NFR-3. Backends that already report cost (claude) keep
their reported number; never overwrite a reported cost with a computed one
(reported is ground truth). The `thinkingLevelMap`-null pattern is the
precedent for a per-tier "effort unsupported" marker if `ModelSpec.effort`
ever meets a model that lacks it.

---

## 4. Tool-gate verdicts (extension `tool_call`)

Files: `packages/coding-agent/src/core/extensions/types.ts` (API),
`extensions/runner.ts` (dispatch), `core/agent-session.ts:486–540` (wiring),
`packages/agent/src/agent-loop.ts` (verdict application).

### 4.1 The verdict type

```ts
interface ToolCallEventResult {
  /** Block tool execution. To modify arguments, mutate event.input in place instead. */
  block?: boolean;
  reason?: string;
  /** Hint that the agent should stop after the current tool batch when this call is blocked.
   *  Early termination only happens when every finalized tool result in the batch sets this to true. */
  terminate?: boolean;
}
```
[src: extensions/types.ts:1125–1134]

The event is a discriminated union per built-in tool
(`{type:"tool_call", toolCallId, toolName, input}` with typed `input` for
bash/read/edit/write/grep/find/ls/powershell, plus a
`CustomToolCallEvent` with `Record<string, unknown>` input) [src:
extensions/types.ts:889–954].

### 4.2 Dispatch semantics (multiple extensions)

`ExtensionRunner.emitToolCall`: handlers run **in extension order, then
handler order**; the first result with `block: true` **short-circuits** and
is returned immediately; otherwise the *last* non-undefined result wins
[src: extensions/runner.ts:982–1003]. Unlike most other events, handler
**exceptions are not swallowed** here: they propagate, and the session
wrapper converts a non-Error throw to
`Error("Extension failed, blocking execution: …")` — a crashing gate
**fails closed** [src: core/agent-session.ts:493–505; the throw is caught in
`prepareToolCall`'s try/catch and becomes an error tool result].

### 4.3 Input rewriting

`event.input` **is the same object** that will be executed: the session
passes the already-validated `args` reference into the event
(`input: args as Record<string, unknown>`), and the loop executes
`prepared.args` — so in-place mutation propagates with **no re-validation**
("Later `tool_call` handlers see earlier mutations. No re-validation is
performed after mutation") [src: extensions/types.ts:939–944;
core/agent-session.ts:494–499; agent-loop.ts:607–667]. Order of operations
per call: `tool.prepareArguments` (tool-supplied normalization) →
`validateToolArguments` (schema) → `beforeToolCall` hook (gate + mutation)
→ execute. There is no "return replacement input" variant — mutation is the
only rewriting mechanism.

### 4.4 What block does — how the reason reaches the model

In `prepareToolCall` [src: agent-loop.ts:626–654]:

```ts
if (beforeResult?.block) {
  const result = createErrorToolResult(beforeResult.reason || "Tool execution was blocked");
  if (beforeResult.terminate === true) result.terminate = true;
  return { kind: "immediate", result, isError: true };
}
```

So the reason becomes the **text of a synthetic error tool result** for
that `toolCallId` — the model reads it exactly like any failed tool call in
the next request. Default text when no reason:
`"Tool execution was blocked"`. The blocked result still flows through the
`tool_result` extension event and is persisted/mirrored normally.

### 4.5 What terminate does mid-turn

`AgentToolResult.terminate` participates in a **batch-unanimity rule**:
`shouldTerminateToolBatch = finalizedCalls.length > 0 &&
finalizedCalls.every(f => f.result.terminate === true)` [src:
agent-loop.ts:589–591]. In the main loop, `hasMoreToolCalls =
!executedToolBatch.terminate` [src: agent-loop.ts:235] — meaning: the tool
results (including the synthesized error results) are **still appended to
context and persisted**, `turn_end` still fires, but the loop does **not**
send another model request; the agent run ends after the current turn
(`agent_end`). It is not an abort — nothing in-flight is cancelled by the
flag itself; parallel/other tool calls in the same batch run to completion,
and if even one of them doesn't set `terminate`, the turn continues
(the every() rule). `afterToolCall`/`tool_result` handlers may also
set/override `terminate` on any result [src: agent-loop.ts:744–752;
types.ts:68–94, 275–288].

**Tradewind mapping sketch.** Extends the broker port
(`domain/models.py:118` `Verdict = Literal["allow","deny"]`,
`PermissionBroker.decide`). Shape:
`Verdict = Allow | Deny(reason: str | None = None, terminate: bool = False)`
(or keep the Literal and add a richer `BrokerDecision` dataclass — either
way `decide()`'s return widens, touching FR-4 and every backend gate).
Deny-with-reason is portable to all four backends because the deny path is
tradewind-owned everywhere: the reason string becomes the error
tool-result text the model sees (Pi's exact mechanism; tradewind already
synthesizes such a result per README §Permission broker), and rides the
existing `PermissionRequested` event unchanged plus the mirrored
`tool_result` item. `terminate` maps to the turn runner invoking the
existing `interrupt()` path *after* the deny result is mirrored — end
result `TurnResult.status="interrupted"`; whether that deserves its own
`EndReason` member (e.g. `"terminated_by_broker"` vs reusing
`"interrupted"`) is a small honesty decision touching the frozen
`EndReason` literal (`domain/models.py`) → P-decision. Pi's batch-unanimity
rule is only meaningful with parallel tool calls; tradewind gates calls
one-at-a-time, so terminate-on-first-deny is the simpler faithful mapping.
Input rewriting: adopt only behind `supports_input_rewrite` (maps to
Claude's `can_use_tool` updated-input and tradewind's own loops; not to
Codex approvals), per the survey's honest-capabilities argument. Also copy
Pi's fail-closed rule: a crashing broker must deny, not allow.

---

## 5. Branch summary on fork

Files: `packages/coding-agent/src/core/compaction/branch-summarization.ts`,
`core/agent-session.ts:3100–3300` (`navigateTree`),
`core/session-manager.ts:1395–1420` (`branchWithSummary`).

### 5.1 When

On **in-file tree navigation** (`/tree`, `navigateTree(targetId, {summarize,
customInstructions, replaceInstructions, label})`) — i.e. when abandoning
the current leaf's branch to continue from an earlier point. It is
**opt-in per navigation** (`options.summarize`; interactive mode asks
"Summarize branch?" unless `branchSummary.skipPrompt` is set). Not during
streaming (throws). `/fork`-to-new-file (`createBranchedSession`) copies a
path verbatim and does **not** summarize [src:
core/agent-session.ts:3113–3131; core/settings-manager.ts:19–22, 858–866;
core/session-manager.ts:1427+].

### 5.2 What is summarized

`collectEntriesForBranchSummary(session, oldLeafId, targetId)`: compute the
**deepest common ancestor** of the old leaf's path and the target's path
(set intersection over the root-first branch paths), then collect entries
walking from the old leaf back to (excluding) that ancestor; reverse to
chronological. Compaction entries along the abandoned branch are
**included** — "those are included and their summaries become context"
[src: branch-summarization.ts:96–146].

`prepareBranchEntries(entries, tokenBudget)` with `tokenBudget =
model.contextWindow - reserveTokens` (reserveTokens from
`branchSummary.reserveTokens`, default 16384): two passes. Pass 1 harvests
cumulative file-ops from *nested* pi-generated `branch_summary` entries'
`details` (so re-branching keeps the cumulative read/modified lists). Pass
2 walks **newest→oldest** adding messages until the budget; a
compaction/branch_summary entry that would overflow is squeezed in anyway
if under 90% of budget ("important context"). `toolResult` messages are
skipped entirely ("context is in assistant's tool call") [src:
branch-summarization.ts:156–247].

### 5.3 Generation

Same serialization + system prompt as compaction
(`serializeConversation`, `SUMMARIZATION_SYSTEM_PROMPT`), instruction block
`BRANCH_SUMMARY_PROMPT` (same structured format minus Critical Context;
`customInstructions` appends `Additional focus:`, or *replaces* the prompt
when `replaceInstructions`); output budget `min(4096, model.maxTokens)` —
notably smaller than compaction's; same `completeSummarization` retry choke
point; abort → `{aborted:true}` (navigation cancelled), error/length →
error (navigation aborted, honest). The final text is prefixed with a
preamble *baked into the summary string itself*: "The user explored a
different conversation branch before returning here.\nSummary of that
exploration:\n\n", then the `<read-files>`/`<modified-files>` blocks are
appended [src: branch-summarization.ts:253–382].

### 5.4 How the entry is attached

`branchWithSummary(branchFromId, summary, details, fromHook, usage)`:
records `fromId = current leaf ?? "root"` (**the divergence point being
abandoned**), moves the leaf to the navigation target, and appends:

```ts
interface BranchSummaryEntry<T = unknown> extends SessionEntryBase {
  type: "branch_summary";
  fromId: string;        // old leaf (tip of the abandoned branch)
  summary: string;
  details?: T;           // pi default: { readFiles, modifiedFiles }
  usage?: Usage;         // summarization LLM usage
  fromHook?: boolean;    // extension-generated?
}
```

as a **child of the new position** (`parentId = branchFromId`), so the new
branch grows from the summary entry [src: core/session-manager.ts:82–92,
1395–1420]. Position subtlety: when the target is a *user* message, the new
leaf is that message's **parent** and the message text is returned as
`editorText` for re-editing; otherwise the target itself [src:
core/agent-session.ts:3240–3251]. A `label` option attaches a label entry
to the summary. Extensions can cancel or supply the summary
(`session_before_tree` → `session_tree`). In context, the entry renders as
a user message: "The following is a summary of a branch that this
conversation came back from:\n\n<summary>\n" + summary + "</summary>"
[src: core/messages.ts:19–24, 170–175].

**Tradewind mapping sketch.** Tradewind's fork is `copy_history(src,
dst_row, up_to_seq)` (`application/ports.py:205`) creating a *separate
session row* (DR-4), so Pi's shape inverts: there is no "abandoned branch"
— the parent session keeps everything past `up_to_seq`. Two useful
mappings: (a) *summarized fork* — `fork(summarize=True)`: instead of
copying the full prefix, the child session gets a single
`kind="compaction"`-style summary message (same summarizer as feature 1,
Pi's 4096-token budget and newest-first budget walk) followed by nothing,
or summary + last-N verbatim; needs a new store affordance beside
`copy_history` (write-one-synthetic-message + row creation) — an
application-layer function over existing port methods, no port change
strictly required. (b) *return-from-branch summary* — when a caller
resumes a parent session after a fork/subagent finished, prepend the
child's transcript summary (the `history_scope="tree"` fold already
positions child transcripts; a summarized variant is the token-bounded
version of that same fold). The record itself: a `NormalizedMessage`
(mirrored via `ItemCompleted`) with kind `"compaction"` or a new
`"branch_summary"` literal — a new `Kind` member touches the frozen literal
in `domain/models.py:29` → P-decision; reusing `"compaction"` with a
`raw`-field discriminator avoids that at some honesty cost. Pi's `fromId`
maps to tradewind's existing `parent_session_id` +
`spawned_by_message_id` on `SessionRow` — no new linkage needed.

---

## Honest gaps (would need original design)

Everything above is source-verified; these are the places where **Pi's
answer does not transfer** and tradewind must design its own:

1. **Cross-engine cut points.** Pi cuts over *its own* uniform
   `AgentMessage` roles. Tradewind's mirror rows come from four engines
   with engine-specific kinds (`command_execution`, `file_change`, `plan`,
   `web_search`) that Pi has no analogue for. Which of those are valid cut
   points / turn starts (and whether e.g. a `command_execution` binds to a
   preceding item the way `tool_result` binds to `tool_use`) is an original
   taxonomy decision per backend.
2. **Retry classification without message strings.** Pi's retryable/
   non-retryable split is regex over provider error text — workable but
   fragile (their own file carries 6 issue-number comments patching missed
   patterns). Tradewind's SDK backends surface *typed* errors; mapping each
   backend's error taxonomy to retryable/terminal is original work per
   adapter, with Pi's pattern lists useful only as the langchain fallback
   and as a checklist of transient classes.
3. **Retry safety on native-resume backends.** Pi retries by re-running the
   last LLM call over context it fully owns. Whether re-prompting a
   claude/codex/cursor session after a mid-turn failure duplicates
   tool side effects is a per-engine question Pi never faces; the
   `supports_turn_retry` flag semantics need original definition.
4. **`seq`-based boundary vs entry-id.** `firstKeptEntryId` assumes stable
   per-entry ids in a tree tradewind doesn't have; using message `seq` as
   the kept-boundary works for the flat mirror but its interaction with
   `up_to_seq` forks and with REPLAY reconstruction (does a fork copy the
   compaction record? Pi's `createBranchedSession` re-parents it and
   remaps `firstKeptEntryId` — tradewind's analogue is unspecified) needs
   design.
5. **Where summarizer spend lands.** Pi attaches summarization `usage` to
   the compaction/branch_summary entry and buckets it as
   "Tools/summaries". Tradewind's cost lives on *turns* (`TurnResult.
   cost_usd`); a compaction that runs between turns has no turn row to
   charge. Options (synthetic turn row; a column on the compaction message;
   a separate ledger) are an original schema decision.
6. **Event taxonomy additions.** `compaction_start/end`,
   `auto_retry_start/end`, and any `branch_summary` kind all touch frozen
   surfaces (ARCH §3.2 events, `Kind` literal, `EndReason`). Pi tells us
   the *payloads* that proved useful; the P-decisions on whether/how to
   extend are tradewind's own.
7. **Minor unverified details**: the `models.json` user-model defaults
   (contextWindow 128000 / maxTokens 16384) were taken from the docs and
   the type's doc-comment, not traced through the models-store parsing
   code; Pi's RPC-mode projection of these events was not re-verified
   against `modes/rpc/rpc-mode.ts` (the core event shapes above are the
   source of truth it serializes).
