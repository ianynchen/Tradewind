"""Turn runner: orchestrates one turn end to end (task-9 brief).

`TurnRunner.execute()` is the one place that: merges option layers into an
effective tier and resolves it to a `ModelSpec` via the session's profile;
mints the turn id and opens it in the store (`begin_turn`, `TurnInProgress`
propagates -- I-5); builds the `ToolHost`/broker/`TurnContext` for this
turn; mirrors every `ItemCompleted` into the store as the backend emits it;
forwards every event to both the caller's stream and the config's
`on_event` tap; and finalizes the turn's terminal status in the store no
matter how the backend's event stream ends (clean completion, `TurnFailed`,
mid-turn interrupt, or the caller abandoning the stream early).

Constructed once per `Tradewind` instance (`client.py`), which supplies the
store, config, and a `resolve_backend` callback so this module never has to
import an adapter itself (GUIDELINES §8 "dependencies flow inward" --
`application` may not import `adapters`).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Callable
from typing import cast

import anyio

from tradewind.application.config import TradewindConfig
from tradewind.application.ports import Backend, SessionStorePort, TurnContext
from tradewind.application.tool_host import ToolHost
from tradewind.domain.errors import ConfigError, SessionNotFound
from tradewind.domain.events import Event, ItemCompleted, TurnCompleted, TurnFailed
from tradewind.domain.models import (
    NormalizedMessage,
    Profile,
    SessionOptions,
    SessionRow,
    TierName,
    TurnResult,
    TurnStatus,
    Verdict,
)

_logger = logging.getLogger(__name__)


class _AllowAllBroker:
    """Fallback used when neither `SessionOptions.permission_broker` nor
    `TradewindConfig.permission_broker` is set: permits every tool call.

    No ruling specifies a default policy for "no broker configured at all";
    this is the least-surprising default for an embedded library (FR-4.1
    requires a broker be *consulted*, not that a caller-supplied one always
    exists) -- flagged here so it is easy to revisit.
    """

    async def decide(self, _tool_name: str, _tool_input: dict[str, object]) -> Verdict:
        return "allow"


def _resolve_profile(config: TradewindConfig, profile_name: str) -> Profile:
    # A small, deliberate duplicate of `client._resolve_profile`'s lookup
    # (not its `None`-defaults-to-`default_profile` branch, which this
    # caller never needs -- `session_row.profile` is always concrete):
    # `turn_runner` cannot import from `client` without creating the import
    # cycle client.py -> turn_runner.py -> client.py.
    profile = config.profiles.get(profile_name)
    if profile is None:
        raise ConfigError(f"unknown profile {profile_name!r}")
    return profile


def _effective_tier(
    profile: Profile,
    snapshot: dict[str, object],
    overrides: dict[str, object],
    defaults_tier: TierName,
) -> TierName:
    """Merge `TurnDefaults.tier < snapshot["tier"] < overrides["tier"]`
    (task-9 ruling) and validate the result against `profile.models`.

    `TurnDefaults.request_timeout_s` is the other field in this layering
    but nothing downstream of this runner enforces a wall-clock timeout
    yet -- deferred, not silently dropped (flagged for a later task, same
    pattern as `ctx.output_schema` in the langchain adapter).
    """
    override_tier = overrides.get("tier")
    if override_tier is not None:
        tier = cast(TierName, override_tier)
    else:
        snapshot_tier = snapshot.get("tier")
        tier = cast(TierName, snapshot_tier) if snapshot_tier is not None else defaults_tier
    if tier not in profile.models:
        raise ConfigError(f"unknown tier {tier!r} for profile with models {sorted(profile.models)}")
    return tier


def _effective_system_prompt(session: SessionRow, overrides: dict[str, object]) -> str | None:
    override = overrides.get("system_prompt")
    if override is not None:
        return cast(str, override)
    return session.system_prompt


def _effective_output_schema(
    snapshot: dict[str, object], overrides: dict[str, object]
) -> dict[str, object] | None:
    override = overrides.get("output_schema")
    if override is not None:
        return cast("dict[str, object]", override)
    return cast("dict[str, object] | None", snapshot.get("output_schema"))


class TurnRunner:
    """Orchestrates one turn per `execute()` call (see module docstring)."""

    def __init__(
        self,
        store: SessionStorePort,
        config: TradewindConfig,
        resolve_backend: Callable[[str, Profile], Backend],
    ) -> None:
        self._store = store
        self._config = config
        self._resolve_backend = resolve_backend
        # session_id -> a stop() call is pending/in-flight for its current
        # turn; consulted only when a turn's event stream ends without a
        # terminal event, to tell an expected interrupt apart from an
        # unexpected early end (see `execute()`).
        self._interrupt_requested: set[str] = set()

    async def request_stop(self, session_id: str, backend: Backend) -> None:
        """`Session.stop()`'s implementation: record that this session's
        in-flight turn (if any) is being interrupted on purpose, then ask
        the backend to cancel it. A no-op at the backend level when no turn
        is in flight (`Backend.interrupt` contract)."""
        self._interrupt_requested.add(session_id)
        await backend.interrupt(session_id)

    def _tap(self, event: Event, turn_id: str) -> None:
        on_event = self._config.on_event
        if on_event is None:
            return
        try:
            on_event(event)
        except Exception:
            # The tap is observability only and must never break the turn
            # (task-9 ruling: "swallowed -- log-and-continue").
            _logger.exception("on_event tap raised", extra={"turn_id": turn_id})

    async def execute(
        self,
        session_id: str,
        options: SessionOptions,
        prompt: str,
        overrides: dict[str, object],
    ) -> AsyncIterator[Event]:
        """Run one turn on `session_id` and yield its normalized events.

        `options` carries the live objects a snapshot cannot (tools,
        `mcp_servers`, `permission_broker` -- I-2); everything else
        (profile, tier, system_prompt, output_schema) is read back from the
        persisted session row/snapshot so this works identically whether
        `options` came from a fresh `create()`/`resume(options=...)` or is
        the empty default of a bare `resume(session_id)`.

        Failure modes:
            SessionNotFound: `session_id` does not exist.
            ConfigError: the session's profile is no longer registered, or
                the effective tier is not one of its models.
            TurnInProgress: `session_id` already has an in-flight turn
                (raised by `store.begin_turn`, propagates untouched -- I-5).
        """
        session_row = await anyio.to_thread.run_sync(self._store.get_session, session_id)
        if session_row is None:
            raise SessionNotFound(session_id)

        profile = _resolve_profile(self._config, session_row.profile)
        snapshot = cast("dict[str, object]", session_row.options_snapshot)
        tier = _effective_tier(profile, snapshot, overrides, self._config.defaults.tier)
        model_spec = profile.models[tier]

        turn_id = str(uuid.uuid4())
        self._interrupt_requested.discard(session_id)
        await anyio.to_thread.run_sync(self._store.begin_turn, session_id, turn_id, None)

        terminal_status: TurnStatus | None = None
        final_text: str | None = None
        usage: dict[str, object] | None = None
        cost_usd: float | None = None
        error: str | None = None

        # Everything from here on runs with an open turn in the store, so
        # it is all inside try/finally: any failure -- resolving the
        # backend included, not just the turn itself -- still finalizes
        # the turn as `failed` rather than leaving it `in_progress` until
        # the next `sweep_stale_turns` (session open).
        try:
            backend = self._resolve_backend(session_row.profile, profile)
            broker = (
                options.permission_broker or self._config.permission_broker or _AllowAllBroker()
            )
            async with ToolHost(options.tools, options.mcp_servers, self._resolve_ref) as tool_host:
                history = await anyio.to_thread.run_sync(
                    lambda: self._store.history(
                        session_id, include_children=False, include_raw=False
                    )
                )
                # The prompt is this turn's own input, not something a
                # backend "completes" as an `ItemCompleted` event, so
                # nothing else mirrors it -- the runner writes it directly.
                # After `history` above so this turn's own prompt doesn't
                # also show up in `ctx.load_history()` (the backend appends
                # `ctx.prompt` itself when building its request).
                prompt_content: dict[str, object] = {"text": prompt}
                await anyio.to_thread.run_sync(
                    self._store.append_message,
                    session_id,
                    turn_id,
                    NormalizedMessage(role="user", kind="text", content=prompt_content),
                )
                ctx = TurnContext(
                    session=session_row,
                    turn_id=turn_id,
                    prompt=prompt,
                    model_spec=model_spec,
                    system_prompt=_effective_system_prompt(session_row, overrides),
                    output_schema=_effective_output_schema(snapshot, overrides),
                    tools=tool_host,
                    broker=broker,
                    load_history=lambda: history,
                )

                async for event in backend.run(ctx):
                    if isinstance(event, ItemCompleted):
                        await anyio.to_thread.run_sync(
                            self._store.append_message, session_id, turn_id, event.message
                        )
                    if isinstance(event, TurnCompleted):
                        terminal_status = event.result.status
                        final_text = event.result.final_text
                        usage = cast("dict[str, object]", event.result.usage)
                        cost_usd = event.result.cost_usd
                    elif isinstance(event, TurnFailed):
                        terminal_status = "failed"
                        error = event.error
                    self._tap(event, turn_id)
                    yield event

                if terminal_status is None:
                    # The backend's iterator ended with neither TurnCompleted
                    # nor TurnFailed -- per `Backend.run`'s contract, this only
                    # happens on a mid-turn interrupt. Synthesize the terminal
                    # event callers of run()/stream() rely on for a result.
                    interrupted = session_id in self._interrupt_requested
                    terminal_status = "interrupted" if interrupted else "failed"
                    error = None if interrupted else "turn ended without emitting a terminal event"
                    synthesized: Event = (
                        TurnCompleted(
                            result=TurnResult(
                                turn_id=turn_id,
                                status="interrupted",
                                final_text=None,
                                usage={},
                                cost_usd=None,
                            )
                        )
                        if interrupted
                        else TurnFailed(turn_id=turn_id, error=cast(str, error))
                    )
                    self._tap(synthesized, turn_id)
                    yield synthesized
        except BaseException as exc:
            if terminal_status is None:
                terminal_status = "failed"
                error = f"turn runner: unhandled exception: {exc}"
            raise
        finally:
            if terminal_status is None:
                # Reached only when the caller abandoned the stream before
                # a terminal event was produced (GeneratorExit from an
                # early `break`/`aclose()`) -- no further `yield` is
                # possible here, only the store write.
                interrupted = session_id in self._interrupt_requested
                terminal_status = "interrupted" if interrupted else "failed"
                error = None if interrupted else "turn ended without emitting a terminal event"
            await anyio.to_thread.run_sync(
                lambda: self._store.finalize_turn(
                    turn_id,
                    status=terminal_status,
                    final_text=final_text,
                    usage=usage,
                    cost_usd=cost_usd,
                    error=error,
                )
            )
            self._interrupt_requested.discard(session_id)

    def _resolve_ref(self, ref: str) -> str:
        key = ref.removeprefix("ref:")
        secret = self._config.secret_refs.get(key)
        if secret is None:
            raise ConfigError(
                f"unresolved secret ref {ref!r}: no entry in TradewindConfig.secret_refs"
            )
        return secret.get_secret_value()
