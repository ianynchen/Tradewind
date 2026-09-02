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

from collections.abc import AsyncIterator, Callable
from typing import ClassVar, cast

import anyio
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
from tradewind.domain.errors import Unsupported
from tradewind.domain.events import (
    Event,
    ItemCompleted,
    PermissionRequested,
    TextDelta,
    ThinkingDelta,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from tradewind.domain.models import (
    ApiKeyAuth,
    BackendName,
    Capabilities,
    ModelSpec,
    NormalizedMessage,
    Profile,
    SessionRow,
    StoredMessage,
    TurnResult,
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


def _rebuild_messages(ctx: TurnContext) -> list[BaseMessage]:
    """Rebuild the request's leading messages from the mirror
    (`ctx.load_history()`), per the component spec's "History" decision:
    the store is the conversation, so every turn after the first replays
    it in full rather than keeping provider-side state.

    `thinking` items are dropped (see module docstring). Consecutive
    `tool_use` items from one assistant turn are merged into a single
    `AIMessage(tool_calls=[...])` — Anthropic's API groups all of one
    turn's tool calls into one assistant message — while `tool_result`
    items each become their own `ToolMessage` (langchain-anthropic groups
    consecutive `ToolMessage`s into one API-side user turn itself).
    """
    messages: list[BaseMessage] = []
    if ctx.system_prompt is not None:
        messages.append(SystemMessage(content=ctx.system_prompt))

    pending_tool_calls: list[ToolCall] = []

    def flush_tool_calls() -> None:
        if pending_tool_calls:
            messages.append(AIMessage(content="", tool_calls=list(pending_tool_calls)))
            pending_tool_calls.clear()

    for stored in ctx.load_history():
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
    block_type = block.get("type")
    if block_type == "text":
        text = cast(str, block.get("text", ""))
        return TextDelta(text=text) if text else None
    if block_type == "reasoning":
        text = cast(str, block.get("reasoning", ""))
        return ThinkingDelta(text=text) if text else None
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
    return cast(BaseChatModel, chat_model.bind_tools(schemas))


def _accumulate_usage(totals: dict[str, int], usage: UsageMetadata | None) -> None:
    if usage is None:
        return
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        totals[key] = totals.get(key, 0) + cast(int, usage.get(key, 0))


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
        try:
            with scope:
                async for event in self._run_turn(ctx):
                    yield event
        finally:
            # Only pop the scope this call registered: a second `run()` on
            # the same `session_id` (e.g. this one interrupted, a new turn
            # started before this generator's `finally` runs) would already
            # have overwritten `self._scopes[session_id]` with its own
            # scope, and popping unconditionally here would drop that
            # newer scope out of the registry, leaving its own future
            # `interrupt()` a silent no-op.
            if self._scopes.get(session_id) is scope:
                self._scopes.pop(session_id, None)
        # Falling off the end here covers both a clean finish (the loop
        # below always yields its own terminal event) and an interrupt:
        # `scope.cancel()` unwinds the `async for` above via `Cancelled`,
        # `with scope:` swallows it (it is this scope's own cancellation),
        # and the generator ends here with no further event and no
        # exception (Backend.run contract).

    async def _run_turn(self, ctx: TurnContext) -> AsyncIterator[Event]:
        yield TurnStarted(turn_id=ctx.turn_id)
        try:
            chat_model = self._chat_model_factory(ctx.model_spec)
            schemas = ctx.tools.schemas()
            bound_model = _bind_tools(chat_model, schemas)
            messages = _rebuild_messages(ctx)
            messages.append(HumanMessage(content=ctx.prompt))

            usage_totals: dict[str, int] = {}
            final_text: str | None = None

            for _ in range(_MAX_ITERATIONS):
                gathered: AIMessageChunk | None = None
                async for chunk in bound_model.astream(messages):
                    for block in chunk.content_blocks:
                        delta = _delta_event(block)
                        if delta is not None:
                            yield delta
                    gathered = chunk if gathered is None else gathered + chunk
                if gathered is None:
                    raise RuntimeError("chat model produced no response")
                _accumulate_usage(usage_totals, gathered.usage_metadata)
                messages.append(gathered)

                for item in _completed_items(gathered):
                    yield ItemCompleted(message=item)

                tool_calls = gathered.tool_calls
                if not tool_calls:
                    final_text = _final_text(gathered)
                    break

                for call in tool_calls:
                    verdict = await ctx.broker.decide(call["name"], call["args"])
                    # `PermissionRequested` fires only on "deny", per the task-8
                    # brief's literal wording ("'deny' -> synthesized error
                    # ToolMessage + PermissionRequested event with verdict;
                    # 'allow' -> ToolHost.call") — flagged for controller
                    # confirmation in the task-8 report, since `Verdict` is
                    # typed to carry "allow" too.
                    if verdict == "deny":
                        yield PermissionRequested(
                            tool_name=call["name"], tool_input=call["args"], verdict="deny"
                        )
                        content, is_error = "permission denied", True
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
                    messages.append(
                        ToolMessage(
                            content=content,
                            tool_call_id=cast(str, call["id"]),
                            status="error" if is_error else "success",
                        )
                    )
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
                    final_text=final_text,
                    usage=usage_totals,
                    cost_usd=None,
                )
            )
        except Exception as exc:
            yield TurnFailed(turn_id=ctx.turn_id, error=str(exc))
