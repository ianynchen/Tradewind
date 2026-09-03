# Phase 3: Broker verdict enrichment — deny-reason and terminate

Date: 2026-09-03 · Status: PROPOSED (awaiting approval) · Parent:
`2026-09-03-improvement-roadmap.md` (item F). SDK wire shapes VERIFIED
before speccing, per the roadmap's own precondition:

- claude `can_use_tool` deny is `PermissionResultDeny(message: str,
  interrupt: bool)` — deny-reason AND terminate are NATIVE (better than
  the matrix guessed). (`PermissionResultAllow.updated_input` also exists
  — input rewrite stays DEFERRED per the roadmap.)
- codex approval responses are protocol-untyped `JsonObject`s whose
  accepted vocabulary is `{"decision": "accept"|"reject"}` / elicitation
  `{"action": ...}` — **no reason channel exists**; the matrix's
  "doubtful" resolves to NO.
- cursor: no interception (unchanged; `supports_interactive_permissions`
  already False).

## 1. The broker contract — backward compatible

`PermissionBroker.decide` today returns `"allow" | "deny"`. Phase 3 widens
the RETURN type only — every existing broker keeps working unchanged:

```python
@dataclass(frozen=True)
class Denial:
    """A rich deny (FR-4.4): `reason` is shown to the MODEL where the
    backend has a channel for it (`supports_deny_reason`), and always
    recorded in the `PermissionRequested` event and the mirrored error
    tool_result. `terminate=True` ends the turn after this denial is
    delivered."""

    reason: str | None = None
    terminate: bool = False

BrokerDecision = Verdict | Denial   # decide() may return either
```

A single domain helper normalizes: `("deny", reason, terminate)` /
`("allow", None, False)`. `PermissionRequested` gains an optional
`reason: str | None = None` field (additive; the event dataclass is
frozen-taxonomy-safe — no new member, one new field with a default).

## 2. Per-backend delivery (verified honesty)

| | deny-reason to the model | terminate |
|---|---|---|
| langchain | The reason IS the synthesized error tool_result text (today's hardcoded "permission denied" becomes `reason or "permission denied"`). | After delivering the batch's denial results, the loop ends: `status="completed"`, new `end_reason="broker_terminated"` (Literal growth, backward-compatible) — results persisted, honest partial, Pi's after-the-batch semantics. |
| claude | `PermissionResultDeny(message=reason)` — native. | `PermissionResultDeny(interrupt=True)` — native. The adapter marks the turn broker-terminated so the resulting aborted `ResultMessage` becomes `TurnCompleted(status="completed", end_reason="broker_terminated")` rather than the generic interrupted synthesis. |
| codex | NO channel (protocol-verified). The model sees the engine's own denial; tradewind's `reason` is recorded in `PermissionRequested.reason` and nowhere else — declared via `supports_deny_reason=False`, never faked. | Approximate, documented: reply `reject`, then `TurnHandle.interrupt()`; the turn ends through the existing interrupt contract with the adapter marking `end_reason="broker_terminated"` on its synthesized completion where distinguishable, else the standard interrupted shape. |
| cursor | n/a — no interception (existing flag). | n/a |

New capability flag (required, the established pattern — BREAKING for
`Capabilities` constructors): `supports_deny_reason` — langchain/claude
True, codex/cursor False. Terminate needs no flag: it is delivered
everywhere a broker is consulted at all, at the strength the table states.

## 3. What does NOT change

Allow-path behavior, the deny-only `PermissionRequested` emission rule,
`ToolHost.call` gating for codex's socket path (the socket-side broker in
`ToolHost` gains the same normalization so a `Denial` works there too),
and input rewriting (deferred — a mutation-hook philosophy decision, Phase
5 material).

## Deliverables & verification

- Tests: normalization unit table; langchain reason-in-tool_result +
  terminate-ends-loop (`broker_terminated`, batch results persisted,
  call-counts); claude `can_use_tool` bridge maps reason/interrupt
  (fake-SDK-client level) + broker-terminated completion; codex socket/
  approval paths accept `Denial` (reason recorded in event, reject sent);
  `PermissionRequested.reason` populated on all consulted paths;
  backward-compat: existing string-returning brokers untouched (every
  existing broker test keeps passing unmodified).
- Docs: REQUIREMENTS FR-4.4 (+FR-8.1 flag list), ARCHITECTURE §3.2
  (`PermissionRequested.reason`; `broker_terminated`), components 01/03,
  README (Permission broker section + capability matrix + end_reason
  table), CHANGELOG, RUNBOOK.
- Version: minor bump proposed (0.9.0 → 0.10.0) with a BREAKING footer
  (required `supports_deny_reason` capability field); confirmed before
  applying.

## Out of scope

Input rewriting (`updated_input` — noted as natively available on claude
for the Phase-5 decision), an "ask"-with-options UI protocol, per-tool
static policies, and any cursor interception work (P-5 unchanged).
