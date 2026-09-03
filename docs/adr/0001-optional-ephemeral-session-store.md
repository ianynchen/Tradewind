# ADR-0001: Optional session store — one union field, ephemeral in-memory default

Date: 2026-09-02 · Status: accepted (user-approved in session) · Drives: FR-5.7

## Context

`TradewindConfig` required a store: `StoreConfig` carried two mutually
exclusive optional fields (`sqlite_path: Path | None`, `store:
SessionStorePort | None`) with a hand-written "exactly one" cross-field
validator — a shape inherited verbatim from the implementation plan.
Sextant (and embedders like it) want to use tradewind without configuring
any database: SDK backends already persist natively, and a langchain
session only needs context for the life of the process.

## Decision

1. **Persistence is optional (FR-5.7).** With no store configured, the
   mirror is an **ephemeral in-memory sqlite database** (`":memory:"`
   through the ordinary `SqliteSessionStore`, which holds one connection
   per instance — so it is fully functional, per-instance, and gone at
   exit). Chosen over "no store at all" (user-confirmed): the mirror is
   also what feeds langchain's context rebuild, so a RAM store preserves
   multi-turn conversation context, single-flight, and reconcile with zero
   runner changes, while still writing nothing to disk.
2. **`StoreConfig` is replaced** by a single union field:
   `TradewindConfig.store: Path | SessionStorePort | None = None`
   (path → default sqlite engine; port instance → caller-built store,
   Postgres later per P-4; `None` → ephemeral). The illegal both-set
   state becomes unrepresentable, deleting the cross-field validator.

## Replaced

- `StoreConfig` (class, export, and its "exactly one of
  sqlite_path/store" `ConfigError`) — removed outright, pre-1.0 breaking.
- FR-5.1/FR-6.3's unconditional "mirror is the durability floor" — now
  scoped to configurations with a persistent store.

## Consequences

- Embedders construct `Tradewind(TradewindConfig(profiles=...,
  default_profile=...))` with no storage decision at all.
- Two no-store instances never share state (`":memory:"` is
  per-connection; test-locked).
- A langchain session on a no-store instance loses all context at process
  exit, and `resume()` of its ids in a new process raises
  `SessionNotFound` — by design; configure a path for durability.

## Revisit when

Postgres (P-4) lands, or an embedder needs a process-shared in-memory
store (would require `sqlite3` shared-cache URIs or a dedicated port
implementation).
