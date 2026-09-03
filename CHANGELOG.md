# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `TurnResult.end_reason` (FR-6.5): a required, machine-readable statement
  of why the turn ended — `end_turn` | `max_tokens` | `max_tool_rounds` |
  `interrupted` — so truncated output can no longer be mistaken for a clean
  finish. All four adapters populate it.
- Per-call `max_tool_rounds` option on `Session.run()`/`stream()` (FR-6.5):
  caps tool-execution rounds on backends that can enforce it honestly
  (langchain: its own loop; claude: native `max_turns`); hitting the cap
  completes the turn as an honest partial (`end_reason="max_tool_rounds"`).
  Codex/Cursor expose no cap surface and raise `Unsupported` when it is set,
  declared via the new `Capabilities.supports_tool_round_cap` flag (FR-8.1).
- On the langchain backend, a response the provider truncated at
  `max_tokens` now completes with `end_reason="max_tokens"` and its
  (possibly truncated) tool calls are not executed.

### Changed

- Lowered the supported Python floor from ≥3.13 to ≥3.12 so downstream
  projects declaring `requires-python = ">=3.12"` (e.g. sextant) can resolve
  tradewind. No source changes were needed; the full suite, mypy strict,
  ruff, and the import-linter contracts pass on both 3.12 and 3.13.
