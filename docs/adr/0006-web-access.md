---
id: "ADR-0006"
title: "Web access in two modes: gated and observed"
status: accepted
date: 2026-11-22
deciders: [human:ianynchen]
---

# ADR-0006: Web access in two modes: gated and observed

Date: 2026-11-22 · Status: Accepted · Spec: `docs/components/07-web-access.md`

## Context

Meridian needs web lookups (search and page fetch) on any backend. Engines differ.
The spike (spec §4a) found that Claude's `WebSearch` can be stopped before it runs
(`can_use_tool`), and that Codex's search cannot: no approval request comes, and the
query is known only when the item completes. Removing a capability only because it
cannot be stopped in advance treats the agent as a suspect, so the caller chooses.

## Decision

1. **Two modes.** A backend offers each web kind as `gated` (the caller's rules decide
   before the call) or `observed` (it runs, and the rules see it right after and can
   stop the turn). `Capabilities.web_search` and `Capabilities.fetch_url` hold
   `WebMode | None`.
2. **The setting.** `SessionOptions.web = WebAccess(search, fetch, observed=False)`,
   in the session snapshot. `observed=True` accepts an observed kind. A kind the
   backend does not offer, or an observed kind without `observed=True`, raises
   `Unsupported`. Any web setting on a backend without action policy (Cursor)
   raises `Unsupported`.
3. **A floor rule per kind**, built from the setting (`floor_tradewind(web)`): `web_search`
   is denied unless `web.search` is on, `fetch_url` unless `web.fetch` is on.
4. **One fetch tool**, `tradewind_fetch_url`: standard library only, `http` and `https`,
   no redirects (a redirect ends the fetch and names its target), time and size
   limits, text only. It is served only when `web.fetch` is on.
   It is not reachable until slice 2, because every backend's `fetch_url` is still `None`.
5. **One shared mapper** classifies Tradewind's own tools, so `tradewind_fetch_url` is
   the `fetch_url` kind on every backend.
6. **No web setting: every backend as today.** Tradewind does not change an engine's
   own default; Codex keeps its own search, recorded.
7. `Tradewind.capabilities(profile)` returns a profile's backend capabilities with no
   session and no network call, so a caller can show only what is supported.

## Consequences

- Slice 1 (0.15.0) ships the shared parts. Every backend's two modes are still `None`,
  so a session with a web setting raises `Unsupported` until slice 2.
- Slice 2 still to do: Claude gated search; Codex observed search and its observe
  mapping (`webSearch` actions to `web_search` and `fetch_url`); fetch gated where served;
  one live test for each backend.
- An observed search has already run when a rule sees it. That is the mode the caller
  accepts with `observed=True`.
- No search provider and no new dependency: LangChain has fetch only.
