# Compaction observability across all four backends

Date: 2026-09-03 · Status: APPROVED (user, 2026-09-03) · Builds on: FR-5.8
(tradewind's own compaction, Phase 1), ADR-0003 (migrations).

## Problem

When an SDK engine compacts its own context, tradewind has no idea. The
mirror believes it holds the whole conversation while the engine has
silently summarized part of its own away. Today only `langchain` — where
tradewind performs the compaction itself — produces a `kind="compaction"`
record; `claude`, `codex`, and `cursor` produce nothing.

That is an observability gap, not a correctness one: the mirror keeps every
row, so a later REPLAY is *richer* than the engine's compacted state. What
is missing is the marker saying **the engine's context diverged from the
mirror from here on** — which matters when debugging why an engine behaved
as if it had forgotten something the mirror still holds.

## Decision: ride the surface that already exists

No new callback. Compaction is surfaced as an ordinary
`ItemCompleted(NormalizedMessage(kind="compaction"))`, exactly as
tradewind's own compaction has been since Phase 1. That means one observer
handler works on every backend, the record is persisted by the turn runner
for free, and the frozen event taxonomy (ARCH §3.2) grows by nothing.

Observers listen in any of three ways, all the same event:

```python
# 1. config-level tap, every session on the instance
def on_event(event):
    if isinstance(event, ItemCompleted) and event.message.kind == "compaction":
        ...

# 2. per-session stream
async for event in session.stream(prompt): ...

# 3. after the fact -- the record is mirrored
[m for m in await tw.history(sid) if m.kind == "compaction"]
```

## What each engine actually gives us (source-verified)

| Backend | Signal | Summary text |
|---|---|---|
| `langchain` | tradewind compacts itself (FR-5.8) | yes — full record |
| `cursor` | `summary-started` → `summary` → `summary-completed` on `RunStreamEvent.interaction_update` | **yes — `SummaryUpdate.summary`** |
| `claude` | `PreCompact` hook (`trigger`, `custom_instructions`) | no — fires *before* compaction |
| `codex` | `ContextCompactedNotification {thread_id, turn_id}` | no — its `CompactionResponseItem` carries `encrypted_content` |

Cursor's summary IS its compaction, verified three ways: its persisted
conversation model carries `summary`, `summary_archive`, `summary_archives`,
`self_summary_count`, and `message_count_at_last_compaction` in one message;
the three events form a started/produced/completed lifecycle; and the SDK
explicitly excludes all three from the conversation delta flow and from
step-completion logic, i.e. they are operations *on* the conversation, not
content within it.

## Record contents — only what the caller cannot derive

No `source` field and no `backend` field (user decision, 2026-09-03). The
caller knows it is talking to tradewind, and knows which backend it
configured: with `session.stream()` it knows the session, and with
`tw.history(session_id)` it queried by session, so the profile and backend
follow either way. The one surface where it does not follow — the
config-level `on_event` tap, since `ItemCompleted` carries no session or
turn id and one instance may hold profiles on different backends — is a
GENERAL correlation gap affecting every event kind equally. Papering over it
for compaction alone would be incoherent; the real fix, if wanted, is
correlation on the event envelope, tracked separately.

| Backend | `content` | `native_id` |
|---|---|---|
| `langchain` | `summary`, `first_kept_seq`, `tokens_before`, `summarizer_usage`, `summarizer_cost_usd` (**unchanged**) | — |
| `cursor` | `{"summary": str}` | — |
| `claude` | `{"trigger": "manual" \| "auto"}` | — |
| `codex` | `{}` | the engine's native turn id |

Because nothing is added to the existing langchain shape, this is **not** a
content-shape change: `CONTENT_SHAPE_VERSION` stays 1 and no shape
migration is needed (FR-5.9).

Success/failure is deliberately NOT recorded. Cursor's wire protocol has a
`failed` flag on `summary-completed`, but the Python SDK's parser discards
the payload (`SummaryCompletedUpdate(type=...)` only), so it is unreachable
through the typed API; claude's hook fires before the outcome exists. We
record that compaction happened and do not pretend to know how it went.

## Wiring

- **cursor**: `_consume` already iterates `RunStreamEvent`s and reads only
  `event.sdk_message`; add a branch on `event.interaction_update` for
  `SummaryUpdate`. `summary-started`/`summary-completed` are ignored — one
  record per compaction, emitted when the text arrives.
- **claude**: register a `PreCompact` hook in `ClaudeAgentOptions.hooks` and
  append to the same `pending_events` list `_make_can_use_tool` already
  uses. The record honestly means "the engine is about to compact".
- **codex**: one more branch in `_drive_turn`'s notification dispatch,
  alongside `ThreadTokenUsageUpdatedNotification`.

Role is `"assistant"` for engine records (the engine did it), matching the
existing `resume_degraded` event-item precedent.

## Verification

Unit: cursor emits one record carrying the real summary text and ignores
the started/completed bookends; claude's hook produces a record with its
trigger; codex's notification produces a record carrying the native turn id;
each backend's record is mirrored and reaches both `on_event` and the
session stream. Regression: langchain's existing record shape is untouched,
and `CONTENT_SHAPE_VERSION` remains 1.

Live verification of cursor's events is an ACCEPTANCE CONDITION deferred to
the P-5 cursor live-verification work, not claimed here: the cursor findings
are read from a vendored minified bundle and proto field names, and cursor
is the one backend never exercised live (ARCH P-5). The adapter must
therefore treat absence of these events as normal, never as an error.

## Out of scope

Configuring engine compaction thresholds (codex exposes
`model_auto_compact_token_limit` via `config_overrides`; claude and cursor
expose nothing) — user decision: not worth a knob. Event-envelope
correlation ids. Cursor's `failed` flag.
