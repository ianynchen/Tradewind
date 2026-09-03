# ADR-0002: Public backend-factory injection seam on the Tradewind constructor

Date: 2026-09-02 · Status: accepted (user-requested, sextant integration) ·
Amends: task-9's "single-argument constructor" ruling (client.py module
docstring)

## Context

The backend-factory registry (`client._backend_factories`) is module-private
by design — a layering seam assigned once at import time. That left no
public way to inject a scripted backend: sextant's no-network adapter tests
had to monkeypatch the private map to hand `LangchainBackend` a fake
`chat_model_factory`, which is not shippable in a downstream test suite.

## Decision

`Tradewind(config, *, backend_factories: dict[BackendName, BackendFactory]
| None = None)` — a keyword-only, per-instance override mapping consulted
before the module registry in `_resolve_backend`. Names not present fall
through to the registry, so overriding one backend leaves the rest
functional; the module registry and other instances are never touched.

Chosen over a `chat_model_factory` passthrough on `Profile.backend_options`:
the kwarg is backend-agnostic (works for faking claude/codex/cursor too),
and it keeps live callables out of the declarative config model that
`options_json` snapshots are built from.

## Replaced

- The "single-argument constructor exactly as specified" clause of the
  task-9 ruling. Lazily-built, per-instance-cached adapters are unchanged.

## Consequences

- Embedders script any backend through the public constructor; tradewind's
  own `test_turn_runner.py` seam tests are the usage reference.
- The registry remains the one production path; the override exists per
  instance only.
