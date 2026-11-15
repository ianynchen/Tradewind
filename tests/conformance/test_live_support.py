"""Hermetic tests (run by `scripts/check.sh`) for the live-test helpers in
`tests/conformance/live_support.py`: a live turn that does not complete must
fail with the turn's own error -- never a bare StopIteration -- and that
error must never leak an API key into test output."""

from __future__ import annotations

import pytest

from tests.conformance.live_support import completed_result, redact_api_keys
from tradewind.domain.events import TurnCompleted, TurnFailed, TurnStarted
from tradewind.domain.models import TurnResult

_FAKE_KEY = "sk-ant-api03-AbC_dEf-123xyz"


def test_redaction_removes_every_anthropic_key_shaped_substring() -> None:
    text = f"401 invalid x-api-key {_FAKE_KEY}; retry with {_FAKE_KEY}."

    redacted = redact_api_keys(text)

    assert "AbC_dEf" not in redacted
    assert "sk-ant-api03" not in redacted
    assert redacted == "401 invalid x-api-key sk-ant-[REDACTED]; retry with sk-ant-[REDACTED]."


def test_redaction_leaves_key_free_text_unchanged() -> None:
    assert redact_api_keys("rate limited (429)") == "rate limited (429)"


def test_completed_result_returns_the_turn_result() -> None:
    result = TurnResult(
        turn_id="t",
        status="completed",
        end_reason="end_turn",
        final_text="hi",
        usage={},
        cost_usd=None,
    )

    assert completed_result([TurnStarted(turn_id="t"), TurnCompleted(result=result)]) is result


def test_a_failed_turn_fails_with_its_redacted_error() -> None:
    events = [
        TurnStarted(turn_id="t"),
        TurnFailed(turn_id="t", error=f"authentication_error: invalid key {_FAKE_KEY}"),
    ]

    with pytest.raises(AssertionError) as raised:
        completed_result(events)

    message = str(raised.value)
    assert "authentication_error: invalid key" in message
    assert "AbC_dEf" not in message


def test_a_turn_with_no_terminal_event_fails_with_a_clear_message() -> None:
    with pytest.raises(AssertionError, match="neither TurnCompleted nor TurnFailed"):
        completed_result([TurnStarted(turn_id="t")])
