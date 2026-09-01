# GUIDELINES  
  
Operating guidelines for AI coding agents and human contributors.  
  
Sections 1–4 govern **how to work** on any change. Sections 5–12 govern **how the project is shaped**, standards, and delivery. Section 13 governs **agent-session discipline** (model use, checkpoints, explicit uncertainty). Re-read 1–4 before each task; 5–12 before structural decisions; 13 when working as an autonomous agent.  
  
---  
  
## 1. Think & Ask  
- State assumptions explicitly. If uncertain or multiple interpretations exist, ask—never pick silently.  
- If a simpler approach exists, push back and suggest it.  
- Simplicity (§2) does not override the duty to surface ambiguity. Resolve ambiguity first.  
  
### 1.1 The spec and plan are a contract — no unilateral deviations  
  
The design spec (`docs/components/*`, and REQUIREMENTS) and the phase plans (`docs/superpowers/plans/*`) exist so work is **trackable against an agreed contract** — NOT as a starting suggestion the agent edits and reports afterward. **Decide-then-inform is a violation.** Before acting, STOP and get explicit approval for any of:  
  
- **Scope changes** — deferring, descoping, or dropping anything in the plan/requirements (even "I'll do it in a later sub-phase"); adding scope not in the plan.  
- **Spec deviations** — a module layout, identifier grammar, table/schema shape, fact-stream contract, node/edge/catalog form, or construct set that differs from what the spec states (including "extending" a set the spec calls *closed*).  
- **Design choices not pre-decided** in the plan's decisions, the spec, or this document's defaults.  
- **Plan/spec edits themselves** — changing a decision, marking scope done/deferred, or amending a `docs/components/*` file.  
  
Allowed without asking: reading anything; running tests/linters/builds; writing tests; **implementing exactly what the plan/spec specifies**. When the spec is silent or wrong, do not pick — follow §6 (propose amendment with rationale, then proceed *once confirmed*; never code around a doc without updating it first, per §5).  
  
**Pre-commit gate:** before committing or merging, confirm the change contains **zero un-approved deviations** from the spec/plan. If a deviation was necessary, it must already be an approved doc amendment. List any deviation in the message only after it was approved — never as a fait accompli.  
  
---  
  
## 2. Simplicity First  
- Minimum code to solve the exact request. Zero speculative features, abstractions, or unrequested config.  
- No error handling for impossible scenarios.  
- Rewrite 200 lines if it can be done in 50. (Test: Would a senior engineer call this overcomplicated?)  
  
---  
  
## 3. Surgical Changes  
- Touch only what you must. Match existing style.  
- Do not "improve" or refactor adjacent code, comments, or formatting.  
- Delete unused imports/variables/functions created *by your change*. Do not touch pre-existing dead code; mention it instead.  
- Test: Every changed line traces directly to the user's request.  
  
---  
  
## 4. Goal-Driven Execution  
  
**Define success criteria. Loop until verified.**  
  
Transform tasks into verifiable goals:  
  
- "Add validation" → write tests for invalid inputs, then make them pass.  
- "Fix the bug" → write a test that reproduces it, then make it pass.  
- "Refactor X" → ensure tests pass before and after.  
  
For multi-step tasks, state a brief plan:  
  
```  
1. [step] → verify: [check]  
2. [step] → verify: [check]  
```  
  
AI agents additionally checkpoint after each significant step per §13.4.  
  
---  
  
## 5. Source of Truth  
  
Authoritative when documents disagree, in this order (initialize these under the `docs/` folder on first use if they do not exist):  
  
1. **[REQUIREMENTS.md](docs/REQUIREMENTS.md)** — functional and non-functional requirements.  
2. **[ARCHITECTURE.md](docs/ARCHITECTURE.md)** — design decisions, technology choices, extension points, C4 diagrams. Must be consistent with REQUIREMENTS.md.  
3. **[PROJECT.md](docs/PROJECT.md)** — delivery status only (`planned | in-progress | delivered | deferred | cancelled`), each entry linking to a requirement. Create on first use if missing.  
4. **[RUNBOOK.md](docs/RUNBOOK.md)** - lessons learned throughout current project. To be reviewed before a task is being performed to avoid past mistakes.  
  
**CHANGELOG.md** follows [Keep a Changelog](https://keepachangelog.com/) and [SemVer](https://semver.org/). Every merge to main adds an `## [Unreleased]` entry; tagging rolls it into a versioned section.  
  
Reference across documents; never duplicate. Any change that alters behaviour or design **updates REQUIREMENTS.md and/or ARCHITECTURE.md in the same commit**.  
  
### 5.1 Schema changes update every schema-describing file  
  
The schema contract can described in several files at once; they must never drift apart. **When the schema changes** — update **all** of these in the **same commit**:  
  
- The affected **component specs** under `docs/components/`, and any other touched spec.  
- **REQUIREMENTS.md / ARCHITECTURE.md** where the contract (FR-3 endpoint catalog, NFR-5 construct set, §8 catalog format) is stated.  
- `CHANGELOG.md`.  

### 5.2 Architectural Changes

Architectural change should not take place silently. Always ask for approval before making an architectural change, and always record such changes as ADRs after approval. Clearly state what's replaced in the ADRs. Always apply architectural unit tests to ensure there is no authorized architectural changes. Exact architectural test framework is to be specified in ARCHITECTURE.md file.
  
---  
  
## 6. Conflict Resolution  
  
When code or evidence conflicts with the source of truth, **stop and surface the conflict before implementing**. Resolutions, in order of preference:  
  
1. Propose a doc amendment with rationale, proceed once confirmed.  
2. Propose an alternative that fits the existing design.  
3. Flag and ask.  
  
Never resolve a conflict by editing code in a way that contradicts the documents without updating them first.  
  
When **two patterns in the codebase contradict**, do not blend or split the difference — that produces the worst kind of change: it satisfies neither rule and obscures the inconsistency. Pick one (prefer the more recent or better-tested path), state why, implement consistently, and flag the other pattern for cleanup in a follow-up.  
  
---  
  
## 7. Agent Scope  
  
**Out of scope without explicit instruction:**  
  
- CI/CD config, build scripts, infrastructure-as-code.  
- Adding, upgrading, or removing dependencies.  
- Database migrations.  
- Disabling, skipping, or weakening existing tests.  
- Public APIs, wire formats, persisted schemas.  
- New global or static mutable state.  
- Bulk reformatting outside the requested change.  
  
**Always in scope:** reading any file for context; running tests, linters, formatters, and architecture checks; proposing follow-up work as separate items.  
  
---  
  
## 8. Design Principles  
  
Non-negotiable defaults. Deviations require justification in ARCHITECTURE.md.  
  
- **Single responsibility.** If a unit needs "and" to describe its purpose, split it.  
- **Composition over inheritance.** Inheritance permitted only for: framework integration, sealed type hierarchies, or exception hierarchies — all documented in ARCHITECTURE.md.  
- **Business rules in domain or application services**, never in controllers or transport-layer code.  
- **Dependencies flow inward** toward the domain.  
- **Extension points are explicit interfaces**, with documented contracts.  
- **No new global or static mutable state.** Immutability by default; mutation is a deliberate choice with a stated reason. Side effects at the edges.  
- **Architecture rules are enforceable, not aspirational.** Layering, dependency direction, and naming conventions are encoded as automated architecture tests (tooling chosen in ARCHITECTURE.md). An untested rule is already being violated.  
  
**Diagrams:** Use Mermaid for L1/L2 in docs; L3 only if non-obvious. Use ASCII art for L2, class, and sequence diagrams during chat conversations. A lying diagram is worse than none.  
  
---  
  
## 9. Coding Standards  
  
### Documentation  
  
Every public symbol documents: **purpose**, **parameters and return** (meaning, units, ranges, nullability), **failure modes**, and **concurrency guarantees** when relevant.  
  
**Extension points** additionally document: required invariants, ordering and idempotency expectations, what the framework will and will not do, and examples when the contract is subtle.  
  
**Non-obvious decisions** document: alternatives considered, tradeoff accepted, and conditions to revisit. Prefer one comment explaining *why* over five restating *what*.  
  
### Naming and style  
  
- Names describe intent at the call site, not implementation.  
- Booleans read as predicates (`isReady`, `hasExpired`).  
- Avoid abbreviations except established domain terms.  
- Formatter and linter configs are committed and run in CI; the agent runs them locally before proposing a change. Tooling named in ARCHITECTURE.md.  
  
### Errors  
  
- Typed errors or exceptions, not strings — callers can branch on them.  
- Messages name the failed operation, the inputs that mattered (no secrets), and the likely cause when known.  
- No silent catches without a comment explaining why.  
  
### Logging and observability  
  
- Log at boundaries (request, external call, state transition), not in tight loops.  
- Structured logging with stable field names; no string concatenation of values into messages.  
- Never log secrets, tokens, full PII, or full user-data payloads.  
  
---  
  
## 10. Testing  
  
GUIDELINES.md states *what tests must cover and how they behave*. ARCHITECTURE.md names the framework, assertion library, mocking library, and integration harness.  
  
### Coverage  
  
Every public method on a domain or application service has tests for the **happy path**, each **documented failure mode**, and **boundary conditions** (empty, absent, max, zero, negative, duplicate, concurrent if relevant).  
  
Branch and condition coverage are diagnostics, not targets. Coverage drops require justification; coverage increases driven by trivial tests are equally suspect. Bug fixes ship with a regression test that fails before the fix.  
  
### Behaviour  
  
- Tests describe behaviour, not implementation; renaming a private method must not break tests.  
- Tests should encode **why** the behaviour matters (invariants, risks), not only snapshots of outputs — see §13.3 for the agent-facing bar.  
- Test names read as specifications (exact convention in ARCHITECTURE.md).  
- Each test is independent — no shared mutable state, no ordering dependencies.  
- No sleeps for synchronisation; use awaitility/polling primitives or deterministic clocks.  
- Flaky tests are bugs: quarantine immediately, fix or delete within a defined window. Never re-run until green.  
  
### Test types  
  
- **Unit** — fast, isolated, no I/O. Bulk of the suite.  
- **Integration** — real adapters against test doubles, in-memory implementations, or testcontainers. Mocks acceptable at boundaries with systems the team does not own.  
- **Architecture** — enforce §8 in CI alongside unit tests.  
- **Contract** — for services consumed by or consuming other services, when applicable.  
  
### Don't test  
  
Framework behaviour, trivial accessors, generated code, or implementation details that exist only to make a test pass.  
  
---  
  
## 11. Version Control  
  
**Conventional Commits**: `type(scope): summary`. Types: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `build`, `ci`, `perf`. Breaking changes use `!` and a `BREAKING CHANGE:` footer.  
  
**PR description** states what changed and why (linking REQUIREMENTS.md), any deviation from ARCHITECTURE.md with justification, risk and rollback notes for non-trivial changes, and new dependencies with rationale.  
  
### Versioning  
  
The package version in `pyproject.toml` follows [SemVer](https://semver.org/) and is bumped **on every change that alters shipped behaviour**:  
  
- **patch** (`0.0.x`) — bug fixes (no behaviour/API addition).  
- **minor** (`0.x.0`) — backward-compatible enhancements / new features.  
- **major** (`x.0.0`) — breaking changes. Pre-1.0, a breaking change may ride a minor bump but must be called out with `!` / a `BREAKING CHANGE:` footer.  
  
Pure docs, chore, test, or behaviour-preserving refactor changes do **not** bump. **Propose the exact bump and get explicit confirmation before applying it — never bump silently.** A change may span several commits; bump once, on the commit where the behaviour lands.  
  
### Definition of Done  
  
- [ ] Compiles cleanly; all tests pass locally.  
- [ ] Run linter before commit code.  
- [ ] New behaviour has tests per §10.  
- [ ] Architecture tests pass.  
- [ ] Formatter and linter clean.  
- [ ] REQUIREMENTS.md / ARCHITECTURE.md updated in the same commit if behaviour or design changed.  
- [ ] Schema change? All schema-describing files updated together, and `docs/html` regenerated via `scripts/generate-html.py` (§5.2).  
- [ ] PROJECT.md status updated.  
- [ ] CHANGELOG.md `Unreleased` updated for user-visible changes.  
- [ ] Package version bumped per SemVer (§ Versioning) if behaviour changed — proposed and confirmed before applying.  
- [ ] RUNBOOK.md updated with lessons learned.  
- [ ] Conventional Commits format.  
  
---  
  
## 12. When in Doubt  
  
Ask before assuming. Prefer the option that is easier to undo, easier to read six months from now, and — if equivalent — smaller.  
  
---  
  
## 13. Agent discipline  
  
Rules in this section apply to **AI coding agents** in addition to §1–§12. They operationalize judgment and honesty defaults that plain "be careful" guidance does not nail down.  
  
### 13.1 Use the model only for judgment calls  
  
Use the language model for: classification, drafting, summarization, extraction from unstructured text.  
  
Do **not** use the model for: routing, retries, status-code handling, or deterministic transforms. If a status code or a short piece of plain code already answers the question, code answers the question.  
  
### 13.2 Read before you write  
  
Before adding code in a file: read that file's public surface (exports, primary types), the immediate caller, and obvious shared utilities. If the reason for the existing structure is unclear, ask before extending it. "Looks orthogonal to me" is a warning sign, not a green light.  
  
### 13.3 Tests encode intent, not only behaviour  
  
Every test should make **why** the behaviour matters obvious — not only **what** happens. A test that would still pass when business rules change but only fixtures were renamed is weak. Prefer assertions and names tied to the requirement or invariant. (Aligns with §10; this section states the bar explicitly for agents.)  
  
### 13.4 Checkpoint after every significant step  
  
After each meaningful step in a multi-step task: briefly state what was done, what was verified, and what remains. Do not continue from a state you cannot describe back to the requester. If continuity is lost, stop and restate the plan from evidence (files, test output), not from memory alone.  
  
### 13.5 Match the codebase's conventions, even if you disagree  
  
Conformance beats personal taste inside the repo: naming, file layout, patterns (e.g. class vs hooks, snake_case vs other styles) follow what is already established. If a convention appears harmful, surface it explicitly and propose a separate change — do not fork style silently in one patch.  
  
### 13.6 Fail loud  
  
If something might not have worked, say so. Do not claim "migration completed" when rows were skipped without calling that out; do not claim "tests pass" when any were skipped; do not claim "feature works" when an agreed edge case was not checked. Default to surfacing uncertainty and partial results over confident understatement.  
