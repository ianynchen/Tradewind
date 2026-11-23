# Component: Web access (any backend)

Status: slice 1 shipped in 0.15.0 and slice 2 in 0.16.0 (design approved 2026-11-22). Its first caller is Meridian's web
lookups (`meridian/docs/components/web-lookups.md`). It builds on turn governance (`05`):
the action kinds `web_search` and `fetch_url` (§2.1), the rules (§2.2) and the broker
contract (§1.1). It changes `05` only where a decision below says so.

## Purpose

A caller turns on web search, web fetch or both for one session, on any backend that
Tradewind connects to. Each call is either gated before it runs or recorded with every
call visible, by the shared kinds, and the caller chooses which it accepts.

### Success

1. With no web setting (the default), every backend behaves as today: Claude and
   LangChain have no web tool on, and Codex keeps its own search, recorded (owner
   decision 11).
2. With the setting, the backend offers the call in one of two modes: **gated** (the
   caller's rules decide before it runs) or **observed** (it runs, and the caller's rules
   see it right after and can stop the turn). The rules see it as `web_search` (field
   `query`) or `fetch_url` (field `url`) in both modes.
3. A backend reports its mode for each kind in its capabilities. A session that asks for
   a kind the backend does not offer, or for an observed kind without accepting
   observed calls, fails with `Unsupported` (R-1), not with a silent call.
4. The calls and their results are recorded in the mirror.

## Owner decisions (2026-11-22)

1. Web access is configurable, and off by default. The agent requests it, and a human
   approves each request. In Tradewind terms, the approval is the caller's: its rules
   and its broker.
2. It works on any backend that Tradewind connects to, not only Claude.
3. ~~A backend offers search only when it can stop a search before it runs.~~
   Superseded by decision 10. Every backend that serves caller tools offers fetch,
   through one fetch tool that Tradewind provides.
4. No search provider, so no new dependency and no API key. Search comes only from a
   backend's own tool.
5. **No web on Cursor** (review finding 1): Cursor has no action policy
   (`supports_action_policy=False`; `turn_runner.py` refuses `policy_rules`), so a web
   call there could not be gated. Both its modes are `None` until it gets action policy.
6. **One shared mapper** classifies Tradewind's own tools (review finding 2): today four
   places always classify a caller tool as `caller_tool` (`claude_backend`,
   `langchain_backend`, the gate broker, the Codex shim branch). One function maps
   `tradewind_fetch_url` to the `fetch_url` kind, and those four places call it.
7. **The floor rule is per kind, from the setting** (review finding 3).
8. **Codex gets fetch through its shim** (review finding 4): its caller tools reach
   `ToolHost.call`, which the gate broker gates.
9. **The fetch tool does not follow redirects** (review finding 6).
10. **Two modes, the caller chooses** (owner, after the spike, 2026-11-22): a backend
    offers web search either **gated** before it runs (Claude) or **observed**, recorded
    with every query and result visible (Codex). Removing a backend's own capability
    because it cannot be stopped in advance treats the agent as a suspect; the caller
    decides which mode it accepts. Codex's own search is offered as observed.
11. **No web setting: every backend as today** (owner, 2026-11-22): Tradewind does not
    change an engine's own default. Codex keeps its own web search, recorded, in a
    session with no web setting. Decision 1's "off by default" covers the tools that
    Tradewind turns on.
12. **Record a Codex web item's action and queries** (review): so that every URL Codex
    opened is in the record.
13. **An item with no action, an unknown action, or a page with no URL is a missing
    critical field** (review): the floor denies it, so on the observe path the turn
    stops and the record says why.

## Design

### 1. The setting

`SessionOptions.web: WebAccess | None = None`, with `WebAccess(search: bool, fetch:
bool, observed: bool = False)`. `observed=True` accepts a kind that the backend offers
only as observed. `None` means no web access. The setting is declarative, so it is part
of the session snapshot (I-2).

### 2. The capability modes

`Capabilities` gets `web_search: WebMode | None` and `fetch_url: WebMode | None`, with
`WebMode = "gated" | "observed"`. `gated`: the call goes through the gate path of §2.1
before it runs. `observed`: it goes through the observe path right after it runs, so a
rule can stop the turn. `None`: not offered. A session fails with `Unsupported` when its
setting asks for a kind that is `None`, or for an `observed` kind without
`observed=True`.

### 3. A floor rule, per kind

When a session has a web setting, the floor (§2.4) denies `web_search` unless
`web.search` is on, and `fetch_url` unless `web.fetch` is on. So a session with fetch
only can never search, and no caller rule can loosen it. The floor is built from the
session's setting, so `floor_tradewind()` takes it as input. On an observed backend,
the adapter also passes the setting to the engine, so that a kind the caller left off is
off in the engine too (Codex: `web_search = "disabled"`); it changes nothing else in
the engine's own setup. With no web setting, the floor has no web rule, and the engine
keeps its own default (owner decision 11).

### 4. Each backend

| Backend | Search | Fetch | Known today | The spike must find |
|---|---|---|---|---|
| Claude | **gated**: its own `WebSearch` (turned on with `tools=["WebSearch"]`), through `can_use_tool` | **gated**: Tradewind's fetch tool | See "Spike results" | — |
| Codex | **observed**: its own web search (`web_search = "live"`, or `"cached"`), with its page actions | **gated**: Tradewind's fetch tool, through the shim; its own `openPage` is part of its search and is observed | See "Spike results" | — |
| Cursor | `None` | `None` | `supports_action_policy=False`: caller rules are refused | — (owner decision 5) |
| LangChain | `None` (owner decision 4) | **gated**: Tradewind's fetch tool | No built-in web tool; Tradewind owns the tool loop | — |

Each adapter maps its own web tools to the shared kinds, with the critical field
(`query` or `url`), so that rules and brokers see the same action on every backend:

- Claude: `WebSearch` → `web_search` (`query`), on the gate path.
- Codex: a `webSearch` item → on the observe path, by its action: `search` →
  `web_search` (`query`; one action for each distinct query, `query` first and then
  the `queries` list in order, and the first stop ends the turn), `openPage` and `findInPage` → `fetch_url`
  (`url`). No action, an unknown action, or a page action with no `url` sets
  `missing_critical_field` (owner decision 13). Today `observed_actions` maps only
  commands and file changes; it gains these.

Claude's own `WebFetch` is not used: the spike showed that it returns a second model's
summary of the page, not the page, so the agent cannot quote it.

### 4a. Spike results (2026-11-22)

| Question | Claude | Codex |
|---|---|---|
| Can its own search be stopped before it runs? | Yes: `WebSearch` is a CLI tool; `can_use_tool` gets `{"query": …}` first; a deny means no search | No: no approval request comes; the item starts with an empty query, which is known only when it completes |
| Recorded? | Yes: a normal tool use and tool result, with the links | Yes: a `webSearch` item with the query, the action and the results (title, URL, domain, snippet; 12 to 17 for each search) |
| Structured output with web on? | Yes | Yes, `live` and `cached`, also through Tradewind with no code change (two sources, URL and quote) |
| Its own fetch | `WebFetch` gets `url` and `prompt`; returns a model's summary | `openPage` and `findInPage` work; recorded with the URL |
| Terms for a program | Allowed for the owner's own plan on the owner's tool; uses the subscription limits | Not settled for a personal plan; OpenAI points programmatic use to an API key (Business and Enterprise: access tokens). Tradewind's Codex backend already runs on the ChatGPT login |

### 5. Tradewind's fetch tool

One tool, `tradewind_fetch_url`, served like a caller tool, and classified as the
`fetch_url` kind by the shared mapper (owner decision 6):

- the Python standard library (`urllib`, `html.parser`) plus certifi's CA bundle for TLS;
- `http` and `https` only, and **no redirects**: a redirect ends the fetch, and the
  result says where it points, so the caller can approve that URL in its turn (owner
  decision 9);
- a time limit and a size limit for each page; text only, HTML tags removed;
- the result is untrusted data for the model.

### 6. Recording

The calls and their results are recorded in the mirror as items, as for any tool. Codex
records `web_search` with its query and results today; its mapping
(`thread_item_to_messages`) also records the `action` and the `queries` (owner decision
12).

## Interface changes

- `SessionOptions.web` and `WebAccess`.
- `Capabilities.web_search` and `Capabilities.fetch_url` (`WebMode | None`), and
  `WebAccess.observed`.
- Codex's `observed_actions` maps `webSearch` items to `web_search` and `fetch_url`.
- The floor rule for `web_search` and `fetch_url`.
- The tool name `tradewind_fetch_url`, and the shared mapper that classifies it.
- `floor_tradewind()` takes the session's web setting.
- `Tradewind.capabilities(profile)`: a public call that returns a profile's backend
  capabilities, so that a caller can show only what is supported.
- A minor version, with an ADR, as for 0.12.0 and 0.13.0.

## Slices

0. **Spikes, one per backend** (no commit to main): done for Claude and Codex
   (section 4a).
1. **Shared:** the setting, the modes (all `None`), the floor rule per kind, the fetch
   tool, the shared mapper in the four places, the recording. Tested with the fake
   backend.
2. **Per backend:** Claude search gated; Codex search observed, with its observe
   mapping; fetch gated everywhere it is served. One live test for each backend.

## Known issues, recorded and not designed for

- An engine update can move a web tool from the gated path to an ungated one. The floor
  rule and the version checks of `06` cover the known cases; a live test for each
  backend catches the rest.
- A URL that the owner approves can point to a host on the owner's own network. The
  owner approves the exact URL, and redirects are not followed, so this is the owner's
  choice. Probability: low.
- Without a search provider, LangChain has fetch only.
- An observed search has already run when a rule sees it. A rule can stop the turn
  after it, not prevent it; that is the mode the caller accepts with `observed=True`.

## Left to the builder

- The exact time and size limits of the fetch tool.
- The text extraction details.
