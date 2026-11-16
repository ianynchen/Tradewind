"""Workspace customizations through the real runner and `Session` API
(docs/components/06-workspace-safety.md §2, §3): the check before
`begin_turn`, the delete before `run()`, and the notice after
`TurnStarted`, on the fake governed backend with patterns set."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import anyio
import pytest
from fake_governed_backend import FakeGovernedBackend, Say
from pydantic import SecretStr

from tradewind import Tradewind
from tradewind.application import workspace_scan
from tradewind.application.config import NativeStoreConfig, TradewindConfig
from tradewind.application.workspace_scan import CustomizationPattern
from tradewind.domain.errors import ConfigError
from tradewind.domain.events import Event, ItemCompleted, TurnCompleted, TurnStarted
from tradewind.domain.models import (
    ApiKeyAuth,
    CustomizationDecision,
    ModelSpec,
    Profile,
    SessionOptions,
)

_ID = "66666666-6666-6666-6666-666666666666"
_SETTINGS = ".claude/settings.json"
_PATTERNS = [CustomizationPattern(_SETTINGS, "settings", is_dir=False)]


def _profile() -> Profile:
    return Profile(
        backend="codex",
        auth=ApiKeyAuth(api_key=SecretStr("sk-test")),
        models={"standard": ModelSpec(model="model-standard")},
    )


def _repo(path: Path, *, settings: bool = True) -> Path:
    """A repository folder; with `settings`, it holds a flagged file."""
    (path / ".git").mkdir(parents=True)
    if settings:
        (path / ".claude").mkdir()
        (path / _SETTINGS).write_text('{"hooks": {}}')
    return path


def _tradewind(
    tmp_path: Path,
    *,
    patterns: list[CustomizationPattern] | None = _PATTERNS,
    backup_dir: Path | None = None,
) -> tuple[Tradewind, FakeGovernedBackend]:
    profile = _profile()
    tw = Tradewind(
        TradewindConfig(
            profiles={"default": profile},
            default_profile="default",
            store=tmp_path / "s.db",
            customization_backup_dir=backup_dir,
        )
    )
    fake = FakeGovernedBackend(profile, NativeStoreConfig(), [Say("done")], patterns=patterns)
    tw._backends["default"] = fake
    return tw, fake


def _customization_notices(events: list[Event]) -> list[dict[str, Any]]:
    return [
        e.message.content
        for e in events
        if isinstance(e, ItemCompleted)
        and e.message.content.get("type") == "workspace_customizations"
    ]


def _shape(events: list[Event]) -> list[tuple[str, object]]:
    """Event types plus item content: what a plain turn must keep."""
    return [
        (type(e).__name__, e.message.content if isinstance(e, ItemCompleted) else None)
        for e in events
    ]


async def _turn_rows(tw: Tradewind, session_id: str) -> list[Any]:
    return await anyio.to_thread.run_sync(tw._store.turn_usages, session_id)


@pytest.mark.parametrize(
    ("decision", "message"),
    [
        (None, "workspace customizations found: .claude/settings.json; scan and pass a decision"),
        (CustomizationDecision(action="proceed", fingerprint="stale"), "changed since the scan"),
        ("stop", "stopped by the caller: .claude/settings.json"),
    ],
)
async def test_refusal_leaves_no_turn_row_and_no_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decision: object, message: str
) -> None:
    repo = _repo(tmp_path / "repo")
    tw, fake = _tradewind(tmp_path)
    reconciled: list[str] = []

    async def spy_reconcile(*_args: object) -> int:
        reconciled.append("reconcile")
        return 0

    monkeypatch.setattr(tw._turn_runner._resume_planner, "reconcile", spy_reconcile)
    session = await tw.create(_ID, SessionOptions(cwd=repo))
    if decision == "stop":
        decision = CustomizationDecision(
            action="stop", fingerprint=session.scan_customizations().fingerprint
        )
    assert decision is None or isinstance(decision, CustomizationDecision)

    with pytest.raises(ConfigError, match=message):
        await session.run("go", customizations=decision)

    assert await _turn_rows(tw, _ID) == []
    assert await tw.history(_ID) == []
    assert fake.calls == []
    assert reconciled == []
    assert (repo / _SETTINGS).exists()


@pytest.mark.parametrize("action", ["proceed", "delete"])
async def test_matching_decision_runs(tmp_path: Path, action: str) -> None:
    repo = _repo(tmp_path / "repo")
    backup_dir = tmp_path / "backup"
    tw, fake = _tradewind(tmp_path, backup_dir=backup_dir)
    session = await tw.create(_ID, SessionOptions(cwd=repo))
    scan = session.scan_customizations()
    assert [str(f.path) for f in scan.files] == [_SETTINGS]

    events = [
        e
        async for e in session.stream(
            "go",
            customizations=CustomizationDecision(action=action, fingerprint=scan.fingerprint),
        )
    ]

    assert fake.calls == ["prepare_turn", "run"]
    assert isinstance(events[-1], TurnCompleted)
    assert events[-1].result.status == "completed"
    [notice] = _customization_notices(events)
    if action == "proceed":
        assert (repo / _SETTINGS).exists()
        assert notice["text"] == (
            "workspace customizations loaded without broker control: .claude/settings.json"
        )
    else:
        assert not (repo / _SETTINGS).exists()
        [backup] = list(backup_dir.iterdir())
        assert backup.name.endswith(f"-{events[-1].result.turn_id}")
        assert (backup / _SETTINGS).read_text() == '{"hooks": {}}'
        assert notice["text"] == (
            f"workspace customizations deleted: .claude/settings.json; backup at {backup}"
        )
    # Persisted like the other notices, in this turn.
    stored = [m for m in await tw.history(_ID) if m.kind == "event"]
    assert [m.content for m in stored] == [notice]
    assert stored[0].turn_id == events[-1].result.turn_id


async def test_files_gone_by_turn_start_runs_normally(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    tw, fake = _tradewind(tmp_path, backup_dir=tmp_path / "backup")
    session = await tw.create(_ID, SessionOptions(cwd=repo))
    scan = session.scan_customizations()
    (repo / _SETTINGS).unlink()

    events = [
        e
        async for e in session.stream(
            "go",
            customizations=CustomizationDecision(action="delete", fingerprint=scan.fingerprint),
        )
    ]

    assert fake.calls == ["prepare_turn", "run"]
    assert isinstance(events[-1], TurnCompleted)
    assert _customization_notices(events) == []
    assert not (tmp_path / "backup").exists()


async def test_cwd_unset_and_process_cwd_changed_raises_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _repo(tmp_path / "first")
    second = _repo(tmp_path / "second")
    tw, fake = _tradewind(tmp_path)
    session = await tw.create(_ID, SessionOptions())
    monkeypatch.chdir(first)
    scan = session.scan_customizations()
    assert scan.root == first.resolve()
    monkeypatch.chdir(second)

    with pytest.raises(ConfigError, match=r"^customizations changed since the scan$"):
        await session.run(
            "go",
            customizations=CustomizationDecision(action="proceed", fingerprint=scan.fingerprint),
        )

    assert await _turn_rows(tw, _ID) == []
    assert fake.calls == []


async def test_stream_delete_notice_right_after_turn_started(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo = _repo(tmp_path / "repo")
    backup_dir = tmp_path / "backup"
    tw, _ = _tradewind(tmp_path, backup_dir=backup_dir)
    session = await tw.create(_ID, SessionOptions(cwd=repo))
    decision = CustomizationDecision(
        action="delete", fingerprint=session.scan_customizations().fingerprint
    )

    with caplog.at_level(logging.WARNING, logger="tradewind.application.turn_runner"):
        events = [e async for e in session.stream("go", customizations=decision)]

    [backup] = list(backup_dir.iterdir())
    text = f"workspace customizations deleted: .claude/settings.json; backup at {backup}"
    assert isinstance(events[0], TurnStarted)
    assert isinstance(events[1], ItemCompleted)
    assert events[1].message.kind == "event"
    assert events[1].message.content == {"type": "workspace_customizations", "text": text}
    # The first engine event comes after the notice.
    assert isinstance(events[2], ItemCompleted)
    assert events[2].message.content == {"text": "done"}
    assert text in [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


async def test_proceed_logs_a_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    repo = _repo(tmp_path / "repo")
    tw, _ = _tradewind(tmp_path)
    session = await tw.create(_ID, SessionOptions(cwd=repo))
    decision = CustomizationDecision(
        action="proceed", fingerprint=session.scan_customizations().fingerprint
    )

    with caplog.at_level(logging.WARNING, logger="tradewind.application.turn_runner"):
        await session.run("go", customizations=decision)

    assert "workspace customizations loaded without broker control: .claude/settings.json" in [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    ]


async def test_delete_failure_fails_the_turn_before_run(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    tw, fake = _tradewind(tmp_path, backup_dir=None)
    session = await tw.create(_ID, SessionOptions(cwd=repo))
    decision = CustomizationDecision(
        action="delete", fingerprint=session.scan_customizations().fingerprint
    )

    with pytest.raises(ConfigError, match="customization_backup_dir"):
        await session.run("go", customizations=decision)

    # As a `prepare_turn` ConfigError ends: the turn is finalized as failed.
    [row] = await _turn_rows(tw, _ID)
    assert row.status == "failed"
    assert fake.calls == ["prepare_turn"]
    assert (repo / _SETTINGS).exists()


async def test_plain_turn_without_files_emits_same_events(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", settings=False)
    (tmp_path / "plain").mkdir()
    (tmp_path / "scanned").mkdir()
    tw_plain, _ = _tradewind(tmp_path / "plain", patterns=None)
    tw_scanned, _ = _tradewind(tmp_path / "scanned")
    plain = await tw_plain.create(_ID, SessionOptions(cwd=repo))
    scanned = await tw_scanned.create(_ID, SessionOptions(cwd=repo))

    plain_events = [e async for e in plain.stream("go")]
    scanned_events = [e async for e in scanned.stream("go")]
    with_decision = [
        e
        async for e in scanned.stream(
            "go", customizations=CustomizationDecision(action="delete", fingerprint="")
        )
    ]

    assert scanned.scan_customizations().fingerprint == ""
    assert _shape(scanned_events) == _shape(plain_events)
    assert _shape(with_decision) == _shape(plain_events)


async def test_decision_not_in_snapshot(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    tw, _ = _tradewind(tmp_path)
    session = await tw.create(_ID, SessionOptions(cwd=repo))
    fingerprint = session.scan_customizations().fingerprint

    await session.run(
        "go", customizations=CustomizationDecision(action="proceed", fingerprint=fingerprint)
    )

    row = await anyio.to_thread.run_sync(tw._store.get_session, _ID)
    assert row is not None
    assert "customizations" not in row.options_snapshot
    assert fingerprint not in json.dumps(row.options_snapshot)
    # Nowhere in the store at all (D4).
    with sqlite3.connect(tmp_path / "s.db") as db:
        dump = "\n".join(db.iterdump())
    assert fingerprint not in dump


async def test_spawn_passes_customizations_to_child_first_turn(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    tw, _ = _tradewind(tmp_path)
    parent = await tw.create(_ID, SessionOptions(cwd=repo))
    decision = CustomizationDecision(
        action="proceed", fingerprint=parent.scan_customizations().fingerprint
    )
    await parent.run("go", customizations=decision)

    # The child never takes the parent's decision (D9).
    with pytest.raises(ConfigError, match="workspace customizations found"):
        await parent.spawn("child")

    child = await parent.spawn("child", customizations=decision)

    child_events = [m for m in await tw.history(child.id) if m.kind == "event"]
    assert [m.content["type"] for m in child_events] == ["workspace_customizations"]
    [row] = await _turn_rows(tw, child.id)
    assert row.status == "completed"


async def test_backend_without_patterns_never_scans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path / "repo")
    tw, fake = _tradewind(tmp_path, patterns=None)

    def no_scan(_folder: Path) -> Path | None:
        raise AssertionError("scanned with no patterns")

    monkeypatch.setattr(workspace_scan, "find_root", no_scan)
    session = await tw.create(_ID, SessionOptions(cwd=repo))

    scan = session.scan_customizations()
    events = [e async for e in session.stream("go")]

    assert scan.files == ()
    assert scan.fingerprint == ""
    assert fake.calls == ["prepare_turn", "run"]
    assert isinstance(events[-1], TurnCompleted)
    assert _customization_notices(events) == []
