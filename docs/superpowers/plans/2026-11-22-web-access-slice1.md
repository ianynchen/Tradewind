# Web Access, Slice 1 (shared parts) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the backend-independent parts of web access: the session setting, the capability modes (all `None` for now), the per-kind floor, Tradewind's own fetch tool and its classification as `fetch_url`, the recording of Codex web items' action and queries, and a public `capabilities(profile)` call; release 0.15.0.

**Architecture:**
- The setting lives in `SessionOptions` and its snapshot; the runner reads it from the snapshot, checks it against the backend's capability modes, builds the per-kind floor from it, and serves Tradewind's fetch tool through the existing `ToolHost` when `web.fetch` is on.
- One function, `caller_tool_action()`, builds the `Action` for a caller tool in the four places that build it by hand today, and it classifies `tradewind_fetch_url` as `fetch_url`.
- No backend turns a web tool on in this slice: every backend's modes stay `None`. Slice 2 does Claude (search gated) and Codex (search observed, with its observe mapping).

**Tech Stack:** Python 3.12+, the standard library (`urllib.request`, `html.parser`, `http.server` in tests), the existing runner, `ToolHost`, policy gate and adapters. No new dependency.

**Spec:** `docs/components/07-web-access.md` (owner decisions 1–13, 2026-11-22), slice 1. It builds on `05-turn-governance.md` (§1.1, §2.1–§2.4) and `06-workspace-safety.md`.

## Global Constraints

- GUIDELINES.md §1–4, §7, §8, §10 (a test that opens a socket or spawns a process is an integration test), §11 and §13.5 bind. No CI change.
- No new runtime dependency (owner decision 4).
- With no web setting (`web=None`), every backend behaves exactly as today: no web floor rule, no fetch tool, the same events (owner decision 11).
- Exact names: `WebMode = Literal["gated", "observed"]`; `WebAccess(search: bool, fetch: bool, observed: bool = False)`; `SessionOptions.web: WebAccess | None = None`; `Capabilities.web_search: WebMode | None = None`; `Capabilities.fetch_url: WebMode | None = None`; the tool name `tradewind_fetch_url`; `Tradewind.capabilities(profile: str | None = None) -> Capabilities`.
- Every backend's `web_search` and `fetch_url` stay `None` in this slice.
- The fetch tool: `http` and `https` only, also on the request it makes; **no redirects** (a 3xx ends the fetch, and the result names the `Location`); a time limit and a size limit for each page; text only, HTML tags removed (owner decision 9).
- Never run `claude`, `codex` or `agy` CLIs and never run live tests. Never read `~/.config/tradewind/live-tests.env`.
- Gate: `bash scripts/check.sh > log 2>&1 && git commit …` — never `;`, never a pipe on the gate.

## Review Focus

- An old session, stored before 0.15.0, has no `web` key in its snapshot; resuming it must behave as `web=None` (Task 1).
- `web=None` on every backend must add no floor rule and no tool, so plain turns emit exactly the same events as before (Tasks 2 and 5).
- A fetch of a page that redirects to another host, or to `localhost`, must not follow it: the result names the `Location` and nothing else is read (Task 3).
- A fetch of `file:///etc/passwd`, `ftp://…` or a URL with no scheme must read nothing and return a refusal (Task 3).
- A `tradewind_fetch_url` call with no `url`, or a `url` that is not a string, must map with `missing_critical_field`, so the floor denies it on every backend (Task 4).

---

### Task 1: The setting and the capability modes

**Files:**
- Modify: `src/tradewind/domain/models.py` (`Capabilities`, `SessionOptions`, `SessionOptions.snapshot`)
- Test: `tests/unit/test_domain_models.py`

**Interfaces:**
- Produces: `WebMode`, `WebAccess`, `SessionOptions.web`, snapshot key `"web"` (`{"search": bool, "fetch": bool, "observed": bool}` or `None`), `Capabilities.web_search`, `Capabilities.fetch_url`; a helper `web_from_snapshot(snapshot: dict[str, object]) -> WebAccess | None` in the same module (a missing key is `None`).

- [ ] **Step 1: Write the failing tests**

```python
def test_web_access_defaults_and_snapshot():
    assert SessionOptions().web is None
    assert SessionOptions().snapshot()["web"] is None
    snap = SessionOptions(web=WebAccess(search=True, fetch=False)).snapshot()
    assert snap["web"] == {"search": True, "fetch": False, "observed": False}
    assert web_from_snapshot(snap) == WebAccess(search=True, fetch=False)

def test_an_old_snapshot_has_no_web():
    assert web_from_snapshot({"tools": []}) is None

def test_capabilities_web_modes_default_to_none():
    caps = Capabilities(**REQUIRED_CAPABILITY_FLAGS)  # the existing required flags, all False
    assert caps.web_search is None and caps.fetch_url is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_domain_models.py -q -k web`
Expected: FAIL (`WebAccess` not defined).

- [ ] **Step 3: Implement** `WebMode`, `WebAccess` (a frozen pydantic model like the module's others), the two `Capabilities` fields with default `None` (so no existing constructor changes), `SessionOptions.web`, the `"web"` snapshot key, and `web_from_snapshot`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_domain_models.py -q`
Expected: PASS.

- [ ] **Step 5: Commit** (`feat(web): the web setting and the capability modes`), after the gate.

### Task 2: The per-kind floor and the support check

**Files:**
- Modify: `src/tradewind/domain/policy.py` (`floor_tradewind`)
- Modify: `src/tradewind/application/turn_runner.py`: `web = web_from_snapshot(snapshot)` next to `_stored_governance(snapshot)`; a new `_check_web_support(backend: Backend, web: WebAccess | None) -> None`, called right after `_check_governance_support`; `floor=[*floor_tradewind(web), *backend.floor_rules()]`
- Modify: `tests/unit/fake_governed_backend.py` (keyword arguments `web_search: WebMode | None = None`, `fetch_url: WebMode | None = None`, returned in `capabilities()`)
- Test: `tests/unit/test_policy_rules.py`, `tests/unit/test_governance_fake_backend.py`

**Interfaces:**
- Consumes: Task 1.
- Produces: `floor_tradewind(web: WebAccess | None = None) -> list[Rule]`; with `web` set, it adds `Rule(id="floor.web_search_off", kind="web_search", outcome="deny", record=True, reason="web search is not turned on for this session")` when `not web.search`, and `floor.fetch_url_off` likewise for fetch. `web=None` returns today's list unchanged. The other callers (`claude_backend`, `langchain_backend`, `codex_backend` direct-policy paths) keep calling it with no argument.

- [ ] **Step 1: Write the failing tests**

```python
def test_floor_without_web_is_unchanged():
    assert [r.id for r in floor_tradewind()] == ["floor.missing_critical_field"]
    assert [r.id for r in floor_tradewind(None)] == ["floor.missing_critical_field"]

def test_floor_denies_each_kind_left_off():
    ids = {r.id for r in floor_tradewind(WebAccess(search=False, fetch=True))}
    assert "floor.web_search_off" in ids and "floor.fetch_url_off" not in ids
```

In `test_governance_fake_backend.py`:
- a session with `web=WebAccess(search=True, fetch=False)` on a fake whose `web_search=None` fails with `Unsupported` before `TurnStarted`;
- the same with `web_search="observed"` and `observed=False` fails with `Unsupported`; with `observed=True` the turn runs;
- with `web=WebAccess(search=False, fetch=True)` and `fetch_url="gated"`, an observed `web_search` action stops the turn (`end_reason` from the floor) and a gated one is denied;
- with `web=None`, an observed `web_search` action does not stop the turn (owner decision 11).

- [ ] **Step 2: Run them to verify they fail.**

- [ ] **Step 3: Implement.** The support check raises `Unsupported` with a message that names the backend and the kind, for example `"web search is not offered on backend 'codex'"` and `"web search is only observed on backend 'codex'; set WebAccess(observed=True) to accept it"`. A web setting on a backend without `supports_action_policy` is refused by the same message as a kind that is `None`.

- [ ] **Step 4: Run** `uv run pytest tests/unit/test_policy_rules.py tests/unit/test_governance_fake_backend.py -q`. Expected: PASS.

- [ ] **Step 5: Commit** (`feat(web): per-kind floor and the support check`), after the gate.

### Task 3: Tradewind's fetch tool

**Files:**
- Create: `src/tradewind/application/web_fetch.py`
- Test: `tests/integration/test_web_fetch.py` (it opens a local socket: §10)

**Interfaces:**
- Produces: `FETCH_TOOL_NAME = "tradewind_fetch_url"`; `FETCH_TIMEOUT_S = 20`; `FETCH_MAX_BYTES = 1_000_000`; `fetch_tool() -> Tool` (`input_schema`: an object with one required string `url`); its handler `async def fetch_url(url: str) -> str`.
- The handler runs the blocking `urllib` call in a worker thread (`anyio.to_thread.run_sync`), with an opener whose redirect handler does not follow (a 3xx returns `"Redirect <status> to <Location>; not followed."`), refuses any scheme other than `http`/`https` with `"Only http and https URLs can be fetched."` before any request, reads at most `FETCH_MAX_BYTES` (then adds `"[truncated]"`), and returns the text with tags, `<script>` and `<style>` removed (`html.parser`); a non-HTML text type is returned as it is, any other type as `"Not a text page (<content type>)."`.

- [ ] **Step 1: Write the failing tests**, against a `http.server.ThreadingHTTPServer` on `127.0.0.1:0` in a fixture:
  - `test_fetch_returns_the_page_text`: an HTML page with `<script>x()</script><p>Hello</p>` returns text that contains `"Hello"` and not `"x()"`;
  - `test_a_redirect_is_not_followed`: a `302` to `http://127.0.0.1:<port>/secret` returns `"Redirect 302 to http://127.0.0.1:<port>/secret; not followed."`, and the server log shows no request for `/secret`;
  - `test_other_schemes_are_refused`: `file:///etc/passwd`, `ftp://example.com/x` and `example.com` each return the refusal text, and no request is made;
  - `test_a_large_page_is_cut`: a page of `FETCH_MAX_BYTES + 10` bytes returns text that ends with `"[truncated]"`;
  - `test_a_binary_page_is_not_returned`: `image/png` returns `"Not a text page (image/png)."`.

- [ ] **Step 2: Run them to verify they fail.**

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run** `uv run pytest tests/integration/test_web_fetch.py -q`. Expected: PASS.

- [ ] **Step 5: Commit** (`feat(web): Tradewind's own fetch tool`), after the gate.

### Task 4: One caller-tool mapper, with `tradewind_fetch_url` as `fetch_url`

**Files:**
- Modify: `src/tradewind/application/policy_gate.py` (new `caller_tool_action`; `_GateBroker.decide` uses it)
- Modify: `src/tradewind/adapters/claude_backend.py` (`_caller_tool_action` replaced by it)
- Modify: `src/tradewind/adapters/langchain_backend.py` (same)
- Modify: `src/tradewind/adapters/codex_backend.py` (the shim branch, around the `kind="caller_tool"` construction, uses it)
- Test: `tests/unit/test_policy_gate.py`

**Interfaces:**
- Consumes: `FETCH_TOOL_NAME` from Task 3 (`tradewind.application.web_fetch`).
- Produces: `caller_tool_action(name: str, args: dict[str, object], *, backend: str) -> Action`. For `FETCH_TOOL_NAME`: `kind="fetch_url"`, `fields={"url": args.get("url")}`, `missing_critical_field=True` when `url` is missing or not a `str`. For every other name: exactly today's `caller_tool` action (`fields={"name", "args", "source"}`, `summary=name`).

- [ ] **Step 1: Write the failing tests**

```python
def test_a_caller_tool_maps_as_today():
    a = caller_tool_action("lookup", {"q": 1}, backend="claude")
    assert (a.kind, a.fields["name"], a.fields["source"], a.summary) == ("caller_tool", "lookup", "local", "lookup")

def test_the_fetch_tool_maps_to_fetch_url():
    a = caller_tool_action("tradewind_fetch_url", {"url": "https://example.com"}, backend="codex")
    assert (a.kind, a.fields, a.missing_critical_field) == ("fetch_url", {"url": "https://example.com"}, False)

@pytest.mark.parametrize("args", [{}, {"url": 7}, {"url": None}])
def test_a_fetch_call_with_no_usable_url_is_missing_a_field(args):
    assert caller_tool_action("tradewind_fetch_url", args, backend="langchain").missing_critical_field
```

- [ ] **Step 2: Run them to verify they fail.**

- [ ] **Step 3: Implement** and replace the four hand-built constructions. The existing adapter and gate tests must pass unchanged.

- [ ] **Step 4: Run** `uv run pytest tests/unit -q`. Expected: PASS.

- [ ] **Step 5: Commit** (`refactor(policy): one caller-tool mapper; the fetch tool is fetch_url`), after the gate.

### Task 5: Serve the fetch tool when `web.fetch` is on

**Files:**
- Modify: `src/tradewind/application/turn_runner.py` (the `ToolHost(...)` call: append `fetch_tool()` to `options.tools` when the stored web setting has `fetch`)
- Test: `tests/unit/test_governance_fake_backend.py` or `tests/unit/test_turn_runner.py`

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: nothing new for later tasks. The tool is added only at the `ToolHost` call, never to `SessionOptions.tools`, so the snapshot and resume validation do not change.

- [ ] **Step 1: Write the failing tests:**
  - with `web=WebAccess(search=False, fetch=True)` on a fake with `fetch_url="gated"`, the turn's tool host serves `tradewind_fetch_url`, and a caller rule `Rule(id="u", kind="fetch_url", where={"url": Matcher("equals", "https://example.com")}, outcome="allow")` allows that URL and the default asks the broker for another;
  - with `web=None`, the tool host serves only the caller's own tools, and a plain turn's events equal today's.

- [ ] **Step 2: Run them to verify they fail.** **Step 3: Implement.** **Step 4: Run** `uv run pytest tests/unit -q`; PASS. **Step 5: Commit** (`feat(web): serve the fetch tool when fetch is on`), after the gate.

### Task 6: Record a Codex web item's action and queries

**Files:**
- Modify: `src/tradewind/adapters/codex_backend.py` (`thread_item_to_messages`, the `WebSearchThreadItem` branch)
- Test: `tests/unit/test_codex_mapping.py`

**Interfaces:**
- Produces: the recorded `web_search` message's `content` gains `"action"` (the item's action as JSON, or `None`) and `"queries"` (`action.queries` when the action is a search with a list, else `None`), next to today's `"query"` and `"results"` (owner decision 12).

- [ ] **Step 1: Write the failing tests**, from `WebSearchThreadItem` values shaped as the spike saw them (07 §4a): a search with `query`; a search with a `queries` list; an `openPage` with a `url`; and an item with `action=None`. Assert the four `content` dicts exactly.
- [ ] **Step 2: Run them to verify they fail.** **Step 3: Implement.** **Step 4: Run** `uv run pytest tests/unit/test_codex_mapping.py -q`; PASS. **Step 5: Commit** (`feat(codex): record a web item's action and queries`), after the gate.

### Task 7: `Tradewind.capabilities(profile)`, docs and release 0.15.0

**Files:**
- Modify: `src/tradewind/application/client.py` (`capabilities`)
- Modify: `README.md` (client API and `SessionOptions.web`), `docs/components/01-configuration-and-client.md` (client API), `CHANGELOG.md` (0.15.0), `pyproject.toml` (`version = "0.15.0"`)
- Create: `docs/adr/0006-web-access.md` (the two modes, the per-kind floor, the fetch tool; status accepted, with the 07 owner decisions)
- Test: `tests/unit/test_client_sessions.py`

**Interfaces:**
- Produces: `Tradewind.capabilities(profile: str | None = None) -> Capabilities`: the backend of that profile (default `config.default_profile`), through `_resolve_backend`; an unknown profile raises the same `ConfigError` as a session on it.

- [ ] **Step 1: Write the failing tests:** the default profile's capabilities equal its backend's `capabilities()`; a named profile works; an unknown name raises `ConfigError`; no network and no session are created.
- [ ] **Step 2: Run them to verify they fail.** **Step 3: Implement**, then the docs, the ADR and the version. **Step 4: Run** `bash scripts/check.sh`; all checks pass. **Step 5: Commit** (`feat(client): capabilities(profile); release 0.15.0`), after the gate.
