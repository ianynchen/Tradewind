# Web Access, Slice 2 (per backend) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn web access on per backend: Claude's own search **gated**, Codex's own search **observed** with its observe mapping and engine setting, Tradewind's fetch tool **gated** on Claude, Codex and LangChain; live tests for Claude and Codex; release 0.16.0.

**Architecture:**
- The runner passes the session's stored web setting to the backend in `TurnContext.web`. Each adapter reads it there: Claude turns on its `WebSearch` tool and maps it to `web_search` on the gate path; Codex passes `web_search` to its engine and maps its web items to `web_search` and `fetch_url` on the observe path.
- Each backend reports its modes in `capabilities()`; slice 1's support check and per-kind floor already act on them.
- Cursor stays `None`/`None` (07 owner decision 5).

**Tech Stack:** Python 3.12+, the existing adapters, runner, policy gate and `openai_codex` / `claude_agent_sdk` packages. No new dependency.

**Spec:** `docs/components/07-web-access.md` (owner decisions 1–13; §4 and §4a, slice 2), built on slice 1 (0.15.0, ADR-0006).

## Global Constraints

- GUIDELINES.md §1–4, §7, §8, §10, §11 and §13.5 bind. No CI change.
- No new dependency.
- With no web setting (`web=None`), every backend behaves exactly as today: Claude keeps `tools=[]`; Codex gets no `web_search` override, so it keeps its own default search, recorded (owner decision 11); no web floor rule; no fetch tool.
- Exact modes: Claude `web_search="gated"`, `fetch_url="gated"`; Codex `web_search="observed"`, `fetch_url="gated"`; LangChain `web_search=None`, `fetch_url="gated"`; Cursor `None`, `None`.
- Codex engine setting, as `--config` overrides like the shim's: `web=None` → none; `web.search` on → `web_search="live"`; a web setting with search off → `web_search="disabled"`. Nothing else in Codex's setup changes.
- Mapping: Claude `WebSearch` → `web_search` (`query`); Codex `webSearch` action `search` → `web_search` (`query`; a `queries` list gives one action per query, in list order, the first stop ends the turn), `openPage` and `findInPage` → `fetch_url` (`url`); no action, an unknown action, a search with no query, or a page action with no `url` → `missing_critical_field` (owner decision 13).
- Live tests: one file, `tests/integration/test_web_live.py`, marked `integration`, skipped unless `TRADEWIND_RUN_WEB_INTEGRATION=1`; never in CI. Implementers never run them; the controller runs them once with the owner's standing authorization to use the subscriptions for tests. Never `--dangerously-skip-permissions`; never read `~/.config/tradewind/live-tests.env`.
- Gate: `bash scripts/check.sh > log 2>&1 && git commit …` — never `;`, never a pipe on the gate.
- Note for callers (docs): Codex opens pages as part of its own search (`openPage`), which maps to `fetch_url`; a Codex session that searches needs `fetch=True` too, or the floor stops the turn at the first opened page.

## Review Focus

- `web=None` on Claude must keep `tools=[]` and on Codex must add no `web_search` override, so plain turns are unchanged (Tasks 2 and 4).
- A Claude `WebSearch` call with no `query` (or a non-string one) must be a missing critical field, denied by the floor (Task 2).
- A Codex item whose `queries` list holds an allowed query and then a stopped one must stop the turn at the second, with both recorded (Task 3).
- A Codex `openPage` with no `url`, or an action type the generated model does not know, must stop the turn as a missing critical field (Task 3).
- A Codex session with `WebAccess(search=True, fetch=False)` that opens a page stops at the floor's `fetch_url_off` rule, and the docs say why (Tasks 3 and 6).

---

### Task 1: Pass the web setting to the backend

**Files:**
- Modify: `src/tradewind/application/ports.py` (`TurnContext`)
- Modify: `src/tradewind/application/turn_runner.py` (the `TurnContext(...)` construction)
- Test: `tests/unit/test_governance_fake_backend.py`

**Interfaces:**
- Produces: `TurnContext.web: WebAccess | None = None`, set by the runner to the stored setting (`web_from_snapshot(snapshot)`, already computed for the floor).

- [ ] **Step 1:** failing tests: with `web=WebAccess(search=True, fetch=True)` on a fake that offers both, `fake.contexts[-1].web == WebAccess(search=True, fetch=True)`; with `web=None`, `fake.contexts[-1].web is None`.
- [ ] **Step 2:** run them; they fail. **Step 3:** implement. **Step 4:** `uv run pytest tests/unit -q`; PASS. **Step 5:** commit after the gate (`feat(web): pass the web setting to the backend`).

### Task 2: Claude — gated search, gated fetch; LangChain — gated fetch

**Files:**
- Modify: `src/tradewind/adapters/claude_backend.py` (`capabilities`; the `ClaudeAgentOptions(tools=…)` construction; `_make_can_use_tool`)
- Modify: `src/tradewind/adapters/langchain_backend.py` (`capabilities`)
- Test: the existing Claude and LangChain backend unit tests (`tests/unit/test_claude_*.py`, `tests/unit/test_langchain_*.py`)

**Interfaces:**
- Consumes: `TurnContext.web` (Task 1).
- Produces: `claude_web_search_action(tool_input: dict[str, object]) -> Action` in `claude_backend.py`: `kind="web_search"`, `fields={"query": tool_input.get("query")}`, `backend="claude"`, `native_name="WebSearch"`, `native_args=tool_input`, `summary` the query, `missing_critical_field=True` when the query is missing or not a `str`.

- [ ] **Step 1:** failing tests:
  - Claude `capabilities()` has `web_search == "gated"` and `fetch_url == "gated"`; LangChain has `web_search is None` and `fetch_url == "gated"`;
  - the options built for a turn have `tools == []` with `ctx.web=None` and with `WebAccess(search=False, fetch=True)`, and `tools == ["WebSearch"]` with `WebAccess(search=True, fetch=False)`;
  - `can_use_tool("WebSearch", {"query": "q"}, …)` gates an `Action` of kind `web_search` with `fields == {"query": "q"}`; a rule that allows `query == "q"` lets it through and a deny stops it;
  - `can_use_tool("WebSearch", {}, …)` gates an action with `missing_critical_field`, which the floor denies;
  - `StructuredOutput` and caller tools are gated exactly as before.
- [ ] **Step 2:** run them; they fail. **Step 3:** implement. **Step 4:** `uv run pytest tests/unit -q`; PASS. **Step 5:** commit after the gate (`feat(claude): gated web search; gated fetch on Claude and LangChain`).

### Task 3: Codex — the observe mapping for web items

**Files:**
- Modify: `src/tradewind/adapters/codex_backend.py` (`observed_action` → `observed_actions`; `_observe`)
- Test: `tests/unit/test_codex_mapping.py`, `tests/unit/test_codex_backend.py` (or where `_observe` is tested today)

**Interfaces:**
- Produces: `observed_actions(item: ThreadItem, *, workspace: str | None) -> list[Action]`: today's command and file-change actions as one-item lists; `[]` for items that are not observed; for a `webSearch` item the actions of the mapping in Global Constraints. `_observe` evaluates them in order and returns the first stop.

- [ ] **Step 1:** failing tests, with `WebSearchThreadItem` values shaped as in 07 §4a:
  - a search with `query` → `[web_search {query}]`;
  - a search with a `queries` list of two → two `web_search` actions in list order;
  - `openPage` and `findInPage` → `[fetch_url {url}]`;
  - `action=None`, a search with neither `query` nor `queries`, a page action with no `url`, and an action of an unknown type (build it from the generated model's catch-all variant if it has one; otherwise from a raw dict through `ThreadItem.model_validate`) → one action with `missing_critical_field`;
  - commands and file changes map exactly as before;
  - through `_observe` with a gate whose caller rules allow `query == "a"` and stop any other `web_search`: an item with `queries ["a", "b"]` stops at `"b"`.
- [ ] **Step 2:** run them; they fail. **Step 3:** implement. **Step 4:** `uv run pytest tests/unit -q`; PASS. **Step 5:** commit after the gate (`feat(codex): observe web items as web_search and fetch_url`).

### Task 4: Codex — the engine setting and the modes

**Files:**
- Modify: `src/tradewind/adapters/codex_backend.py` (`capabilities`; where `_engine_config(config_overrides)` is called for a turn)
- Test: `tests/unit/test_codex_backend.py`

**Interfaces:**
- Consumes: `TurnContext.web` (Task 1).
- Produces: `web_search_overrides(web: WebAccess | None) -> tuple[str, ...]`: `()` for `None`; `('web_search="live"',)` when `web.search`; `('web_search="disabled"',)` for a setting with search off. The turn's overrides are the shim's plus these.

- [ ] **Step 1:** failing tests: `capabilities()` has `web_search == "observed"` and `fetch_url == "gated"`; `web_search_overrides` for the three cases; a turn's `CodexConfig.config_overrides` contain the web override after the shim's (use the backend's existing test seam for the engine config).
- [ ] **Step 2:** run them; they fail. **Step 3:** implement. **Step 4:** `uv run pytest tests/unit -q`; PASS. **Step 5:** commit after the gate (`feat(codex): observed web search and its engine setting`).

### Task 5: Live tests

**Files:**
- Create: `tests/integration/test_web_live.py`

**Interfaces:**
- Consumes: Tasks 1–4.

- [ ] **Step 1:** write the tests, each against a real engine through `Tradewind` (follow `tests/conformance/test_claude.py` and `test_codex.py` for profiles: `SubscriptionAuth`, the model ids they use):
  - `test_claude_gated_search_runs_the_approved_query`: `web=WebAccess(search=True, fetch=False)`, a caller rule that allows `web_search` with `query` matching the prompt's instructed query, the default denies others; the turn completes and a `web_search` decision `allow` is recorded;
  - `test_claude_gated_search_denied`: a caller rule that denies every `web_search`; the turn completes, and no search result is recorded;
  - `test_claude_fetch_tool`: `web=WebAccess(search=False, fetch=True)`, a rule that allows `fetch_url` for `https://example.com`; the recorded tool result contains `"Example Domain"`;
  - `test_codex_observed_search_is_recorded`: `web=WebAccess(search=True, fetch=True, observed=True)`; the turn completes and at least one `web_search` item with a non-empty `query` is recorded;
  - `test_codex_observed_search_can_be_stopped`: the same with a caller rule that stops every `web_search`; the turn ends with the policy stop.
- [ ] **Step 2:** run the gate (the file must skip without the variable). The controller runs `TRADEWIND_RUN_WEB_INTEGRATION=1 uv run pytest tests/integration/test_web_live.py -v -m integration` once and records the result in the RUNBOOK (Task 6).
- [ ] **Step 3:** commit after the gate (`test(web): live tests for Claude and Codex`).

### Task 6: Docs and release 0.16.0

**Files:**
- Modify: `README.md` (capability matrix rows; the web access section: per-backend modes, the Codex `fetch=True` note), `docs/components/07-web-access.md` (status line: slice 2 implemented in 0.16.0), `docs/RUNBOOK.md` (the live command and the result), `CHANGELOG.md` (0.16.0), `pyproject.toml` and `src/tradewind/__init__.py` `__version__` (`0.16.0`), `uv.lock` (the package version line only)

- [ ] **Step 1:** update the docs; the README matrix shows the exact modes from Global Constraints.
- [ ] **Step 2:** `bash scripts/check.sh`; all checks pass. **Step 3:** commit after the gate (`docs(web): per-backend modes; release 0.16.0`).
