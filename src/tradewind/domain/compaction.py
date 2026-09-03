"""Pure compaction core (FR-5.8): cut-point selection, token estimation,
transcript serialization, and the summarizer prompts.

Stdlib-only and side-effect free — domain-pure by the import-linter
contract — so the same core serves the langchain adapter today and REPLAY
(ARCHITECTURE P-7) later. Algorithm ported from Pi's compaction
(docs/research/2026-09-03-pi-implementation-notes.md §1; Pi is MIT,
github.com/earendil-works/pi — concepts re-implemented, not copied).

Two deliberate simplifications versus Pi, both from the approved Phase-1
spec: split-turn detection is EXACT (mirror rows carry `turn_id`; Pi has
to infer turn starts), and token counting is the chars/4 conservative
estimate only (it overestimates, so compaction fires early, never late).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from tradewind.domain.models import StoredMessage

# Pi's deliberate heuristic: "conservative — overestimates".
_CHARS_PER_TOKEN = 4
# Tool results are truncated in the summarizer's serialized view (Pi: 2000).
_TOOL_RESULT_SERIALIZE_CHARS = 2000


def estimate_message_tokens(message: StoredMessage) -> int:
    """chars/4 over the message's serialized content — every kind counts,
    including tool_use name+args (Pi's estimator does the same)."""
    return max(1, len(json.dumps(message.content)) // _CHARS_PER_TOKEN)


def estimate_tokens(messages: list[StoredMessage]) -> int:
    return sum(estimate_message_tokens(message) for message in messages)


def should_compact(estimated_tokens: int, context_window: int, reserve_tokens: int) -> bool:
    """Pi's trigger predicate: context exceeds `window - reserve`."""
    return estimated_tokens > context_window - reserve_tokens


def is_cut_point(message: StoredMessage) -> bool:
    """A cut may land on any message EXCEPT a tool_result (it must follow
    its tool_use; cutting at an assistant message with tool calls keeps
    the results with it) — Pi's never-split rule."""
    return message.kind != "tool_result"


@dataclass(frozen=True)
class CutPoint:
    """Where compaction cuts. Everything before `first_kept_index` is
    summarized; `is_split_turn` means the cut lands inside a turn, whose
    prefix `[turn_start_index, first_kept_index)` gets its own summary."""

    first_kept_index: int
    turn_start_index: int
    is_split_turn: bool


def find_cut_point(messages: list[StoredMessage], keep_recent_tokens: int) -> CutPoint | None:
    """Walk backwards from the newest message accumulating estimated
    tokens; once `keep_recent_tokens` is reached, cut at the nearest valid
    cut point at-or-after that message (a SOFT floor on the retained tail
    — the tail is at least that big and extends to a safe boundary).

    Returns None when there is nothing to summarize: the budget was never
    reached, or the cut would land at index 0 (everything retained).

    Split-turn detection is exact: the cut splits a turn when the cut
    message shares a non-None `turn_id` with the message before it;
    `turn_start_index` walks back to that turn's first message.
    """
    if not messages:
        return None
    accumulated = 0
    budget_index = len(messages) - 1
    for index in range(len(messages) - 1, -1, -1):
        accumulated += estimate_message_tokens(messages[index])
        if accumulated >= keep_recent_tokens:
            budget_index = index
            break
    else:
        return None  # budget never reached: keep everything

    first_kept = next(
        (i for i in range(budget_index, len(messages)) if is_cut_point(messages[i])), None
    )
    if first_kept is None or first_kept == 0:
        return None

    turn_start = first_kept
    cut_turn_id = messages[first_kept].turn_id
    is_split = cut_turn_id is not None and messages[first_kept - 1].turn_id == cut_turn_id
    if is_split:
        while turn_start > 0 and messages[turn_start - 1].turn_id == cut_turn_id:
            turn_start -= 1
    return CutPoint(
        first_kept_index=first_kept, turn_start_index=turn_start, is_split_turn=is_split
    )


def serialize_for_summary(messages: list[StoredMessage]) -> str:
    """The conversation as PLAIN TEXT for the summarizer — deliberately not
    chat messages, so the model cannot try to continue it (Pi §1.5). Prior
    compaction records are excluded (chaining feeds their summary through
    `<previous-summary>` instead, never as conversation)."""
    lines: list[str] = []
    for message in messages:
        content = message.content
        if message.kind == "compaction":
            continue
        if message.kind == "thinking":
            lines.append(f"[Assistant thinking]: {content.get('text', '')}")
        elif message.kind == "tool_use":
            args = json.dumps(content.get("input", {}))
            lines.append(f"[Assistant tool calls]: {content.get('name')}({args})")
        elif message.kind == "tool_result":
            text = str(content.get("content", ""))[:_TOOL_RESULT_SERIALIZE_CHARS]
            lines.append(f"[Tool result]: {text}")
        elif message.role == "user":
            lines.append(f"[User]: {content.get('text', '')}")
        else:
            lines.append(
                f"[{message.role.capitalize()} {message.kind}]: {content.get('text', json.dumps(content))}"
            )
    return "\n".join(lines)


SUMMARIZER_SYSTEM_PROMPT = (
    "You are a context summarization assistant. You will be shown a "
    "serialized conversation transcript. Do NOT continue the conversation. "
    "ONLY output the structured summary in the requested format."
)

_CHECKPOINT_FORMAT = (
    "Summarize the conversation as a structured checkpoint with exactly "
    "these sections:\n"
    "## Goal\n## Constraints & Preferences\n"
    "## Progress\n(subsections: Done / In Progress / Blocked)\n"
    "## Key Decisions\n## Next Steps\n## Critical Context\n"
    "Preserve exact file paths, function names, identifiers, and error "
    "messages verbatim."
)

_UPDATE_VARIANT = (
    "A previous checkpoint summary is provided in <previous-summary>. "
    "UPDATE it: PRESERVE all still-relevant existing information, move "
    "items from In Progress to Done where the newer conversation shows "
    "completion, and fold in everything new."
)

TURN_PREFIX_PROMPT = (
    "This is the PREFIX of a single turn that was too large to keep "
    "whole. The SUFFIX (the most recent work of the same turn) is "
    "retained verbatim elsewhere. Summarize ONLY this prefix with "
    "sections:\n## Original Request\n## Early Progress\n"
    "## Context for Suffix"
)


def build_summary_request(
    serialized_conversation: str,
    *,
    previous_summary: str | None = None,
    instructions: str | None = None,
    turn_prefix: bool = False,
) -> tuple[str, str]:
    """(system_prompt, user_text) for one summarization call. `turn_prefix`
    selects the split-turn prefix prompt; `previous_summary` selects the
    update variant (chaining); `instructions` is the manual-compact
    "Additional focus" passthrough."""
    parts: list[str] = []
    if previous_summary is not None:
        parts.append(f"<previous-summary>\n{previous_summary}\n</previous-summary>")
    parts.append(f"<conversation>\n{serialized_conversation}\n</conversation>")
    if turn_prefix:
        parts.append(TURN_PREFIX_PROMPT)
    else:
        parts.append(_CHECKPOINT_FORMAT)
        if previous_summary is not None:
            parts.append(_UPDATE_VARIANT)
    if instructions:
        parts.append(f"Additional focus: {instructions}")
    return SUMMARIZER_SYSTEM_PROMPT, "\n\n".join(parts)


def merge_split_turn_summaries(history_summary: str, turn_prefix_summary: str) -> str:
    return f"{history_summary}\n\n---\n\n**Turn Context (split turn):**\n\n{turn_prefix_summary}"


def find_latest_compaction(messages: list[StoredMessage]) -> StoredMessage | None:
    for message in reversed(messages):
        if message.kind == "compaction":
            return message
    return None


def compacted_view(messages: list[StoredMessage]) -> tuple[str | None, list[StoredMessage]]:
    """The context-feed view of a history (FR-5.8 rebuild rule): find the
    LATEST compaction record; return its summary text plus the messages
    with `seq >= first_kept_seq` (compaction records themselves excluded
    from the retained list — the summary carries them). Without a record:
    `(None, messages)` — byte-identical to today's behavior."""
    record = find_latest_compaction(messages)
    if record is None:
        return None, messages
    first_kept_seq = int(record.content["first_kept_seq"])
    summary = str(record.content["summary"])
    retained = [m for m in messages if m.seq >= first_kept_seq and m.kind != "compaction"]
    return summary, retained


def summarizer_spend(messages: list[StoredMessage]) -> tuple[dict[str, int], float | None]:
    """Sum summarizer token usage and recorded cost across a session's
    `kind="compaction"` records (FR-5.9 rollup input). Cost is None only
    when NO record carries one -- a known-zero and an unknown must not
    look alike (same rule as turn costs)."""
    totals: dict[str, int] = {}
    cost: float | None = None
    for message in messages:
        if message.kind != "compaction":
            continue
        usage = message.content.get("summarizer_usage")
        if isinstance(usage, dict):
            for key, value in usage.items():
                if isinstance(key, str) and isinstance(value, int):
                    totals[key] = totals.get(key, 0) + value
        recorded = message.content.get("summarizer_cost_usd")
        if isinstance(recorded, int | float):
            cost = (cost or 0.0) + float(recorded)
    return totals, cost
