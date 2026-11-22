# Component: Web access (any backend)

Status: design, for the owner's approval (2026-11-22). Its first caller is Meridian's web
lookups (`meridian/docs/components/web-lookups.md`). It builds on turn governance (`05`):
the action kinds `web_search` and `fetch_url` (§2.1), the rules (§2.2) and the broker
contract (§1.1). It changes `05` only where a decision below says so.

## Purpose

A caller turns on web search, web fetch or both for one session, on any backend that
Tradewind connects to, and each call is gated by the shared kinds before it runs.

### Success

1. With no web setting (the default), every backend behaves as today, and a web call is
   denied.
2. With the setting, a backend that can stop a call before it runs offers it; the
   caller's rules see it as `web_search` (field `query`) or `fetch_url` (field `url`).
3. A backend that cannot do that reports it in its capabilities, and a session that asks
   for it fails with `Unsupported` (R-1), not with a silent call.
4. The calls and their results are recorded in the mirror.

## Owner decisions (2026-11-22)

1. Web access is configurable, and off by default. The agent requests it, and a human
   approves each request. In Tradewind terms, the approval is the caller's: its rules
   and its broker.
2. It works on any backend that Tradewind connects to, not only Claude.
3. A backend offers search only when it can stop a search before it runs. Every backend
   that serves caller tools offers fetch, through one fetch tool that Tradewind provides.
4. No search provider, so no new dependency and no API key. Search comes only from a
   backend's own tool.
5. **No web on Cursor** (review finding 1): Cursor has no action policy
   (`supports_action_policy=False`; `turn_runner.py` refuses `policy_rules`), so a web
   call there could not be gated. Both its flags are false until it gets action policy.
6. **One shared mapper** classifies Tradewind's own tools (review finding 2): today four
   places always classify a caller tool as `caller_tool` (`claude_backend`,
   `langchain_backend`, the gate broker, the Codex shim branch). One function maps
   `tradewind_fetch_url` to the `fetch_url` kind, and those four places call it.
7. **The floor rule is per kind, from the setting** (review finding 3).
8. **Codex gets fetch through its shim** (review finding 4): its caller tools reach
   `ToolHost.call`, which the gate broker gates. Its own search has no approval before
   it runs, so its search flag is false unless the spike finds one.
9. **The fetch tool does not follow redirects** (review finding 6).

## Design

### 1. The setting

`SessionOptions.web: WebAccess | None = None`, with `WebAccess(search: bool, fetch:
bool)`. `None` keeps today's behaviour. The setting is declarative, so it is part of the
session snapshot (I-2).

### 2. The capability flags

`Capabilities` gets `supports_web_search: bool` and `supports_fetch_url: bool`. A flag
is true only when the backend can gate the call **before** it runs (the gate path of
§2.1, not the observe path). A session whose setting asks for an unsupported tool fails
with `Unsupported`.

### 3. A floor rule, per kind

The floor (§2.4) denies `web_search` unless `web.search` is on, and `fetch_url` unless
`web.fetch` is on. So no backend reaches the web unless the caller turned that kind on,
a session with fetch only can never search, and no caller rule can loosen it. The floor
is built from the session's setting, so `floor_tradewind()` takes it as input.

### 4. Each backend

| Backend | Search | Fetch | Known today | The spike must find |
|---|---|---|---|---|
| Claude | its own `WebSearch`, when the spike shows that `can_use_tool` fires before it runs | Tradewind's fetch tool | Built-in tools are off (`tools=[]`); `can_use_tool` gates every tool call as `caller_tool` by name; `ServerToolUseBlock` is skipped | Is `WebSearch` a CLI tool (gated) or a server tool (not gated)? Does structured output still work? May a program use the subscription's own web tool under its terms? |
| Codex | false, unless the spike finds an approval before the search runs | Tradewind's fetch tool, through the shim | It records its web search as `web_search` after the fact; its caller tools reach `ToolHost.call` through the shim, gated by the gate broker | Can a web search be stopped before it runs? |
| Cursor | false | false | `supports_action_policy=False`: caller rules are refused | — (owner decision 5) |
| LangChain | none (owner decision 4) | Tradewind's fetch tool | No built-in web tool; Tradewind owns the tool loop | — |

Each adapter maps its own web tools to the shared kinds, with the critical field
(`query` or `url`), so that rules and brokers see the same action on every backend.

### 5. Tradewind's fetch tool

One tool, `tradewind_fetch_url`, served like a caller tool, and classified as the
`fetch_url` kind by the shared mapper (owner decision 6):

- the Python standard library only (`urllib`, `html.parser`);
- `http` and `https` only, and **no redirects**: a redirect ends the fetch, and the
  result says where it points, so the caller can approve that URL in its turn (owner
  decision 9);
- a time limit and a size limit for each page; text only, HTML tags removed;
- the result is untrusted data for the model.

### 6. Recording

The calls and their results are recorded in the mirror as items, as for any tool. Codex
already records `web_search` with its query and results.

## Interface changes

- `SessionOptions.web` and `WebAccess`.
- `Capabilities.supports_web_search` and `Capabilities.supports_fetch_url`.
- The floor rule for `web_search` and `fetch_url`.
- The tool name `tradewind_fetch_url`, and the shared mapper that classifies it.
- `floor_tradewind()` takes the session's web setting.
- `Tradewind.capabilities(profile)`: a public call that returns a profile's backend
  capabilities, so that a caller can show only what is supported.
- A minor version, with an ADR, as for 0.12.0 and 0.13.0.

## Slices

0. **Spikes, one per backend** (no commit to main), with the questions in section 4.
   The answers set each backend's flags.
1. **Shared:** the setting, the flags (all false), the floor rule per kind, the fetch
   tool, the shared mapper in the four places, the recording. Tested with the fake
   backend.
2. **Per backend:** turn on what the spike allows, map it to the kinds, set the flag.
   One live test for each backend that gets a flag.

## Known issues, recorded and not designed for

- An engine update can move a web tool from the gated path to an ungated one. The floor
  rule and the version checks of `06` cover the known cases; a live test for each
  backend catches the rest.
- A URL that the owner approves can point to a host on the owner's own network. The
  owner approves the exact URL, and redirects are not followed, so this is the owner's
  choice. Probability: low.
- Without a search provider, LangChain (and any backend whose search cannot be gated)
  has fetch only.

## Left to the builder

- The exact time and size limits of the fetch tool.
- The text extraction details.
