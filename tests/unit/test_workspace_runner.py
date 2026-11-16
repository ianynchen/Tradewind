"""Workspace customizations through the real runner and `Session` API
(docs/components/06-workspace-safety.md §2, §3): the check before
`begin_turn`, the delete before `run()`, and the notice after
`TurnStarted`, on the fake governed backend with patterns set."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import aclosing
from pathlib import Path
from typing import Any

import anyio
import pytest
from fake_governed_backend import FakeGovernedBackend, Say
from pydantic import SecretStr

from tradewind import Tradewind
from tradewind.application import turn_runner, workspace_decision, workspace_scan
from tradewind.application.config import NativeStoreConfig, TradewindConfig
from tradewind.application.ports import TurnContext
from tradewind.application.workspace_scan import CustomizationPattern, CustomizationScan
from tradewind.domain.errors import ConfigError, Unsupported
from tradewind.domain.events import (
    Event,
    ItemCompleted,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from tradewind.domain.models import (
    ApiKeyAuth,
    Capabilities,
    CustomizationDecision,
    ModelSpec,
    Profile,
    SessionOptions,
    SessionRow,
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


class _NativeFake(FakeGovernedBackend):
    """Resumes natively and reads its transcript, so the runner reconciles
    a session that has a native id."""

    def capabilities(self) -> Capabilities:
        return (
            super()
            .capabilities()
            .model_copy(update={"supports_native_resume": True, "supports_transcript_read": True})
        )

    async def probe_native(self, session: SessionRow) -> bool:  # noqa: ARG002
        return True


class _FailsBeforeStart(FakeGovernedBackend):
    """Raises before `TurnStarted`, as Codex does for `max_tool_rounds`."""

    async def run(self, ctx: TurnContext) -> AsyncIterator[Event]:  # noqa: ARG002
        self.calls.append("run")
        raise Unsupported("fails before TurnStarted")
        yield  # an async generator, like every real backend


class _FailedBeforeStart(FakeGovernedBackend):
    """Ends with `TurnFailed` and never emits `TurnStarted`."""

    async def run(self, ctx: TurnContext) -> AsyncIterator[Event]:
        self.calls.append("run")
        yield TurnFailed(turn_id=ctx.turn_id, error="engine failed to start")


def _tradewind(
    tmp_path: Path,
    *,
    patterns: list[CustomizationPattern] | None = _PATTERNS,
    backup_dir: Path | None = None,
    backend_cls: type[FakeGovernedBackend] = FakeGovernedBackend,
    tapped: list[Event] | None = None,
) -> tuple[Tradewind, FakeGovernedBackend]:
    profile = _profile()
    tw = Tradewind(
        TradewindConfig(
            profiles={"default": profile},
            default_profile="default",
            store=tmp_path / "s.db",
            customization_backup_dir=backup_dir,
            on_event=None if tapped is None else tapped.append,
        )
    )
    fake = backend_cls(profile, NativeStoreConfig(), [Say("done")], patterns=patterns)
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
    tw, fake = _tradewind(tmp_path, backend_cls=_NativeFake)
    reconciled: list[str] = []

    async def spy_reconcile(*_args: object) -> int:
        reconciled.append("reconcile")
        return 0

    monkeypatch.setattr(tw._turn_runner._resume_planner, "reconcile", spy_reconcile)
    session = await tw.create(_ID, SessionOptions(cwd=repo))
    # A native id on a backend that reads transcripts: a turn reconciles.
    await anyio.to_thread.run_sync(tw._store.rehome_native, _ID, "codex", "native-1")
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
    # Control: the same session reconciles once the decision matches.
    proceed = CustomizationDecision(
        action="proceed", fingerprint=session.scan_customizations().fingerprint
    )
    await session.run("go", customizations=proceed)
    assert reconciled == ["reconcile"]


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


@pytest.mark.parametrize(
    ("where", "message"),
    [
        (None, r"needs TradewindConfig\.customization_backup_dir"),
        ("repo/backups", "inside the scanned area"),
    ],
)
async def test_bad_backup_dir_refused_before_begin_turn(
    tmp_path: Path, where: str | None, message: str
) -> None:
    repo = _repo(tmp_path / "repo")
    tw, fake = _tradewind(tmp_path, backup_dir=None if where is None else tmp_path / where)
    session = await tw.create(_ID, SessionOptions(cwd=repo))
    decision = CustomizationDecision(
        action="delete", fingerprint=session.scan_customizations().fingerprint
    )

    with pytest.raises(ConfigError, match=message):
        await session.run("go", customizations=decision)

    # A static config error: refused like the other rows, with no turn row.
    assert await _turn_rows(tw, _ID) == []
    assert await tw.history(_ID) == []
    assert fake.calls == []
    assert (repo / _SETTINGS).exists()


async def test_delete_failure_fails_the_turn_before_run(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    not_a_folder = tmp_path / "backup"
    not_a_folder.write_text("a file, so the backup folder cannot be made")
    tw, fake = _tradewind(tmp_path, backup_dir=not_a_folder)
    session = await tw.create(_ID, SessionOptions(cwd=repo))
    decision = CustomizationDecision(
        action="delete", fingerprint=session.scan_customizations().fingerprint
    )

    with pytest.raises(ConfigError, match="customization backup failed, nothing deleted"):
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


async def _history_notices(tw: Tradewind, session_id: str) -> list[dict[str, Any]]:
    return [
        m.content
        for m in await tw.history(session_id)
        if m.kind == "event" and m.content.get("type") == "workspace_customizations"
    ]


async def test_delete_notice_persisted_when_backend_fails_before_turn_started(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "repo")
    tw, fake = _tradewind(tmp_path, backup_dir=tmp_path / "backup", backend_cls=_FailsBeforeStart)
    session = await tw.create(_ID, SessionOptions(cwd=repo))
    decision = CustomizationDecision(
        action="delete", fingerprint=session.scan_customizations().fingerprint
    )

    with pytest.raises(Unsupported):
        await session.run("go", customizations=decision)

    assert fake.calls == ["prepare_turn", "run"]
    assert not (repo / _SETTINGS).exists()
    [notice] = await _history_notices(tw, _ID)
    assert notice["text"].startswith("workspace customizations deleted: .claude/settings.json;")


async def test_delete_notice_persisted_when_consumer_breaks_at_turn_started(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "repo")
    tw, _ = _tradewind(tmp_path, backup_dir=tmp_path / "backup")
    session = await tw.create(_ID, SessionOptions(cwd=repo))
    decision = CustomizationDecision(
        action="delete", fingerprint=session.scan_customizations().fingerprint
    )

    async with aclosing(session.stream("go", customizations=decision)) as events:
        async for event in events:
            if isinstance(event, TurnStarted):
                break

    [notice] = await _history_notices(tw, _ID)
    assert notice["text"].startswith("workspace customizations deleted: .claude/settings.json;")


async def test_stop_during_delete_ends_the_turn_without_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path / "repo")
    tw, fake = _tradewind(tmp_path, backup_dir=tmp_path / "backup")
    session = await tw.create(_ID, SessionOptions(cwd=repo))
    decision = CustomizationDecision(
        action="delete", fingerprint=session.scan_customizations().fingerprint
    )

    def delete_then_stop(scan: CustomizationScan, **kwargs: Any) -> Any:
        result = workspace_decision.apply_delete(scan, **kwargs)
        anyio.from_thread.run(session.stop)  # the delete runs in a worker thread
        return result

    monkeypatch.setattr(turn_runner, "apply_delete", delete_then_stop)

    events = [e async for e in session.stream("go", customizations=decision)]

    assert fake.calls == ["prepare_turn"]
    assert isinstance(events[-1], TurnCompleted)
    assert events[-1].result.status == "interrupted"
    assert not (repo / _SETTINGS).exists()
    assert len(await _history_notices(tw, _ID)) == 1
    # No `TurnStarted`: the notice still reaches the stream, once, before
    # the synthesized terminal.
    assert len(_customization_notices(events)) == 1
    assert _customization_notices(events[-2:-1]) == _customization_notices(events)


async def test_no_patterns_and_deleted_process_cwd_still_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tw, fake = _tradewind(tmp_path, patterns=None)
    session = await tw.create(_ID, SessionOptions())
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()

    scan = session.scan_customizations()
    result = await session.run("go")

    assert scan.files == ()
    assert scan.fingerprint == ""
    assert result.status == "completed"
    assert fake.calls == ["prepare_turn", "run"]


async def test_delete_notice_lists_only_the_deleted_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path / "repo")
    (repo / ".mcp.json").write_text("{}")
    patterns = [*_PATTERNS, CustomizationPattern(".mcp.json", "mcp", is_dir=False)]
    tw, _ = _tradewind(tmp_path, patterns=patterns, backup_dir=tmp_path / "backup")
    session = await tw.create(_ID, SessionOptions(cwd=repo))
    decision = CustomizationDecision(
        action="delete", fingerprint=session.scan_customizations().fingerprint
    )

    def gone_before_delete(scan: CustomizationScan, **kwargs: Any) -> Any:
        os.remove(repo / ".mcp.json")  # removed after the check, before the delete
        return workspace_decision.apply_delete(scan, **kwargs)

    monkeypatch.setattr(turn_runner, "apply_delete", gone_before_delete)

    events = [e async for e in session.stream("go", customizations=decision)]

    [backup] = list((tmp_path / "backup").iterdir())
    [notice] = _customization_notices(events)
    assert notice["text"] == (
        f"workspace customizations deleted: .claude/settings.json; backup at {backup}"
    )


async def test_notice_yielded_and_tapped_once_when_backend_fails_before_turn_started(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "repo")
    tapped: list[Event] = []
    tw, fake = _tradewind(
        tmp_path, backup_dir=tmp_path / "backup", backend_cls=_FailedBeforeStart, tapped=tapped
    )
    session = await tw.create(_ID, SessionOptions(cwd=repo))
    decision = CustomizationDecision(
        action="delete", fingerprint=session.scan_customizations().fingerprint
    )

    events = [e async for e in session.stream("go", customizations=decision)]

    assert fake.calls == ["prepare_turn", "run"]
    assert not any(isinstance(e, TurnStarted) for e in events)
    assert [type(e).__name__ for e in events] == ["ItemCompleted", "TurnFailed"]
    [notice] = _customization_notices(events)
    assert notice["text"].startswith("workspace customizations deleted: .claude/settings.json;")
    assert _customization_notices(tapped) == [notice]
    assert [type(e).__name__ for e in tapped] == ["ItemCompleted", "TurnFailed"]
    # Stored once only.
    assert await _history_notices(tw, _ID) == [notice]


@pytest.mark.parametrize("action", ["proceed", "delete"])
async def test_notice_yielded_and_tapped_once_on_a_normal_turn(tmp_path: Path, action: str) -> None:
    repo = _repo(tmp_path / "repo")
    tapped: list[Event] = []
    tw, _ = _tradewind(tmp_path, backup_dir=tmp_path / "backup", tapped=tapped)
    session = await tw.create(_ID, SessionOptions(cwd=repo))
    decision = CustomizationDecision(
        action=action, fingerprint=session.scan_customizations().fingerprint
    )

    events = [e async for e in session.stream("go", customizations=decision)]

    assert isinstance(events[-1], TurnCompleted)
    [notice] = _customization_notices(events)
    assert _customization_notices(tapped) == [notice]
    assert isinstance(events[0], TurnStarted)
    assert _customization_notices(events[1:2]) == [notice]
    assert await _history_notices(tw, _ID) == [notice]
