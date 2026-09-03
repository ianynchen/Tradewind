# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.7.0] - 2026-09-03

### Added

- Model metadata (FR-10.5): optional `ModelMeta` on `ModelSpec`
  (`context_window`, `max_tokens`, `ModelCost` table in $/Mtok with
  whole-request pricing tiers) plus `calculate_cost`. `TurnResult.cost_usd`
  is now computed from token usage on `langchain`/`codex`/`cursor` when a
  cost table exists — on all auth modes; a subscription profile's figure is
  the API-equivalent price, not billed spend. Claude's reported cost is
  never overwritten. Absent metadata keeps `cost_usd` at `None`.
- Mirror compaction (FR-5.8, design ported from Pi, MIT): on mirror-fed
  paths (`langchain`), older transcript is summarized into a structured
  checkpoint recorded as a `kind="compaction"` mirror message and later
  requests rebuild from `[checkpoint + retained tail]` — cut points never
  split a tool call from its result, mid-turn cuts summarize the turn
  prefix separately (exact `turn_id` detection), and repeated compactions
  chain through `<previous-summary>`. Automatic triggering is doubly gated
  (`CompactionSettings.auto` AND the tier's `context_window`; conservative
  chars/4 estimate); `Session.compact(instructions)` compacts manually,
  metadata-free, and raises `Unsupported` on native-resume backends. A
  truncated summary is a hard failure (`CompactionFailed` / a loud event
  item), never a checkpoint. The mirror keeps every row; forks copy
  verbatim history without compaction records.

## [0.6.0] - 2026-09-03

### Added

- Per-call `history_scope` on `Session.run()`/`stream()` (FR-9.3):
  `"none" | "flat" | "tree"`. `"tree"` feeds descendant-session transcripts
  into the parent's context as wrapped blocks positioned after the spawning
  turn; defaults are per-backend and truthful (`"flat"` on langchain,
  `"none"` on native-resume backends, where an explicit `"flat"`/`"tree"`
  raises `Unsupported`).

### Changed

- **Breaking:** `TurnContext.load_history` is now an async, lazy closure
  (`Callable[[], Awaitable[list[StoredMessage]]]`) shaped by the turn's
  `history_scope`. The turn runner no longer reads the session's history
  eagerly on every turn — native-resume backends (claude/codex/cursor) now
  issue zero history queries per turn; langchain reads once, on demand.
  Custom `Backend` implementations must `await ctx.load_history()`.

## [0.5.0] - 2026-09-03

### Added

- `Tradewind(config, backend_factories=...)` (ADR-0002): keyword-only,
  per-instance backend-factory overrides consulted before the module
  registry — the public injection seam for embedders' no-network tests
  (previously only reachable by monkeypatching a private map).

### Fixed

- Langchain backend: `run()` no longer holds an `anyio.CancelScope` open
  across `yield` (the documented anyio async-generator pitfall). Abandoning
  the stream mid-turn — e.g. `Session.run()` raising on a `TurnFailed` —
  made asyncio's async-generator finalizer close the scope from its own
  task and blew up with "Attempted to exit cancel scope in a different task
  than it was entered in", failing otherwise-working turns. The turn now
  runs in a dedicated pump task (scope entered and exited in that one task)
  streaming events out through a memory channel.
- Langchain backend: a chat model without `bind_tools` support plus
  registered tools now fails loudly with a message naming the model and the
  operation (FR-1.2), instead of a `TurnFailed` whose message was the empty
  `str(NotImplementedError())`.
- `tradewind.__version__` is synced with `pyproject.toml` (it had been left
  at 0.1.0 through the 0.2.0/0.3.0 bumps).

## [0.4.0] - 2026-09-02

### Added

- Optional persistence (FR-5.7, ADR-0001): `TradewindConfig.store` may now
  be omitted — the mirror becomes an ephemeral in-memory sqlite database,
  private to the instance and gone at exit. Turns, history (so langchain
  keeps conversation context within the process), single-flight, and
  reconcile behave identically; SDK backends still persist natively.

### Changed

- **Breaking (ADR-0001):** `StoreConfig` is removed. `TradewindConfig.store`
  is one union field: `Path | SessionStorePort | None = None` — a path for
  the default sqlite engine (was `StoreConfig(sqlite_path=...)`), a
  caller-built store (was `StoreConfig(store=...)`), or `None` for the
  ephemeral mirror. The both-set error state is now unrepresentable.

## [0.3.0] - 2026-09-02

### Added

- `TurnResult.end_reason` (FR-6.5): a required, machine-readable statement
  of why the turn ended — `end_turn` | `max_tokens` | `max_tool_rounds` |
  `interrupted` — so truncated output can no longer be mistaken for a clean
  finish. All four adapters populate it. **Breaking:** `end_reason` and
  `Capabilities.supports_tool_round_cap` are required at construction.
- Per-call `max_tool_rounds` option on `Session.run()`/`stream()` (FR-6.5):
  caps tool-execution rounds on backends that can enforce it honestly
  (langchain: its own loop; claude: native `max_turns`); hitting the cap
  completes the turn as an honest partial (`end_reason="max_tool_rounds"`).
  Codex/Cursor expose no cap surface and raise `Unsupported` when it is set,
  declared via the new `Capabilities.supports_tool_round_cap` flag (FR-8.1).
- On the langchain backend, a response the provider truncated at
  `max_tokens` now completes with `end_reason="max_tokens"` and its
  (possibly truncated) tool calls are not executed.

## [0.2.0] - 2026-09-02

### Changed

- Lowered the supported Python floor from ≥3.13 to ≥3.12 so downstream
  projects declaring `requires-python = ">=3.12"` (e.g. sextant) can resolve
  tradewind. No source changes were needed; the full suite, mypy strict,
  ruff, and the import-linter contracts pass on both 3.12 and 3.13.
