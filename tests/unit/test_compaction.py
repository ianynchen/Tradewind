"""Tests for the pure compaction core (`tradewind.domain.compaction`,
FR-5.8) and the cost math (`tradewind.domain.models.calculate_cost`,
FR-10.5). Pure functions, no I/O — the intent each test encodes is named
in its docstring or assertions (GUIDELINES §13.3).
"""

from __future__ import annotations

import pytest

from tradewind.domain.compaction import (
    build_summary_request,
    compacted_view,
    estimate_tokens,
    find_cut_point,
    find_latest_compaction,
    is_cut_point,
    merge_split_turn_summaries,
    serialize_for_summary,
    should_compact,
)
from tradewind.domain.models import (
    Kind,
    ModelCost,
    ModelCostTier,
    Role,
    StoredMessage,
    calculate_cost,
)


def _msg(
    seq: int,
    kind: Kind = "text",
    *,
    role: Role = "user",
    text: str = "x" * 40,
    turn_id: str | None = None,
    content: dict[str, object] | None = None,
) -> StoredMessage:
    return StoredMessage(
        role=role,
        kind=kind,
        content=content if content is not None else {"text": text},
        seq=seq,
        session_id="s1",
        turn_id=turn_id,
        created_at=f"2026-01-01T00:00:{seq:02d}",
    )


# --- calculate_cost (FR-10.5) ---


def test_base_rates_apply_per_million_tokens() -> None:
    cost = ModelCost(input=3.0, output=15.0, cache_read=0.3, cache_write=3.75)

    dollars = calculate_cost(
        cost,
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )

    assert dollars == pytest.approx(3.0 + 15.0 + 0.3 + 3.75)


def test_zero_usage_costs_zero() -> None:
    cost = ModelCost(input=3.0, output=15.0, cache_read=0.3, cache_write=3.75)

    assert calculate_cost(cost, input_tokens=0, output_tokens=0) == 0.0


def test_highest_matched_tier_applies_to_the_whole_request() -> None:
    # Pi's semantics: no marginal/blended pricing — the tier with the
    # highest input_tokens_above that total input exceeds prices EVERYTHING.
    cost = ModelCost(
        input=1.0,
        output=2.0,
        cache_read=0.1,
        cache_write=1.25,
        tiers=[
            ModelCostTier(
                input_tokens_above=100_000, input=2.0, output=4.0, cache_read=0.2, cache_write=2.5
            ),
            ModelCostTier(
                input_tokens_above=10_000, input=1.5, output=3.0, cache_read=0.15, cache_write=1.9
            ),
        ],
    )

    # 50k input: only the 10k tier matches.
    mid = calculate_cost(cost, input_tokens=50_000, output_tokens=0)
    # 200k input (cache counts toward tier selection): the 100k tier wins.
    high = calculate_cost(cost, input_tokens=150_000, output_tokens=0, cache_read_tokens=60_000)
    # 5k input: base rates.
    low = calculate_cost(cost, input_tokens=5_000, output_tokens=0)

    assert mid == pytest.approx(1.5 * 50_000 / 1e6)
    assert high == pytest.approx((2.0 * 150_000 + 0.2 * 60_000) / 1e6)
    assert low == pytest.approx(1.0 * 5_000 / 1e6)


# --- trigger predicate + estimation ---


def test_should_compact_trips_when_context_exceeds_window_minus_reserve() -> None:
    assert should_compact(90_001, context_window=100_000, reserve_tokens=10_000) is True
    assert should_compact(90_000, context_window=100_000, reserve_tokens=10_000) is False


def test_estimate_is_chars_over_four_and_never_zero() -> None:
    # The estimator is deliberately conservative (Pi: "overestimates");
    # a minimal message still counts at least one token.
    tiny = _msg(1, text="")
    assert estimate_tokens([tiny]) >= 1


# --- cut points (never-split rule) ---


def test_tool_result_is_never_a_cut_point() -> None:
    assert is_cut_point(_msg(1, "tool_result")) is False
    assert is_cut_point(_msg(1, "text")) is True
    assert is_cut_point(_msg(1, "tool_use")) is True


def test_cut_moves_past_tool_results_so_they_stay_with_their_call() -> None:
    # Budget lands on a tool_result: the cut must land at the NEXT valid
    # point after it — a tool_use is never orphaned from its result.
    messages = [
        _msg(1, text="a" * 400),
        _msg(2, "tool_use", role="assistant", content={"name": "t", "input": {}}),
        _msg(3, "tool_result", role="tool", content={"content": "r" * 400}),
        _msg(4, text="tail " * 20),
    ]

    cut = find_cut_point(messages, keep_recent_tokens=120)

    assert cut is not None
    # Budget (~120 tokens) is reached inside seq 3 (the tool_result);
    # the cut lands on seq 4, keeping call+result summarized together.
    assert messages[cut.first_kept_index].seq == 4


def test_nothing_to_compact_when_budget_never_reached() -> None:
    assert find_cut_point([_msg(1), _msg(2)], keep_recent_tokens=10_000) is None


def test_cut_at_index_zero_means_nothing_to_compact() -> None:
    messages = [_msg(1, text="z" * 4000), _msg(2, text="tail")]
    assert find_cut_point(messages, keep_recent_tokens=1500) is None


def test_split_turn_detected_exactly_via_turn_id() -> None:
    # The cut lands mid-turn (same turn_id as the previous message):
    # exact detection, no inference (tradewind's schema advantage over Pi).
    messages = [
        _msg(1, text="old " * 100, turn_id="t1"),
        _msg(2, text="turn2 prompt", turn_id="t2"),
        _msg(3, "tool_use", role="assistant", turn_id="t2", content={"name": "t", "input": {}}),
        _msg(4, "tool_result", role="tool", turn_id="t2", content={"content": "r" * 200}),
        _msg(5, text="turn2 reply " * 10, role="assistant", turn_id="t2"),
    ]

    cut = find_cut_point(messages, keep_recent_tokens=90)

    assert cut is not None
    assert cut.is_split_turn is True
    # The turn-prefix span starts at the turn's first message (seq 2).
    assert messages[cut.turn_start_index].seq == 2


# --- serialization for the summarizer ---


def test_serialization_is_plain_text_with_truncated_tool_results() -> None:
    messages = [
        _msg(1, text="do the thing"),
        _msg(2, "tool_use", role="assistant", content={"name": "probe", "input": {"q": "x"}}),
        _msg(3, "tool_result", role="tool", content={"content": "R" * 5000}),
        _msg(4, "thinking", role="assistant", content={"text": "hmm"}),
    ]

    text = serialize_for_summary(messages)

    assert "[User]: do the thing" in text
    assert '[Assistant tool calls]: probe({"q": "x"})' in text
    assert "R" * 2000 in text and "R" * 2001 not in text  # truncated at 2000
    assert "[Assistant thinking]: hmm" in text


def test_prior_compaction_records_are_never_serialized_as_conversation() -> None:
    # Chaining feeds the old summary through <previous-summary>, never as
    # conversation text the model might re-summarize.
    record = _msg(5, "compaction", content={"summary": "OLD SUMMARY", "first_kept_seq": 3})

    assert "OLD SUMMARY" not in serialize_for_summary([record, _msg(6, text="hi")])


def test_summary_request_variants() -> None:
    system, plain = build_summary_request("CONVO")
    _, chained = build_summary_request("CONVO", previous_summary="PREV")
    _, focused = build_summary_request("CONVO", instructions="the auth bug")
    _, prefix = build_summary_request("CONVO", turn_prefix=True)

    assert "Do NOT continue the conversation" in system
    assert "<conversation>\nCONVO\n</conversation>" in plain
    assert "## Goal" in plain
    assert "<previous-summary>\nPREV\n</previous-summary>" in chained
    assert "UPDATE it" in chained
    assert "Additional focus: the auth bug" in focused
    assert "## Original Request" in prefix and "## Goal" not in prefix


def test_split_turn_summaries_merge_with_marker() -> None:
    merged = merge_split_turn_summaries("HIST", "PREFIX")
    assert (
        merged.index("HIST")
        < merged.index("**Turn Context (split turn):**")
        < merged.index("PREFIX")
    )


# --- compacted_view (the FR-5.8 rebuild rule) ---


def test_view_without_records_is_identity() -> None:
    messages = [_msg(1), _msg(2)]
    assert compacted_view(messages) == (None, messages)


def test_view_emits_latest_summary_and_retained_tail_only() -> None:
    older = _msg(1, text="ancient")
    record_a = _msg(2, "compaction", content={"summary": "S1", "first_kept_seq": 1})
    kept = _msg(3, text="kept")
    record_b = _msg(4, "compaction", content={"summary": "S2", "first_kept_seq": 3})
    newest = _msg(5, text="new")

    summary, retained = compacted_view([older, record_a, kept, record_b, newest])

    assert summary == "S2"  # the LATEST record wins
    # Retained: seq >= 3, compaction records themselves excluded.
    assert [m.seq for m in retained] == [3, 5]
    assert find_latest_compaction([older, record_a]) is record_a
