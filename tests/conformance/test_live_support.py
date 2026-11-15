"""Hermetic tests (run by `scripts/check.sh`) for the live-test helpers in
`tests/conformance/live_support.py`: a live turn that does not complete must
fail with the turn's own error -- never a bare StopIteration -- and that
error must never leak an API key into test output."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from tests.conformance.live_support import completed_result, redact_api_keys
from tradewind.domain.events import TurnCompleted, TurnFailed, TurnStarted
from tradewind.domain.models import TurnResult

# pytester runs a nested pytest session to inspect the REAL failure output
# (the leak this guards against lives in pytest's traceback rendering, not
# in the exception message). The repo has no conftest.py and no other
# plugin setup, so enabling it for this module alone conflicts with nothing.
pytest_plugins = ["pytester"]

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

    with pytest.raises(pytest.fail.Exception) as raised:
        completed_result(events)

    message = str(raised.value)
    assert "authentication_error: invalid key" in message
    assert "AbC_dEf" not in message


def test_a_turn_with_no_terminal_event_fails_with_a_clear_message() -> None:
    with pytest.raises(pytest.fail.Exception, match="neither TurnCompleted nor TurnFailed"):
        completed_result([TurnStarted(turn_id="t")])


_NESTED_TEST = """
import asyncio

from tests.conformance.live_support import collect_events, completed_result
from tradewind.domain.events import TurnFailed, TurnStarted


async def _failing_turn():
    # Like a real provider error: the key exists only while the stream runs.
    key = "-".join(["sk", "ant", "api03", "Q" * 6 + "zz" * 6])
    yield TurnStarted(turn_id="t")
    yield TurnFailed(turn_id="t", error="401 bad key " + key)


def test_live_turn():
    events = asyncio.run(collect_events(_failing_turn()))
    completed_result(events)
"""


@pytest.mark.parametrize("flags", [[], ["--showlocals"], ["--showlocals", "--full-trace"]])
def test_failure_output_never_contains_the_key(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, flags: list[str]
) -> None:
    """pytest's own failure rendering (the traceback's argument and local
    values, more with `--showlocals`/`--full-trace`) must not print the
    unredacted error. The nested test collects like the live tests do
    (`collect_events`); the key is built at runtime from parts, so it never
    appears literally in any source this run shows."""
    key = "-".join(["sk", "ant", "api03", "Q" * 6 + "zz" * 6])
    pytester.makepyfile(test_nested=_NESTED_TEST)
    # The nested pytest runs in a temp dir: give it this repo's `tests` package.
    repo_root = str(Path(__file__).resolve().parents[2])
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join(filter(None, [repo_root, os.environ.get("PYTHONPATH")]))
    )

    run = pytester.runpytest_subprocess(*flags, "-p", "no:cacheprovider")

    run.assert_outcomes(failed=1)
    output = run.stdout.str() + run.stderr.str()
    assert "401 bad key sk-ant-[REDACTED]" in output  # the reason IS shown
    assert key not in output
    assert "QQQQQQzz" not in output


_NESTED_PLAIN_COLLECTION = """
import asyncio

from tests.conformance.live_support import completed_result
from tradewind.domain.events import TurnFailed, TurnStarted


async def _failing_turn():
    key = "-".join(["sk", "ant", "api03", "Q" * 6 + "zz" * 6])
    yield TurnStarted(turn_id="t")
    yield TurnFailed(turn_id="t", error="401 bad key " + key)


async def _plain(stream):
    return [event async for event in stream]


def test_live_turn():
    events = asyncio.run(_plain(_failing_turn()))  # NOT redacted on the way in
    completed_result(events)
"""


@pytest.mark.parametrize("flags", [[], ["--showlocals"]])
def test_completed_result_itself_never_prints_its_events(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, flags: list[str]
) -> None:
    """Defence in depth for a caller that did NOT collect through
    `collect_events`: `completed_result` fails without any traceback, so
    neither its `events` argument nor the caller's locals are printed
    (default output and `--showlocals`; `--full-trace` overrides this, which
    is what `collect_events` covers)."""
    key = "-".join(["sk", "ant", "api03", "Q" * 6 + "zz" * 6])
    pytester.makepyfile(test_nested=_NESTED_PLAIN_COLLECTION)
    repo_root = str(Path(__file__).resolve().parents[2])
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join(filter(None, [repo_root, os.environ.get("PYTHONPATH")]))
    )

    run = pytester.runpytest_subprocess(*flags, "-p", "no:cacheprovider")

    run.assert_outcomes(failed=1)
    output = run.stdout.str() + run.stderr.str()
    assert "401 bad key sk-ant-[REDACTED]" in output
    assert key not in output


async def test_collect_events_redacts_failures_as_they_arrive() -> None:
    from tests.conformance.live_support import collect_events

    async def stream() -> Any:
        yield TurnStarted(turn_id="t")
        yield TurnFailed(turn_id="t", error=f"bad {_FAKE_KEY}")

    events = await collect_events(stream())

    failed = events[-1]
    assert isinstance(failed, TurnFailed)
    assert failed.error == "bad sk-ant-[REDACTED]"
