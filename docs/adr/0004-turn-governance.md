---
id: "ADR-0004"
title: "Turn governance is shared mechanisms, not per-backend rules"
status: accepted
date: 2026-11-15
deciders: [human:ianynchen]
---

# ADR-0004: Turn governance is shared mechanisms, not per-backend rules

Date: 2026-11-15 · Status: Accepted · Spec: `docs/components/05-turn-governance.md`

## Context

Safety and lifecycle rules (which action may run, when a turn is stuck,
what to check before a turn starts, how a prompt is read) were being
written once per backend. The planned Antigravity adapter
(`docs/components/04-antigravity-adapter.md`) had grown about 15 special
rules of this kind. Each backend would have its own code for them, its own
tests, and its own gaps. The owner asked (2026-11-15) for generic
mechanisms over problem-specific fixes, without a loss of performance,
security, accessibility or functionality.

## Decision

Four mechanisms are shared by every backend. An adapter only **maps** its
engine's messages onto them and declares its rules; it makes no policy
decision in code. Full design: `05-turn-governance.md`.

1. **Action policy.** Every agent action is one `Action` (`kind` plus
   `fields`). One rule table decides it: `allow`, `ask` (the broker),
   `deny` or `stop`, with an optional `record`. Floor rules (Tradewind's
   and each backend's) only deny or stop. The final outcome is the stronger
   of the floor outcome and the chosen outcome (first match among caller
   rules, backend default rules, the mode default). A caller can tighten
   anything, and can loosen only defaults. Two entry points: `gate` (the
   engine asks first) and `observe` (Tradewind only sees the action).
   Permission modes: `broker` and `engine_auto`.
2. **Work supervisor.** Adapters report work signals (tools, background
   tasks, subagents, model activity). The runner tracks broker questions.
   Limits: model silence 300 s, work silence 1800 s, optional hard cap
   (`request_timeout_s`, now `None` = backend default). All limits pause
   while a broker question is open. A process report is emitted when a
   limit fires, on interrupt, or when descendants stay alive.
3. **Preflight checks.** `Backend.prepare_turn` runs ordered checks
   (`Ok`, `Ask`, `Stop`) before `TurnStarted`. `Stop` raises `ConfigError`.
4. **Prompt rule.** A prompt is always a message to the model.

Also in the port: a result-tool seam (`tradewind_submit_result`, at most 3
calls, validated with `jsonschema`) and three capability flags
(`supports_engine_auto`, `supports_work_supervisor`,
`supports_action_policy`). A setting that needs a flag a backend lacks
raises `Unsupported` from one runner check before `prepare_turn`.

Existing brokers are not broken: on `ask` they get today's name and
arguments plus one extra key, `tradewind`, in the broker's copy of the
arguments only.

### Shipped in PR 1

Claude, Codex and LangChain, and a fake backend that exercises the seams
as Antigravity will. Capability flags (engine_auto / work supervisor /
action policy): Claude False/True/True, Codex True/True/True, LangChain
False/True/True, Cursor False/False/False. Claude `engine_auto` is
unsupported because the SDK's auto mode bypasses `can_use_tool`. The
Antigravity mapping is PR 2 (future work).

## Consequences

- A rule is written once and holds on every backend that can intercept
  actions. A new engine costs a mapping, not a new policy.
- Behaviour changes (all in CHANGELOG 0.12.0): a broker that raises now
  gives `deny`; the hard cap pauses during a broker question on all
  backends and the default is now `None`; `turn_timeout` notice before the
  terminal event; Codex brokers get the extra `tradewind` key; prompts are
  sent as a message (`Message: ` prefix) so slash commands are not
  expanded.
- One new runtime dependency: `jsonschema>=4.26,<5` (it was already in the
  lock through `mcp`). Process listing uses `ps`; no `psutil`.
- A turn can now end with a terminal record and no `TurnStarted` (stop,
  limit or error before `run()` reaches it).
- Known limits: Claude and Codex process reports are skipped (no public
  engine pid); `_in_workspace` is lexical (no symlink resolution); Cursor
  supports none of the governance features. Observe on Codex sees only
  completed actions that had no request, so broad `ask` rules stop turns
  late; callers write narrow rules.
- Plain turns on Claude, Codex and LangChain emit the same events as before.

## Alternatives rejected

- **Per-backend special rules (the Antigravity design as it stood).**
  About 15 rules in adapter code: each needs its own tests, callers cannot
  add their own, and safety rulings could differ between backends.
- **A broker-only model (no rule table).** The broker is a per-call yes/no
  with no shared vocabulary; the floor/caller split and observe-only
  actions would still need code per backend.
- **`psutil` for process listing.** A new dependency for one `ps` call.
- **Set Claude `permission_mode="auto"` for engine_auto.** The SDK skips
  `can_use_tool` for calls the mode allows, so caller tools would bypass
  the gate.
