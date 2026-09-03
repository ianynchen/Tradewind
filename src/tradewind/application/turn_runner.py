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

import json
import logging
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from pathlib import Path
from typing import ClassVar, Protocol, cast

import anyio

from tradewind.application.config import TradewindConfig
from tradewind.application.ports import Backend, SessionStorePort, TurnContext
from tradewind.application.resume import ResumePlanner
from tradewind.application.tool_host import ToolHost
from tradewind.domain.errors import (
    ConfigError,
    SessionNotFound,
    TurnInProgress,
    Unsupported,
)
from tradewind.domain.events import Event, ItemCompleted, TurnCompleted, TurnFailed
from tradewind.domain.models import (
    HistoryScope,
    ModelSpec,
    NormalizedMessage,
    Profile,
    SessionOptions,
    SessionRow,
    StoredMessage,
    TierName,
    TurnResult,
    TurnStatus,
    Verdict,
)

_logger = logging.getLogger(__name__)


class _CompactHistory(Protocol):
    """The duck-typed manual-compaction seam a mirror-rebuilding backend
    exposes (`LangchainBackend.compact_history`) -- typed here so
    `TurnRunner.compact`'s `getattr` cast spells the real signature (same
    reasoning as the `take_native_session_id` cast in `execute`)."""

    def __call__(
        self,
        model_spec: ModelSpec,
        history: list[StoredMessage],
        *,
        keep_recent_tokens: int,
        instructions: str | None = None,
    ) -> Awaitable[tuple[StoredMessage, dict[str, int]]]: ...


class _AllowAllBroker:
    """Fallback used when neither `SessionOptions.permission_broker` nor
    `TradewindConfig.permission_broker` is set: permits every tool call.

    Controller ruling (task-9 fix round 1): APPROVED as the default -- a
    caller that registers tools without wiring a broker gets those tools
    callable, not silently inert (FR-4.1 requires a broker be *consulted*,
    not that a caller-supplied one always exists). See also
    `TradewindConfig.permission_broker`'s field comment. Test-locked by
    `tests/unit/test_turn_runner.py::test_tool_executes_when_no_broker_is_configured_anywhere`.
    """

    # Duck-typed marker (final review wave, item 3a): lets a backend that
    # needs to know whether the *effective* broker is exactly this no-op
    # fallback -- as opposed to any caller-supplied `PermissionBroker`, of
    # whatever concrete type -- tell the two apart via `getattr(ctx.broker,
    # "is_default_allow_all", False)` rather than an `isinstance` check.
    # `CodexBackend` is the first consumer (a safer `sandbox` default when
    # nothing gates tool calls at all -- see its own module docstring's
    # "Approval/sandbox defaults" section). Duck-typing, not an
    # `isinstance` check against this class, deliberately keeps `adapters`
    # from having to import `application.turn_runner` itself (it may only
    # import `application.config`/`.ports`/`.tool_host` -- GUIDELINES §8) --
    # the same reasoning `TurnRunner.execute`'s own `getattr(backend,
    # "take_native_session_id", None)` uses in the opposite direction.
    is_default_allow_all: ClassVar[bool] = True

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

    `TurnDefaults.request_timeout_s` is the other field named in this
    layering, but it is UNIMPLEMENTED here -- not merged into anything,
    not read, not enforced. Wiring a wall-clock timeout needs a decision
    this runner doesn't make on its own (what termination status a timeout
    gets; none of `completed`/`failed`/`interrupted` was specified for it):
    flagged for a later task rather than guessed at, same pattern as
    `ctx.output_schema` in the langchain adapter.
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


def _effective_max_tool_rounds(overrides: dict[str, object]) -> int | None:
    """Validate the per-call `max_tool_rounds` override (FR-6.5).

    Per-call only — deliberately not merged from `TurnDefaults`/snapshot
    layers: the cap is a property of one call's budget, not of a session.
    `0` is meaningful (one model response, no tool execution permitted).

    Failure modes:
        ConfigError: the value is not a non-negative int (bool excluded).
    """
    value = overrides.get("max_tool_rounds")
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ConfigError(f"max_tool_rounds must be a non-negative int, got {value!r}")
    return value


def _effective_history_scope(overrides: dict[str, object]) -> HistoryScope | None:
    """Validate the per-call `history_scope` override (FR-9.3). Per-call
    only, like `max_tool_rounds`. Returns None when unset -- the effective
    default is per-backend and resolved in `execute()` once the backend's
    capabilities are known ("flat" on a mirror-rebuilding backend, "none"
    on a native-resume one).

    Failure modes:
        ConfigError: the value is not one of "none"/"flat"/"tree".
    """
    value = overrides.get("history_scope")
    if value is None:
        return None
    if value not in ("none", "flat", "tree"):
        raise ConfigError(f"history_scope must be one of 'none'/'flat'/'tree', got {value!r}")
    # mypy narrows `value` to the literal set via the membership check above.
    return value


def _render_child_block(child_session_id: str, messages: list[StoredMessage]) -> str:
    """One descendant session's transcript as a single plain-text block
    (FR-9.3, scope "tree"): text kinds verbatim, tool activity as
    one-liners, `thinking` dropped (matching `_rebuild_messages`'
    always-drop rule for replayed context). Wrapping -- rather than raw
    interleave -- keeps the parent's role alternation and tool_use/
    tool_result pairing valid for the provider API (user decision,
    2026-09-03)."""
    lines = [f"[subagent {child_session_id} transcript]"]
    for message in messages:
        content = cast("dict[str, object]", message.content)
        if message.kind == "thinking":
            continue
        if message.kind == "text":
            text = content.get("text")
            if text:
                lines.append(f"{message.role}: {text}")
        elif message.kind == "tool_use":
            lines.append(
                f"assistant called tool {content.get('name')!r} "
                f"with {json.dumps(content.get('input', {}))}"
            )
        elif message.kind == "tool_result":
            flag = "error" if content.get("is_error") else "ok"
            lines.append(f"tool result ({flag}): {content.get('content', '')}")
        else:
            lines.append(f"{message.role} {message.kind}: {json.dumps(content)}")
    lines.append(f"[end subagent {child_session_id} transcript]")
    return "\n".join(lines)


def _fold_child_history(
    parent_session_id: str, rows: list[StoredMessage], before_seq: int
) -> list[StoredMessage]:
    """Shape a tree read (`history(include_children=True)`, ordered
    `(session_id, seq)`) into feedable context (FR-9.3): the parent's own
    messages in order (excluding this turn's prompt, `seq >= before_seq`),
    with each descendant session folded into ONE wrapped `kind="text"`
    block positioned after the last parent message at or before the
    descendant's first message timestamp -- i.e. after the parent turn
    during which it was spawned (user decision, 2026-09-03: coarse
    timestamp positioning now; exact spawn-point interleave waits on the
    `spawned_by_message_id` deferral). Timestamps are the store's own
    microsecond-ISO `created_at` strings, lexicographically ordered."""
    parent = [m for m in rows if m.session_id == parent_session_id and m.seq < before_seq]
    children: dict[str, list[StoredMessage]] = {}
    for message in rows:
        if message.session_id != parent_session_id:
            children.setdefault(message.session_id, []).append(message)
    blocks = [
        StoredMessage(
            role="user",
            kind="text",
            content={"text": _render_child_block(child_id, messages)},
            session_id=child_id,
            created_at=messages[0].created_at,
        )
        for child_id, messages in children.items()
    ]
    blocks.sort(key=lambda block: block.created_at)
    merged: list[StoredMessage] = []
    next_block = 0
    for message in parent:
        while next_block < len(blocks) and blocks[next_block].created_at < message.created_at:
            merged.append(blocks[next_block])
            next_block += 1
        merged.append(message)
    merged.extend(blocks[next_block:])
    return merged


def _effective_output_schema(
    snapshot: dict[str, object], overrides: dict[str, object]
) -> dict[str, object] | None:
    override = overrides.get("output_schema")
    if override is not None:
        return cast("dict[str, object]", override)
    return cast("dict[str, object] | None", snapshot.get("output_schema"))


# --- R-1 system-prompt emulation (ARCHITECTURE §3.1: "performed above the
# port by the Turn Runner / client, driven by the flags") -----------------

# Namespaced, never `AGENTS.md` or any other existing file (R-1) -- the one
# path this emulation is ever allowed to write.
_CURSOR_RULES_RELATIVE_PATH = Path(".cursor", "rules", "tradewind-session.mdc")


def _rules_file_content(system_prompt: str) -> str:
    header = (
        "<!-- tradewind: generated system-prompt emulation (ARCHITECTURE §3.1 R-1). "
        "Safe to delete; rewritten idempotently at the start of every turn on a "
        "backend with no native system prompt. -->\n"
    )
    return header + system_prompt + "\n"


def _write_rules_file(cwd: str, system_prompt: str) -> bool:
    """Best-effort write of `.cursor/rules/tradewind-session.mdc` under
    `cwd` (R-1). Returns whether the write succeeded -- an `OSError`
    (unwritable/missing/non-directory `cwd`, ...) is caught here and treated
    as "not writable", the trigger for `_emulate_system_prompt`'s
    first-message-folding fallback, rather than failing the turn.

    Written idempotently on every turn (overwriting whatever was there) and
    never removed by this function: cleanup on session archive isn't wired
    (controller ruling, task-15) -- a documented follow-up, not a silent gap.
    """
    try:
        rules_path = Path(cwd) / _CURSOR_RULES_RELATIVE_PATH
        rules_path.parent.mkdir(parents=True, exist_ok=True)
        rules_path.write_text(_rules_file_content(system_prompt), encoding="utf-8")
    except OSError:
        return False
    return True


def _fold_system_prompt(system_prompt: str, prompt: str) -> str:
    return f"[Instructions]\n{system_prompt}\n[Task]\n{prompt}"


def _emulate_system_prompt(
    backend: Backend,
    session_row: SessionRow,
    system_prompt: str | None,
    prompt: str,
    *,
    is_first_turn: bool,
) -> str:
    """R-1: a backend whose `capabilities().supports_system_prompt` is False
    never silently drops a caller's `system_prompt` -- this emulates it
    above the port instead, driven by the flag (any such backend, not just
    Cursor, though Cursor is the only one today).

    Preferred: (re)write the rules file into `session_row.cwd` -- idempotent
    overwrite on EVERY turn, regardless of `is_first_turn` -- and return
    `prompt` unchanged: the backend's own request reads the caller's
    workspace files fresh each turn, so there is nothing to duplicate by
    rewriting it repeatedly.

    Fallback (no `cwd`, or the write failed): fold `system_prompt` into the
    returned prompt text (`[Instructions]\\n...\\n[Task]\\n...`) -- but ONLY
    when `is_first_turn` is True (controller ruling, task-15 fix round 1).
    A backend without a native system prompt can still keep its OWN native
    conversation history across turns (e.g. Cursor's `Agent.resume()`); the
    fold on turn one is what plants the instructions in that native
    history, and they stay there on their own from then on -- folding again
    on every later turn would re-inject them into the backend's own context
    on every single turn, duplicating them turn after turn. (This is
    independent of, and does not fix on its own, `TurnRunner.execute`'s own
    obligation to persist the CALLER's original prompt, not this fold, into
    the mirror -- see its own comment for that half.) A turn after the
    first returns `prompt` unchanged even if the rules-file write is still
    failing: the caller gets no repeated emulation attempt past turn one on
    this path, an accepted limitation of the "first turn only" rule as
    specified.

    A backend that natively supports a system prompt, or a turn with no
    `system_prompt` at all, returns `prompt` verbatim -- nothing to emulate.
    """
    if system_prompt is None or backend.capabilities().supports_system_prompt:
        return prompt
    if session_row.cwd is not None and _write_rules_file(session_row.cwd, system_prompt):
        return prompt
    if not is_first_turn:
        return prompt
    return _fold_system_prompt(system_prompt, prompt)


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
        self._resume_planner = ResumePlanner()
        # session_id -> a stop() call is pending/in-flight for its current
        # turn; consulted only when a turn's event stream ends without a
        # terminal event, to tell an expected interrupt apart from an
        # unexpected early end (see `execute()`).
        self._interrupt_requested: set[str] = set()
        # Session ids with an in-flight turn OR manual compaction in THIS
        # process (FR-5.8 B3 single-flight): the store's `begin_turn`
        # covers turn-vs-turn; this set additionally keeps a manual
        # `compact()` and a turn from interleaving mirror writes. Same
        # single-process assumption as the store's own seq serialization.
        self._busy: set[str] = set()

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
            ConfigError: the session's profile is no longer registered, the
                effective tier is not one of its models, or a
                `max_tool_rounds` override is not a non-negative int.
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
        # Validated here, with tier -- before `begin_turn` opens a turn a
        # bad value would only fail.
        max_tool_rounds = _effective_max_tool_rounds(overrides)
        history_scope_override = _effective_history_scope(overrides)

        turn_id = str(uuid.uuid4())
        self._interrupt_requested.discard(session_id)
        if session_id in self._busy:
            # A manual compaction (or an in-process turn the store hasn't
            # rejected yet) holds the session (FR-5.8 B3 / I-5).
            raise TurnInProgress(f"session {session_id!r} is busy (turn or compaction in flight)")
        await anyio.to_thread.run_sync(self._store.begin_turn, session_id, turn_id, None)
        self._busy.add(session_id)

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
            # FR-9.3 scope resolution. A native-resume backend's engine
            # replays its own history -- tradewind feeds it no mirror
            # context -- so the honest default there is "none", while a
            # mirror-rebuilding backend (langchain) defaults to "flat". An
            # EXPLICIT "flat"/"tree" on a native-resume backend cannot
            # reach the model and raises rather than silently dropping
            # (FR-1.2); an explicit "none" is accepted anywhere -- on a
            # native-resume backend it merely states what already happens.
            feeds_mirror_context = not backend.capabilities().supports_native_resume
            if (
                history_scope_override is not None
                and history_scope_override != "none"
                and not feeds_mirror_context
            ):
                raise Unsupported(
                    f"history_scope={history_scope_override!r} cannot be honored on backend "
                    f"{backend.name!r}: it resumes natively and tradewind feeds it no mirror "
                    "context"
                )
            history_scope: HistoryScope = (
                history_scope_override
                if history_scope_override is not None
                else ("flat" if feeds_mirror_context else "none")
            )
            effective_system_prompt = _effective_system_prompt(session_row, overrides)
            if (
                session_row.native_session_id is not None
                and backend.capabilities().supports_transcript_read
            ):
                # Reconcile before this turn's history is loaded (below) so
                # a human's out-of-band native activity (vendor CLI, DR-3)
                # is already part of the mirror this turn's context rebuilds
                # from (ARCHITECTURE §5.2).
                await self._resume_planner.reconcile(session_row, backend, self._store)
            broker = (
                options.permission_broker or self._config.permission_broker or _AllowAllBroker()
            )
            # `socket_dir` (task-12 brief, deferred wiring -- task-14 makes it
            # live): `ToolHostConfig.socket_dir` threads through to
            # `ToolHost.serve_socket()`'s socket location.
            #
            # `broker` is passed to `ToolHost` itself only for backends whose
            # tools are reachable *without* going through this backend's own
            # broker-consulting code first (currently: `codex`, whose tools
            # are called by an out-of-process engine over
            # `serve_socket()`'s unix socket -- see `ToolHost.__init__`'s
            # docstring). Claude/Langchain already gate every call before it
            # ever reaches `ToolHost.call()`, so passing the same broker in
            # for them too would consult it twice per tool call -- harmless
            # for a stateless allow/deny broker, but wrong for "ask"
            # semantics (blocks on a human; must not be asked twice).
            tool_host_broker = broker if backend.name == "codex" else None
            async with ToolHost(
                options.tools,
                options.mcp_servers,
                self._resolve_ref,
                socket_dir=self._config.tool_host.socket_dir,
                broker=tool_host_broker,
            ) as tool_host:
                # The prompt is this turn's own input, not something a
                # backend "completes" as an `ItemCompleted` event, so
                # nothing else mirrors it -- the runner writes it directly,
                # FIRST: its assigned `seq` anchors everything that used to
                # need an eager history read (the read this replaced was
                # paid on EVERY turn of EVERY backend, though only a
                # mirror-rebuilding backend ever consumed it -- FR-9.3
                # laziness). Always the CALLER's original `prompt`, never
                # `backend_prompt` (below): the mirror records what the
                # caller actually said, not tradewind's own request-shaping.
                prompt_content: dict[str, object] = {"text": prompt}
                prompt_seq = await anyio.to_thread.run_sync(
                    self._store.append_message,
                    session_id,
                    turn_id,
                    NormalizedMessage(role="user", kind="text", content=prompt_content),
                )
                # R-1 emulation (ARCHITECTURE §3.1). (1) The fold-fallback
                # path needs to know whether this is genuinely the
                # session's first turn (controller ruling in `_emulate_
                # system_prompt`'s own docstring) -- `seq` is the session-
                # wide message counter, so the prompt landing at seq 1 IS
                # that fact, with no history read. (2) `backend_prompt`
                # (the possibly-emulated text) is deliberately kept
                # SEPARATE from `prompt` (the caller's own, untouched
                # text): `backend_prompt` is what reaches `ctx`/the
                # backend, `prompt` is what was persisted above. Folding
                # `prompt` itself (the earlier, buggy shape -- fix round 1)
                # both re-applied the fold on every turn AND wrote the
                # `[Instructions]\n...\n[Task]\n...` wrapper into the
                # mirror as if the caller had typed it.
                backend_prompt = _emulate_system_prompt(
                    backend,
                    session_row,
                    effective_system_prompt,
                    prompt,
                    is_first_turn=prompt_seq == 1,
                )

                # Lazy by design (FR-9.3): the store is not read until a
                # backend actually awaits this -- native-resume backends
                # never do, so their turns cost no history query. The
                # `seq < prompt_seq` bound excludes this turn's own prompt
                # (the backend appends `ctx.prompt` itself when building
                # its request), replacing the old read-before-append
                # ordering guarantee.
                async def load_history() -> list[StoredMessage]:
                    if history_scope == "none":
                        return []
                    if history_scope == "flat":
                        rows = await anyio.to_thread.run_sync(
                            lambda: self._store.history(
                                session_id, include_children=False, include_raw=False
                            )
                        )
                        return [m for m in rows if m.seq < prompt_seq]
                    tree_rows = await anyio.to_thread.run_sync(
                        lambda: self._store.history(
                            session_id, include_children=True, include_raw=False
                        )
                    )
                    return _fold_child_history(session_id, tree_rows, prompt_seq)

                ctx = TurnContext(
                    session=session_row,
                    turn_id=turn_id,
                    prompt=backend_prompt,
                    model_spec=model_spec,
                    system_prompt=effective_system_prompt,
                    output_schema=_effective_output_schema(snapshot, overrides),
                    tools=tool_host,
                    broker=broker,
                    load_history=load_history,
                    compaction=self._config.defaults.compaction,
                    max_tool_rounds=max_tool_rounds,
                )

                # `aclosing` (not a bare `async for`) so that if THIS
                # generator (`execute()`) is itself abandoned/aclosed mid-turn
                # (a consumer `break`s out of `session.stream()` without
                # calling `stop()`), the backend's own generator is closed
                # deterministically right here -- rather than left to
                # whenever the garbage collector happens to finalize it -- so
                # a backend blocked on e.g. a network call unwinds its
                # `finally`s (cancel scopes, connections) immediately.
                # `Backend.run()`'s port signature is the broader
                # `AsyncIterator[Event]` (ports.py, task-8 brief, not
                # touched here), but every concrete implementation is an
                # `async def ... yield ...` generator (an `AsyncGenerator`,
                # which is what `aclosing` needs -- an `aclose()` method);
                # the cast reflects that real contract without widening the
                # port's own.
                backend_run = cast("AsyncGenerator[Event]", backend.run(ctx))
                async with aclosing(backend_run) as backend_events:
                    async for event in backend_events:
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
                                end_reason="interrupted",
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

                # Native-id rehome (task-11 brief; scoped per-session, fix
                # round 1 post-review): a backend that exposes
                # `take_native_session_id` (claude; not part of the
                # `Backend` ABC -- `getattr` default handles every backend
                # that doesn't) records, per tradewind session_id, the
                # native id its own SDK actually used for this turn as soon
                # as it has one, regardless of how the turn ended
                # (completed/failed/interrupted -- see
                # `ClaudeBackend._drive_client`'s `ResultMessage` handling).
                # `take_native_session_id` POPS that entry -- a backend
                # instance is cached and reused across every session on its
                # profile (`Tradewind._resolve_backend`), so a value must
                # never be read by more than the one turn that produced it
                # (fix round 1: a shared, un-scoped attribute let one
                # session's native id rehome a DIFFERENT session sharing the
                # same profile when that other session's turn ended without
                # ever recording its own). When the popped value differs
                # from what the session row already has (first native turn,
                # or the CLI minted a new native id on this resume),
                # re-home the row so the next turn's `reconcile()`/`resume=`
                # both target the current one.
                take_native_session_id = cast(
                    "Callable[[str], str | None] | None",
                    getattr(backend, "take_native_session_id", None),
                )
                new_native_session_id = (
                    take_native_session_id(session_id)
                    if take_native_session_id is not None
                    else None
                )
                if new_native_session_id is not None and (
                    new_native_session_id != session_row.native_session_id
                ):
                    await anyio.to_thread.run_sync(
                        self._store.rehome_native,
                        session_id,
                        backend.name,
                        new_native_session_id,
                    )
        except BaseException as exc:
            # `GeneratorExit` (the caller abandoning `session.stream()` --
            # `break`/early `aclose()` -- rather than draining it to a
            # terminal event) is deliberately excluded from setting
            # `terminal_status` here: its `str()` is always empty, so
            # classifying it as `failed` right here would both produce a
            # junk "turn runner: unhandled exception: " message AND
            # pre-empt the `finally` block's own interrupted-vs-failed
            # check below -- even when `request_stop()` already marked this
            # session's interrupt as pending, an abandonment-with-pending-
            # stop would then wrongly finalize as `failed` instead of
            # `interrupted` (fix round N, minor #5). Leaving
            # `terminal_status` unset here lets the `finally` block's own
            # `_interrupt_requested` check classify it correctly either way;
            # a genuine exception (anything else) still finalizes as
            # `failed` with its own message immediately, unchanged.
            if terminal_status is None and not isinstance(exc, GeneratorExit):
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
            # Shielded: this write must land even if the surrounding task is
            # itself being cancelled (e.g. the caller's own task group is
            # tearing down while this `finally` runs) -- an unfinalized turn
            # would otherwise sit `in_progress` until the next
            # `sweep_stale_turns` (session open) instead of recording what
            # actually happened.
            with anyio.CancelScope(shield=True):
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
            self._busy.discard(session_id)

    async def compact(self, session_id: str, instructions: str | None = None) -> StoredMessage:
        """Manual mirror compaction (FR-5.8 B3): summarize the session's
        older transcript into a checkpoint NOW, through the same machinery
        the automatic trigger uses, and append the record to the mirror.
        Available regardless of `CompactionSettings.auto` and without
        `ModelMeta` (the caller supplies the "when"). Returns the persisted
        record (with its assigned seq).

        Failure modes:
            SessionNotFound: `session_id` does not exist.
            Unsupported: the session's backend resumes natively (tradewind
                feeds it no mirror context) or exposes no compaction
                machinery.
            TurnInProgress: a turn or another compaction is in flight
                (I-5 single-flight, in-process).
            CompactionFailed: nothing to compact, or the summarizer's
                output was unusable (hard-fail rule).
        """
        session_row = await anyio.to_thread.run_sync(self._store.get_session, session_id)
        if session_row is None:
            raise SessionNotFound(session_id)
        profile = _resolve_profile(self._config, session_row.profile)
        snapshot = cast("dict[str, object]", session_row.options_snapshot)
        tier = _effective_tier(profile, snapshot, {}, self._config.defaults.tier)
        model_spec = profile.models[tier]
        backend = self._resolve_backend(session_row.profile, profile)
        if backend.capabilities().supports_native_resume:
            raise Unsupported(
                f"compact() cannot be honored on backend {backend.name!r}: it resumes "
                "natively and tradewind feeds it no mirror context (same doctrine as "
                "history_scope)"
            )
        compact_history = cast(
            "_CompactHistory | None",
            getattr(backend, "compact_history", None),
        )
        if compact_history is None:
            raise Unsupported(f"backend {backend.name!r} exposes no compaction machinery")
        if session_id in self._busy:
            raise TurnInProgress(f"session {session_id!r} is busy (turn or compaction in flight)")
        self._busy.add(session_id)
        try:
            history = await anyio.to_thread.run_sync(
                lambda: self._store.history(session_id, include_children=False, include_raw=False)
            )
            settings = self._config.defaults.compaction
            record, _usage = await compact_history(
                model_spec,
                history,
                keep_recent_tokens=settings.keep_recent_tokens,
                instructions=instructions,
            )
            record.session_id = session_id
            record.seq = await anyio.to_thread.run_sync(
                self._store.append_message, session_id, None, record
            )
            return record
        finally:
            self._busy.discard(session_id)

    def _resolve_ref(self, ref: str) -> str:
        key = ref.removeprefix("ref:")
        secret = self._config.secret_refs.get(key)
        if secret is None:
            raise ConfigError(
                f"unresolved secret ref {ref!r}: no entry in TradewindConfig.secret_refs"
            )
        return secret.get_secret_value()
