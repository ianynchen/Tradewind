"""Phase-2c live validation (roadmap item E): a genuinely long session
against the real Anthropic API, driven through the full public stack, that
must compact AT LEAST TWICE (exercising `<previous-summary>` chaining) and
still answer a question whose answer exists only inside the checkpoint --
the first time a real model produces one of our compaction summaries.

The cheap-window trick: `ModelMeta.context_window` is TRADEWIND's
metadata, so declaring ~3k tokens on haiku makes the trigger fire after a
few small turns while the API's real window is never at risk. A full run
costs cents. Gated like the other live tests (real, billed API calls);
run via `bash scripts/live-tests.sh tests/integration/test_compaction_live.py -v -s`.
"""

from __future__ import annotations

import os
import uuid

import pytest

from tradewind import Tradewind, TradewindConfig, TurnDefaults
from tradewind.application.config import CompactionSettings
from tradewind.domain.models import (
    ApiKeyAuth,
    ModelCost,
    ModelMeta,
    ModelSpec,
    Profile,
    SessionOptions,
)

pytestmark = pytest.mark.integration

_FACTS = {
    "falcon": "BLUEBERRY",
    "glacier": "TANGOSEVEN",
    "harbor": "MOSSPETAL",
    "lantern": "QUARTZFIN",
    "meadow": "DRIFTBOLT",
    "orchid": "IRONWAKE",
    "quarry": "VELVETMAST",
    "sundial": "PINEHATCH",
    "tundra": "COPPERWREN",
    "willow": "FATHOMKEY",
}

_FILLER = "Background context, acknowledge but do not repeat: " + (
    "the survey vessel logged another uneventful transit across the sound. " * 25
)


@pytest.mark.skipif(
    "ANTHROPIC_API_KEY" not in os.environ,
    reason="requires a real ANTHROPIC_API_KEY (billed live run)",
)
async def test_long_session_compacts_twice_and_memory_survives() -> None:
    meta = ModelMeta(
        context_window=3000,  # the cheap-window trick (module docstring)
        max_tokens=1500,
        # Real haiku pricing ($/Mtok) so cost assertions exercise the
        # FR-10.5 math against live usage numbers.
        cost=ModelCost(input=1.0, output=5.0, cache_read=0.1, cache_write=1.25),
    )
    profile = Profile(
        backend="langchain",
        auth=ApiKeyAuth(api_key=os.environ["ANTHROPIC_API_KEY"]),
        models={"standard": ModelSpec(model="claude-haiku-4-5", meta=meta)},
    )
    config = TradewindConfig(
        profiles={"default": profile},
        default_profile="default",
        defaults=TurnDefaults(
            compaction=CompactionSettings(auto=True, reserve_tokens=700, keep_recent_tokens=400)
        ),
    )
    tw = Tradewind(config)
    session = await tw.create(
        str(uuid.uuid4()),
        SessionOptions(
            system_prompt=(
                "You are extremely terse. Codewords given to you are critical: "
                "remember every codeword EXACTLY."
            )
        ),
    )

    for index, (name, codeword) in enumerate(_FACTS.items(), 1):
        result = await session.run(
            f"Fact #{index}: the codeword for {name} is {codeword}. "
            f"Reply with OK-{index} and nothing else.\n\n{_FILLER}"
        )
        assert result.status == "completed", result
        print(
            f"turn {index}: end_reason={result.end_reason} "
            f"usage={result.usage.get('total_tokens')} cost={result.cost_usd}"
        )

    records = [m for m in await tw.history(session.id) if m.kind == "compaction"]
    print(f"compaction records: {len(records)}")
    for record in records:
        summary = str(record.content["summary"])
        print(
            f"  record seq={record.seq} first_kept_seq={record.content['first_kept_seq']} "
            f"tokens_before={record.content['tokens_before']} "
            f"summarizer_cost={record.content['summarizer_cost_usd']} "
            f"summary_chars={len(summary)}"
        )
        # The structured-checkpoint format actually came out of a real model.
        assert "## " in summary, summary[:400]

    # At least two records: chaining (<previous-summary> update prompt) ran.
    assert len(records) >= 2

    # THE memory test: fact #1 was summarized away turns ago -- the only
    # place BLUEBERRY can live is the checkpoint chain.
    probe = await session.run("What is the codeword for falcon? Reply with the codeword only.")
    print(f"probe answer: {probe.final_text!r}")
    assert probe.final_text is not None
    assert "BLUEBERRY" in probe.final_text.upper()

    rollup = await tw.usage(session.id)
    print(
        f"rollup: turns={rollup.turns} tokens={rollup.usage.get('total_tokens')} "
        f"cost={rollup.cost_usd} summarizer_tokens={rollup.summarizer_usage.get('total_tokens')} "
        f"summarizer_cost={rollup.summarizer_cost_usd}"
    )
    assert rollup.turns == len(_FACTS) + 1
    assert rollup.usage.get("total_tokens", 0) > 0
    assert rollup.cost_usd is not None and rollup.cost_usd > 0
    assert rollup.summarizer_cost_usd is not None and rollup.summarizer_cost_usd > 0
