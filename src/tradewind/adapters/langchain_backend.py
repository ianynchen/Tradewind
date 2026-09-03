"""LangChain adapter: the first `Backend` (ARCHITECTURE §3.1) and template
for the other three (task-8 brief; docs/components/03-langchain-adapter.md).

Talks to the Anthropic API through `langchain-anthropic`'s `ChatAnthropic`
by default, but accepts any injected `langchain_core` `BaseChatModel` via
`chat_model_factory` — this is both how the fake-model unit tests below
drive the adapter without network access, and how a later task points the
same tool loop at Groq/Ollama (any OpenAI/Ollama-compatible
`BaseChatModel`) without touching this module.

Tradewind owns the tool loop itself (DR-1): no `AgentExecutor`/LangGraph.
Each turn rebuilds its initial messages array from the mirror
(`ctx.load_history()`) — the store is the conversation (component spec
"History" decision) — then runs live LangChain message objects through the
loop in memory, appending the model's own `AIMessage`/synthesized
`ToolMessage` objects as it goes; only the *rebuild* step (previous turns)
ever needs to reconstruct messages from scratch.

`NormalizedMessage.content` shapes this adapter reads/writes (its own
convention — the first adapter to define one; kept close to Anthropic's
native block vocabulary since that's what it talks to):
    kind="text"/"thinking":  {"text": str}
    kind="tool_use":         {"id": str, "name": str, "input": dict}
    kind="tool_result":      {"tool_use_id": str, "content": str, "is_error": bool}

Thinking is persisted (`ItemCompleted` still fires for it) but is the one
kind `_rebuild_messages` always drops (component spec "Thinking in rebuild"
decision): a signature the Anthropic API would require to replay it lives
only in the live response's `raw_json`, which the mirror-rebuild path
(`include_raw=False`) never has, so it is legally dropped rather than
re-sent broken.

Deferred: `ctx.output_schema` (tool-choice-forced structured output, per
the component spec's "Structured output" decision row) is accepted on
`TurnContext` but not yet wired into this adapter's request — flagged in
the task-8 report for controller follow-up, not silently dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import ClassVar, cast

import anyio
import httpx
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.ai import UsageMetadata
from langchain_core.messages.content import ContentBlock
from langchain_core.messages.tool import ToolCall
from langchain_core.messages.tool import tool_call as make_tool_call

from tradewind.application.config import NativeStoreConfig
from tradewind.application.ports import Backend, TurnContext
from tradewind.domain.compaction import (
    build_summary_request,
    compacted_view,
    estimate_tokens,
    find_cut_point,
    find_latest_compaction,
    merge_split_turn_summaries,
    serialize_for_summary,
    should_compact,
)
from tradewind.domain.errors import CompactionFailed, Unsupported
from tradewind.domain.events import (
    Event,
    ItemCompleted,
    PermissionRequested,
    TextDelta,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from tradewind.domain.models import (
    ApiKeyAuth,
    BackendName,
    Capabilities,
    EndReason,
    ModelSpec,
    NormalizedMessage,
    Profile,
    SessionRow,
    StoredMessage,
    TurnResult,
    calculate_cost,
    normalize_decision,
    resume_degraded_notice,
    retry_notice,
)

_MAX_ITERATIONS = 25

ChatModelFactory = Callable[[ModelSpec], BaseChatModel]


def _default_chat_model_factory(profile: Profile) -> ChatModelFactory:
    """The out-of-the-box `chat_model_factory`: a `ChatAnthropic` built from
    `profile.auth.api_key` and the turn's `ModelSpec.model`.

    Raises `Unsupported` at call time (not construction time) if the
    profile carries `SubscriptionAuth` — `langchain-anthropic` only speaks
    API-key auth; a subscription-auth profile must inject its own
    `chat_model_factory`.
    """

    def factory(model_spec: ModelSpec) -> BaseChatModel:
        auth = profile.auth
        if not isinstance(auth, ApiKeyAuth):
            raise Unsupported(
                f"LangchainBackend's default chat_model_factory requires ApiKeyAuth, "
                f"got auth.kind={auth.kind!r}; inject chat_model_factory for other auth kinds"
            )
        # `model_name` (not `model`) because mypy's pydantic plugin only
        # exposes `ChatAnthropic`'s field-name alias (`model_name`) in its
        # synthesized `__init__`, even though `populate_by_name=True` also
        # accepts the bare field name (`model=`) at runtime.
        #
        # `type: ignore[call-arg]`: the plugin also reports `timeout` and
        # `stop` as missing required arguments, even though both fields
        # declare `Field(None, alias=...)` (a default of `None`) and the
        # real Anthropic client accepts a bare `model_name`/`api_key` call
        # with everything else defaulted — confirmed empirically
        # (`ChatAnthropic(model_name=..., api_key=...)` constructs and
        # `.max_tokens` resolves from the model profile as documented).
        # Root cause: the pydantic-mypy plugin does not recognize a
        # positional default (`Field(None, alias=...)`, vs.
        # `Field(default=None, alias=...)`) on these two fields, so it
        # treats them as required in the synthesized `__init__`.
        return ChatAnthropic(model_name=model_spec.model, api_key=auth.api_key)  # type: ignore[call-arg]

    return factory


def _stored_text(stored: StoredMessage) -> str:
    return cast(str, stored.content.get("text", ""))


def _rebuild_messages(ctx: TurnContext, history: list[StoredMessage]) -> list[BaseMessage]:
    """Rebuild the request's leading messages from the mirror (`history` =
    the awaited `ctx.load_history()`, shaped by the turn's `history_scope`
    -- FR-9.3), per the component spec's "History" decision: the store is
    the conversation, so every turn after the first replays it in full
    rather than keeping provider-side state.

    `thinking` items are dropped (see module docstring). Consecutive
    `tool_use` items from one assistant turn are merged into a single
    `AIMessage(tool_calls=[...])` — Anthropic's API groups all of one
    turn's tool calls into one assistant message — while `tool_result`
    items each become their own `ToolMessage` (langchain-anthropic groups
    consecutive `ToolMessage`s into one API-side user turn itself).
    """
    summary, retained = compacted_view(history)
    messages: list[BaseMessage] = []
    if ctx.system_prompt is not None:
        messages.append(SystemMessage(content=ctx.system_prompt))
    if summary is not None:
        # FR-5.8 rebuild rule: the latest compaction record renders as a
        # user message carrying the checkpoint; older rows are omitted
        # from the FEED only -- the mirror keeps every row.
        messages.append(
            HumanMessage(
                content=(
                    "The conversation history before this point was compacted "
                    f"into the following summary:\n\n<summary>\n{summary}\n</summary>"
                )
            )
        )
    history = retained

    pending_tool_calls: list[ToolCall] = []

    def flush_tool_calls() -> None:
        if pending_tool_calls:
            messages.append(AIMessage(content="", tool_calls=list(pending_tool_calls)))
            pending_tool_calls.clear()

    for stored in history:
        if stored.kind == "thinking":
            continue
        if stored.kind == "tool_use":
            pending_tool_calls.append(
                make_tool_call(
                    name=cast(str, stored.content["name"]),
                    args=cast("dict[str, object]", stored.content.get("input", {})),
                    id=cast("str | None", stored.content.get("id")),
                )
            )
            continue
        flush_tool_calls()
        if stored.kind == "tool_result":
            messages.append(
                ToolMessage(
                    content=cast(str, stored.content.get("content", "")),
                    tool_call_id=cast(str, stored.content["tool_use_id"]),
                    status="error" if stored.content.get("is_error") else "success",
                )
            )
        elif stored.kind == "text":
            text = _stored_text(stored)
            if stored.role == "assistant":
                messages.append(AIMessage(content=text))
            else:
                messages.append(HumanMessage(content=text))
        # Other kinds (command_execution, file_change, plan, web_search,
        # compaction, event) are never written by this adapter and have no
        # LangChain message equivalent here; skipped rather than guessed at.
    flush_tool_calls()
    return messages


def _delta_event(block: ContentBlock) -> Event | None:
    """Streamed-chunk delta for one content block, or `None` for a block
    type this adapter doesn't stream a live signal for.

    `"reasoning"` chunks are deliberately unmapped here (task-11 taxonomy
    freeze, `domain.events` module docstring): nothing in this adapter's
    request construction enables Anthropic extended thinking today, so this
    branch never actually sees one -- the completed reasoning block, if a
    caller enables it themselves via `chat_model_factory`, still lands as a
    persisted `kind="thinking"` `ItemCompleted` (`_completed_items` below),
    only its live delta is not separately eventized.
    """
    block_type = block.get("type")
    if block_type == "text":
        text = cast(str, block.get("text", ""))
        return TextDelta(text=text) if text else None
    return None


def _completed_items(message: AIMessage) -> list[NormalizedMessage]:
    """Normalize one model response's content blocks into the
    `ItemCompleted` items this turn persists — including `thinking`, which
    is persisted here and only dropped later, at rebuild time."""
    items: list[NormalizedMessage] = []
    for block in message.content_blocks:
        block_type = block.get("type")
        if block_type == "text":
            text = cast(str, block.get("text", ""))
            if text:
                items.append(
                    NormalizedMessage(role="assistant", kind="text", content={"text": text})
                )
        elif block_type == "reasoning":
            text = cast(str, block.get("reasoning", ""))
            if text:
                items.append(
                    NormalizedMessage(role="assistant", kind="thinking", content={"text": text})
                )
        elif block_type == "tool_call":
            items.append(
                NormalizedMessage(
                    role="assistant",
                    kind="tool_use",
                    content={
                        "id": block.get("id"),
                        "name": block.get("name", ""),
                        "input": block.get("args", {}),
                    },
                )
            )
    return items


def _final_text(message: AIMessage) -> str | None:
    texts = [
        cast(str, block.get("text", ""))
        for block in message.content_blocks
        if block.get("type") == "text"
    ]
    joined = "".join(texts)
    return joined if joined else None


def _stop_reason(message: AIMessage) -> str | None:
    """The provider's own stop reason off a gathered response
    (`response_metadata["stop_reason"]` -- langchain-anthropic sets it on
    the final streamed chunk; None when the provider reported none)."""
    value = message.response_metadata.get("stop_reason")
    return value if isinstance(value, str) else None


def _bind_tools(chat_model: BaseChatModel, schemas: list[dict[str, object]]) -> BaseChatModel:
    """Bind `schemas` (Anthropic-format tool dicts, `ToolHost.schemas()`'s
    own output) to `chat_model`, or return it unchanged when there are none.

    `BaseChatModel.bind_tools()` actually returns a `Runnable` wrapper
    (`Runnable[LanguageModelInput, AIMessage]`) — a different generic shape
    than `BaseChatModel` that `langchain-core`'s own typing does not unify
    with the unbound branch. This adapter only ever calls `.astream()` on
    the result either way (a method every `Runnable` provides), so the cast
    back to `BaseChatModel` here keeps that one call site's static type
    unambiguous for the rest of `_run_turn` rather than hand-carrying
    LangChain's own generic `Runnable` typing through this whole module.
    """
    if not schemas:
        return chat_model
    try:
        return cast(BaseChatModel, chat_model.bind_tools(schemas))
    except NotImplementedError as exc:
        # `BaseChatModel.bind_tools` raises a bare `NotImplementedError`
        # whose `str()` is EMPTY -- left uncaught it became a
        # `TurnFailed("")` with no message at all (sextant integration
        # repro, 2026-09-02: `GenericFakeChatModel` has no `bind_tools`).
        # Tools were registered, the model cannot take them: fail loudly
        # with a message that names the operation (FR-1.2, GUIDELINES §9),
        # never proceed unbound as if the caller had registered nothing.
        raise Unsupported(
            f"chat model {type(chat_model).__name__} does not implement bind_tools, but "
            f"{len(schemas)} tool(s) were registered for this turn; inject a chat_model_factory "
            f"whose model supports tool binding (a scripted test fake can subclass it with a "
            f"no-op bind_tools returning self)"
        ) from exc


def _accumulate_usage(totals: dict[str, int], usage: UsageMetadata | None) -> None:
    if usage is None:
        return
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        totals[key] = totals.get(key, 0) + cast(int, usage.get(key, 0))


# FR-6.6 retry classification: typed-first (status codes and transport
# error types), failing CLOSED -- an unclassified error never retries.
# Pi's pattern lists survive only as the narrow string fallback for
# transient classes that arrive without a status attribute.
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504, 529})
_OVERFLOW_MARKERS = (
    "prompt is too long",
    "context length",
    "context window",
    "maximum context",
    "too many tokens",
)
_TRANSIENT_MARKERS = ("overloaded", "connection reset", "connection error", "timed out")


def _classify_error(exc: Exception) -> str:
    """One of "overflow" | "retryable" | "fatal" (FR-6.6)."""
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    if (status == 400 or status is None) and any(m in text for m in _OVERFLOW_MARKERS):
        return "overflow"
    if isinstance(status, int):
        return "retryable" if status in _RETRYABLE_STATUS else "fatal"
    if isinstance(exc, ConnectionError | TimeoutError | httpx.TransportError):
        return "retryable"
    if any(m in text for m in _TRANSIENT_MARKERS):
        return "retryable"
    return "fatal"


class LangchainBackend(Backend):
    """`Backend` for the Anthropic API via `langchain-anthropic`, running
    Tradewind's own broker-gated tool loop (component spec, DR-1)."""

    name: ClassVar[BackendName] = "langchain"

    def __init__(
        self,
        profile: Profile,
        native_config: NativeStoreConfig,
        *,
        chat_model_factory: ChatModelFactory | None = None,
    ) -> None:
        super().__init__(profile, native_config)
        self._chat_model_factory = chat_model_factory or _default_chat_model_factory(profile)
        self._scopes: dict[str, anyio.CancelScope] = {}

    def capabilities(self) -> Capabilities:
        # Exactly the table in docs/components/03-langchain-adapter.md.
        # `supports_structured_output=False`: tool-choice-forced structured
        # output is deferred (controller ruling, task-8 fix round 1) — the
        # design for *where* in the loop forcing applies is not yet made,
        # so the flag stays honest rather than advertising unimplemented
        # behavior. `run()` raises `Unsupported` if a caller asks for it
        # anyway via `ctx.output_schema`.
        return Capabilities(
            supports_system_prompt=True,
            supports_structured_output=False,
            supports_interactive_permissions=True,
            supports_in_process_tools=True,
            supports_native_resume=False,
            supports_fork=False,
            supports_transcript_read=False,
            # The tool loop is this adapter's own (`_run_turn`), so the cap
            # is enforced exactly: `ctx.max_tool_rounds` executed rounds,
            # then an honest `end_reason="max_tool_rounds"` completion.
            supports_tool_round_cap=True,
            # Mid-turn model-call retry is honest here (FR-6.6): the loop
            # is tradewind's own, and the retry unit is one model call --
            # completed tool executions are never re-run.
            supports_turn_retry=True,
            # FR-4.4: the denial reason is the synthesized tool_result text.
            supports_deny_reason=True,
        )

    async def probe_native(
        self,
        session: SessionRow,  # noqa: ARG002 -- part of the `Backend` interface; unused, see below
    ) -> bool:
        # No native store of record on this backend (component spec Purpose):
        # the answer is always False, regardless of `session`.
        return False

    async def read_native_transcript(
        self,
        session: SessionRow,  # noqa: ARG002 -- part of the `Backend` interface; unused, see below
        after_native_id: str | None,  # noqa: ARG002 -- same
    ) -> list[NormalizedMessage]:
        raise Unsupported(
            "LangchainBackend has no native transcript (supports_transcript_read=False)"
        )

    async def interrupt(self, session_id: str) -> None:
        scope = self._scopes.get(session_id)
        if scope is not None:
            scope.cancel()

    async def run(self, ctx: TurnContext) -> AsyncIterator[Event]:
        if ctx.output_schema is not None:
            # Raised before `TurnStarted`, not surfaced as `TurnFailed`:
            # this is a capability mismatch (`supports_structured_output`
            # is False), the same kind of error `read_native_transcript`
            # raises directly rather than emitting an event for.
            raise Unsupported("structured output not yet implemented for langchain backend")

        session_id = ctx.session.session_id
        scope = anyio.CancelScope()
        self._scopes[session_id] = scope

        # The turn runs in a dedicated pump task, streaming its events out
        # through a zero-buffer memory channel, so the `CancelScope` is
        # entered AND exited inside that one task. The earlier shape --
        # `with scope:` wrapped around `yield` inside this async generator --
        # was the documented anyio pitfall: when a consumer abandons the
        # stream (e.g. `Session.run` raising on a `TurnFailed`), asyncio's
        # async-generator finalizer delivers `GeneratorExit` at the yield
        # point FROM ITS OWN TASK, and the scope then exits in a different
        # task than it was entered in ("Attempted to exit cancel scope in a
        # different task...", sextant integration repro, 2026-09-02).
        # `asyncio.create_task` (not an anyio task group) deliberately: a
        # task group is itself a cancel scope and would recreate the exact
        # same across-`yield` hazard; `CodexBackend` already sets the
        # asyncio-native precedent in this repo.
        send, receive = anyio.create_memory_object_stream[Event](0)

        async def pump() -> None:
            with scope:
                async with send:
                    async for event in self._run_turn(ctx):
                        await send.send(event)
            # Falling off the end covers both a clean finish (`_run_turn`
            # always yields its own terminal event) and an interrupt:
            # `scope.cancel()` unwinds `send.send`/`_run_turn` via
            # `Cancelled`, `with scope:` swallows it (its own cancellation),
            # and closing `send` ends the consumer loop below with no
            # further event and no exception (Backend.run contract).

        pump_task = asyncio.get_running_loop().create_task(pump())
        try:
            async with receive:
                async for event in receive:
                    yield event
            # Surface a pump crash (a bug escaping `_run_turn`'s own
            # `except Exception` -> `TurnFailed` net) instead of silently
            # ending the stream; on any normal end this is already done.
            await pump_task
        finally:
            if not pump_task.done():
                scope.cancel()
                # Shielded so the pump's own `finally`s complete even when
                # this generator is being finalized under cancellation or
                # from asyncio's async-generator finalizer task; the shield
                # scope is entered and exited entirely inside this block, in
                # whichever single task runs it, so it never trips the
                # cross-task check this restructure exists to avoid.
                with anyio.CancelScope(shield=True), contextlib.suppress(BaseException):
                    await pump_task
            # Only pop the scope this call registered: a second `run()` on
            # the same `session_id` (e.g. this one interrupted, a new turn
            # started before this generator's `finally` runs) would already
            # have overwritten `self._scopes[session_id]` with its own
            # scope, and popping unconditionally here would drop that
            # newer scope out of the registry, leaving its own future
            # `interrupt()` a silent no-op.
            if self._scopes.get(session_id) is scope:
                self._scopes.pop(session_id, None)

    async def _run_turn(self, ctx: TurnContext) -> AsyncIterator[Event]:
        yield TurnStarted(turn_id=ctx.turn_id)
        if ctx.force_replay:
            # FR-6.1: cross-backend continuation landed here -- the mirror
            # rebuild below IS the (lossless) replay; the event records it.
            yield ItemCompleted(
                message=resume_degraded_notice(
                    reason="cross-backend continuation: lossless mirror rebuild on langchain"
                )
            )
        try:
            chat_model = self._chat_model_factory(ctx.model_spec)
            schemas = ctx.tools.schemas()
            bound_model = _bind_tools(chat_model, schemas)
            history = await ctx.load_history()
            record, failure_item = await self._maybe_auto_compact(ctx, history)
            if failure_item is not None:
                # Auto-compaction failed (FR-5.8 hard-fail rule): surfaced
                # loudly as a mirrored event item, turn proceeds uncompacted.
                yield ItemCompleted(message=failure_item)
            if record is not None:
                # The record reaches the mirror through the ordinary
                # ItemCompleted path (Kind="compaction"); locally it joins
                # the in-memory history so THIS turn already rebuilds from
                # summary + retained tail.
                yield ItemCompleted(message=record)
                history = [*history, record]
            # Split base (rebuilt history) from this turn's suffix so
            # overflow recovery (FR-6.6) can re-derive the base from a
            # freshly compacted view WITHOUT losing in-turn messages.
            base_messages = _rebuild_messages(ctx, history)
            turn_suffix: list[BaseMessage] = [HumanMessage(content=ctx.prompt)]

            usage_totals: dict[str, int] = {}
            final_text: str | None = None
            end_reason: EndReason = "end_turn"
            rounds_executed = 0

            overflow_recovered = False
            for _ in range(_MAX_ITERATIONS):
                retries_used = 0
                while True:
                    try:
                        gathered: AIMessageChunk | None = None
                        async for chunk in bound_model.astream([*base_messages, *turn_suffix]):
                            for block in chunk.content_blocks:
                                delta = _delta_event(block)
                                if delta is not None:
                                    yield delta
                            gathered = chunk if gathered is None else gathered + chunk
                        if gathered is None:
                            raise RuntimeError("chat model produced no response")
                        break
                    except Exception as exc:
                        classification = _classify_error(exc)
                        if classification == "overflow" and not overflow_recovered:
                            # FR-6.6 overflow recovery, Pi §2.3: ONE
                            # compact-then-retry per turn, distinct from the
                            # retry budget; the error itself is the trigger,
                            # so it needs neither ModelMeta nor auto=True. A
                            # failed recovery compaction fails the turn with
                            # ITS error, loudly.
                            overflow_recovered = True
                            keep_recent = (
                                ctx.compaction.keep_recent_tokens
                                if ctx.compaction is not None
                                else 20000
                            )
                            try:
                                recovery_record, _usage = await self._compact(
                                    ctx.model_spec, history, keep_recent_tokens=keep_recent
                                )
                            except CompactionFailed as compaction_exc:
                                raise CompactionFailed(
                                    "context overflow and recovery compaction failed: "
                                    f"{compaction_exc}"
                                ) from exc
                            yield ItemCompleted(message=recovery_record)
                            yield ItemCompleted(
                                message=retry_notice(
                                    phase="overflow_recovery",
                                    attempt=1,
                                    max_attempts=1,
                                    delay_s=0.0,
                                    error=str(exc),
                                )
                            )
                            history = [*history, recovery_record]
                            base_messages = _rebuild_messages(ctx, history)
                            continue
                        retry_settings = ctx.retry
                        if (
                            classification == "retryable"
                            and retry_settings is not None
                            and retries_used < retry_settings.max_attempts
                        ):
                            retries_used += 1
                            delay = retry_settings.base_delay_s * 2 ** (retries_used - 1)
                            yield ItemCompleted(
                                message=retry_notice(
                                    phase="model_call",
                                    attempt=retries_used,
                                    max_attempts=retry_settings.max_attempts,
                                    delay_s=delay,
                                    error=str(exc),
                                )
                            )
                            await anyio.sleep(delay)
                            continue
                        raise
                _accumulate_usage(usage_totals, gathered.usage_metadata)
                turn_suffix.append(gathered)

                for item in _completed_items(gathered):
                    yield ItemCompleted(message=item)

                if _stop_reason(gathered) == "max_tokens":
                    # The provider truncated this response mid-generation
                    # (FR-6.5): any tool calls on it may themselves be
                    # truncated, so none are executed. An honest
                    # `max_tokens` completion, never reported as a clean
                    # `end_turn`.
                    final_text = _final_text(gathered)
                    end_reason = "max_tokens"
                    break

                tool_calls = gathered.tool_calls
                if not tool_calls:
                    final_text = _final_text(gathered)
                    break

                if ctx.max_tool_rounds is not None and rounds_executed >= ctx.max_tool_rounds:
                    # The caller's cap stops the loop before another round
                    # executes (FR-6.5): the requested-but-unexecuted
                    # `tool_use` items above were already emitted/mirrored,
                    # and the turn completes as an honest partial.
                    final_text = _final_text(gathered)
                    end_reason = "max_tool_rounds"
                    break

                terminate_requested = False
                for call in tool_calls:
                    decision = await ctx.broker.decide(call["name"], call["args"])
                    verdict, deny_reason, terminate = normalize_decision(decision)
                    # `PermissionRequested` fires only on "deny", per the task-8
                    # brief's literal wording ("'deny' -> synthesized error
                    # ToolMessage + PermissionRequested event with verdict;
                    # 'allow' -> ToolHost.call") — flagged for controller
                    # confirmation in the task-8 report, since `Verdict` is
                    # typed to carry "allow" too.
                    if verdict == "deny":
                        yield PermissionRequested(
                            tool_name=call["name"],
                            tool_input=call["args"],
                            verdict="deny",
                            reason=deny_reason,
                        )
                        # FR-4.4: the reason IS what the model reads.
                        content, is_error = (deny_reason or "permission denied"), True
                        terminate_requested = terminate_requested or terminate
                    else:
                        outcome = await ctx.tools.call(call["name"], call["args"])
                        content, is_error = outcome.content, outcome.is_error

                    yield ItemCompleted(
                        message=NormalizedMessage(
                            role="tool",
                            kind="tool_result",
                            content={
                                "tool_use_id": call["id"],
                                "content": content,
                                "is_error": is_error,
                            },
                        )
                    )
                    turn_suffix.append(
                        ToolMessage(
                            content=content,
                            tool_call_id=cast(str, call["id"]),
                            status="error" if is_error else "success",
                        )
                    )
                # Not `enumerate()` (SIM113): the loop variable counts model
                # calls, while this counts *completed tool rounds* -- every
                # `break` above exits before the increment, so at the cap
                # check it equals rounds actually executed, not iterations.
                rounds_executed += 1
                if terminate_requested:
                    # FR-4.4 terminate, Pi's after-the-batch rule: every
                    # result in the batch (denials included) was delivered
                    # and mirrored; the turn then ends as an honest partial.
                    final_text = _final_text(gathered)
                    end_reason = "broker_terminated"
                    break
            else:
                yield TurnFailed(
                    turn_id=ctx.turn_id,
                    error=f"max iterations ({_MAX_ITERATIONS}) reached without a final response",
                )
                return

            yield TurnCompleted(
                result=TurnResult(
                    turn_id=ctx.turn_id,
                    status="completed",
                    end_reason=end_reason,
                    final_text=final_text,
                    usage=usage_totals,
                    cost_usd=self._computed_cost(ctx.model_spec, usage_totals),
                )
            )
        except Exception as exc:
            yield TurnFailed(turn_id=ctx.turn_id, error=str(exc))

    # --- compaction (FR-5.8) -------------------------------------------

    def _computed_cost(self, model_spec: ModelSpec, usage_totals: dict[str, int]) -> float | None:
        """`TurnResult.cost_usd` from the tier's cost table (FR-10.5) --
        None without one (honest absence); the turn's OWN tokens only.
        Summarizer spend lives on the compaction record instead (Phase-2a
        amendment: `tw.usage()` sums turns + records with no overlap). On
        a subscription profile the figure is the API-EQUIVALENT price of
        the tokens used, not billed spend (user decision 2026-09-03)."""
        meta = model_spec.meta
        if meta is None or meta.cost is None:
            return None
        return calculate_cost(
            meta.cost,
            input_tokens=usage_totals.get("input_tokens", 0),
            output_tokens=usage_totals.get("output_tokens", 0),
        )

    async def _maybe_auto_compact(
        self, ctx: TurnContext, history: list[StoredMessage]
    ) -> tuple[StoredMessage | None, NormalizedMessage | None]:
        """The automatic trigger (FR-5.8 B3): fires only when settings allow
        (`auto=True`), the tier declares a `context_window`, and the chars/4
        estimate of the ABOUT-TO-BE-FED context (summary + retained tail +
        this prompt) trips Pi's `window - reserve` predicate. Returns
        (compaction record, failure event item, summarizer usage) -- at most
        one of the first two is set."""
        settings = ctx.compaction
        meta = ctx.model_spec.meta
        if settings is None or not settings.auto or meta is None:
            return None, None
        overhead = len(ctx.prompt) // 4 + (
            len(ctx.system_prompt) // 4 if ctx.system_prompt is not None else 0
        )
        # FR-5.8 trigger upgrade (Phase 2b): provider-reported tokens beat
        # the chars/4 estimate where available -- the last turn's reported
        # total covers everything through that turn's end; only rows AFTER
        # it are estimated. Pi's staleness guard, ported to seq-space: a
        # compaction record NEWER than that turn's last row means the
        # reported number is pre-compaction -- ignore it (it would
        # re-trigger immediately right after compacting).
        context_tokens: int | None = None
        if ctx.last_turn_usage is not None and ctx.last_turn_id is not None:
            reported_total = ctx.last_turn_usage.get("total_tokens", 0)
            last_turn_indices = [i for i, m in enumerate(history) if m.turn_id == ctx.last_turn_id]
            if reported_total > 0 and last_turn_indices:
                last_index = last_turn_indices[-1]
                stale = any(m.kind == "compaction" for m in history[last_index + 1 :])
                if not stale:
                    trailing = [m for m in history[last_index + 1 :] if m.kind != "compaction"]
                    context_tokens = reported_total + estimate_tokens(trailing) + overhead
        if context_tokens is None:
            summary, retained = compacted_view(history)
            context_tokens = (
                estimate_tokens(retained)
                + (len(summary) // 4 if summary is not None else 0)
                + overhead
            )
        if not should_compact(context_tokens, meta.context_window, settings.reserve_tokens):
            return None, None
        try:
            record, _usage = await self._compact(
                ctx.model_spec, history, keep_recent_tokens=settings.keep_recent_tokens
            )
        except CompactionFailed as exc:
            failure = NormalizedMessage(
                role="assistant",
                kind="event",
                content={"type": "compaction_failed", "error": str(exc)},
            )
            return None, failure
        return record, None

    async def compact_history(
        self,
        model_spec: ModelSpec,
        history: list[StoredMessage],
        *,
        keep_recent_tokens: int,
        instructions: str | None = None,
    ) -> tuple[StoredMessage, dict[str, int]]:
        """Manual compaction entry point (FR-5.8 B3), duck-typed from the
        turn runner (same pattern as `take_native_session_id`: not part of
        the `Backend` ABC). Works without `ModelMeta` -- the caller supplies
        the "when". Returns the un-persisted compaction record (the runner
        appends it to the mirror) and the summarizer usage.

        Failure modes:
            CompactionFailed: nothing to compact, or the summarizer's
                output was truncated/unusable (hard-fail rule -- a broken
                summary must never become a checkpoint).
        """
        return await self._compact(
            model_spec, history, keep_recent_tokens=keep_recent_tokens, instructions=instructions
        )

    async def _compact(
        self,
        model_spec: ModelSpec,
        history: list[StoredMessage],
        *,
        keep_recent_tokens: int,
        instructions: str | None = None,
    ) -> tuple[StoredMessage, dict[str, int]]:
        previous = find_latest_compaction(history)
        previous_summary = str(previous.content["summary"]) if previous is not None else None
        # Chaining (FR-5.8 B6, Pi's rule): re-summarize the previously-kept
        # tail plus everything since -- `compacted_view` IS that window --
        # never the old summary as conversation; it rides along as
        # <previous-summary> instead.
        _, view_rows = compacted_view(history)
        cut = find_cut_point(view_rows, keep_recent_tokens)
        if cut is None:
            raise CompactionFailed(
                "nothing to compact: the transcript already fits within keep_recent_tokens"
            )
        boundary = cut.turn_start_index if cut.is_split_turn else cut.first_kept_index
        model = self._chat_model_factory(model_spec)
        system_prompt, user_text = build_summary_request(
            serialize_for_summary(view_rows[:boundary]),
            previous_summary=previous_summary,
            instructions=instructions,
        )
        summary_text, usage = await self._invoke_summarizer(model, system_prompt, user_text)
        if cut.is_split_turn:
            prefix_system, prefix_text_req = build_summary_request(
                serialize_for_summary(view_rows[cut.turn_start_index : cut.first_kept_index]),
                turn_prefix=True,
            )
            prefix_summary, prefix_usage = await self._invoke_summarizer(
                model, prefix_system, prefix_text_req
            )
            summary_text = merge_split_turn_summaries(summary_text, prefix_summary)
            for key, value in prefix_usage.items():
                usage[key] = usage.get(key, 0) + value
        meta = model_spec.meta
        summarizer_cost = (
            calculate_cost(
                meta.cost,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            )
            if meta is not None and meta.cost is not None
            else None
        )
        record = StoredMessage(
            role="user",
            kind="compaction",
            content={
                "summary": summary_text,
                "first_kept_seq": view_rows[cut.first_kept_index].seq,
                "tokens_before": estimate_tokens(view_rows),
                "summarizer_usage": dict(usage),
                # Computed at compact time so manual-compact spend is never
                # lost to a later profile change (Phase-2a); None without a
                # cost table -- honest absence.
                "summarizer_cost_usd": summarizer_cost,
            },
        )
        return record, usage

    async def _invoke_summarizer(
        self, model: BaseChatModel, system_prompt: str, user_text: str
    ) -> tuple[str, dict[str, int]]:
        """One summarization call, with Pi's hard-fail honesty rules: a
        `max_tokens` stop or a tool-call response is a failure, never a
        checkpoint. No explicit output cap is set (documented Phase-1
        divergence: `BaseChatModel` has no portable per-call max_tokens;
        the model's own default applies and the stop_reason check guards
        truncation)."""
        response = await model.ainvoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=user_text)]
        )
        if _stop_reason(response) == "max_tokens":
            raise CompactionFailed(
                "summarizer output was truncated at its token cap; a truncated "
                "summary must not become a checkpoint"
            )
        if getattr(response, "tool_calls", None):
            raise CompactionFailed("summarizer returned tool calls instead of a summary")
        text = _final_text(response)
        if text is None:
            raise CompactionFailed("summarizer returned empty output")
        usage: dict[str, int] = {}
        _accumulate_usage(usage, response.usage_metadata)
        return text, usage
