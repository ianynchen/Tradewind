"""Tests for the customization decision table and delete
(`application/workspace_decision.py`, docs/components/06-workspace-safety.md §3)."""

from __future__ import annotations

import dataclasses
import os
import stat
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from pydantic import ValidationError

from tradewind.application.workspace_decision import (
    DecisionOutcome,
    apply_delete,
    check_backup_dir,
    resolve,
)
from tradewind.application.workspace_scan import CustomizationScan, FoundFile
from tradewind.domain.errors import ConfigError
from tradewind.domain.models import CustomizationDecision

NOW = datetime(2026, 11, 15, 9, 8, 7, tzinfo=UTC)
FOLDER = "20261115T090807Z-turn-1"


def _make_scan(root: Path, *names: str) -> CustomizationScan:
    files = []
    for name in names:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() and not target.is_symlink():
            target.write_text(f"content of {name}")
        files.append(FoundFile(path=Path(name), sha256="h", kind="k"))
    return CustomizationScan(root=root, files=tuple(files), fingerprint="fp1" if files else "")


def _decision(action: str, fingerprint: str = "fp1") -> CustomizationDecision:
    return CustomizationDecision(action=cast(Any, action), fingerprint=fingerprint)


FILES = ".claude/settings.json, .mcp.json"


@pytest.mark.parametrize(
    ("case", "decision", "run", "notice", "delete_n", "error"),
    [
        ("no files, no decision", None, True, None, 0, None),
        ("no files, any decision", _decision("stop", "zzz"), True, None, 0, None),
        (
            "files, no decision",
            None,
            False,
            None,
            0,
            f"workspace customizations found: {FILES}; scan and pass a decision",
        ),
        (
            "files, fingerprint differs",
            _decision("proceed", "other"),
            False,
            None,
            0,
            "customizations changed since the scan",
        ),
        ("files, stop", _decision("stop"), False, None, 0, f"stopped by the caller: {FILES}"),
        ("files, delete", _decision("delete"), True, None, 2, None),
        (
            "files, proceed",
            _decision("proceed"),
            True,
            f"workspace customizations loaded without broker control: {FILES}",
            0,
            None,
        ),
    ],
)
def test_decision_table(
    tmp_path: Path,
    case: str,
    decision: CustomizationDecision | None,
    run: bool,
    notice: str | None,
    delete_n: int,
    error: str | None,
) -> None:
    scan = (
        _make_scan(tmp_path)
        if case.startswith("no files")
        else _make_scan(tmp_path, ".claude/settings.json", ".mcp.json")
    )
    if error is not None:
        with pytest.raises(ConfigError) as exc:
            resolve(scan, decision)
        assert str(exc.value) == error
        return
    outcome = resolve(scan, decision)
    assert outcome.run is run
    assert outcome.notice == notice
    assert len(outcome.delete) == delete_n
    assert all(p.is_absolute() for p in outcome.delete)
    if delete_n:
        assert outcome.delete == (tmp_path / ".claude/settings.json", tmp_path / ".mcp.json")
        assert outcome.files_text == FILES


def test_delete_copies_then_deletes_and_returns_backup(tmp_path: Path) -> None:
    root, backups = tmp_path / "repo", tmp_path / "backups"
    root.mkdir()
    scan = _make_scan(root, ".claude/settings.json", ".claude/hooks/x.sh")
    backup, deleted = apply_delete(scan, backup_dir=backups, turn_id="turn-1", now=NOW)
    assert backup == backups / FOLDER
    assert deleted == (Path(".claude/settings.json"), Path(".claude/hooks/x.sh"))
    assert (backup / ".claude/settings.json").read_text() == "content of .claude/settings.json"
    assert (backup / ".claude/hooks/x.sh").read_text() == "content of .claude/hooks/x.sh"
    assert not (root / ".claude/settings.json").exists()
    assert not (root / ".claude/hooks/x.sh").exists()


def test_backup_folder_name_uses_utc(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    scan = _make_scan(root, ".mcp.json")
    local = NOW.astimezone(timezone(timedelta(hours=5)))
    backup, _ = apply_delete(scan, backup_dir=tmp_path / "b", turn_id="t", now=local)
    assert backup.name == "20261115T090807Z-t"


def test_delete_copy_failure_deletes_nothing_and_keeps_backup(tmp_path: Path) -> None:
    root, backups = tmp_path / "repo", tmp_path / "backups"
    root.mkdir()
    scan = _make_scan(root, "a.json", "b.json")
    os.chmod(root / "b.json", 0)  # copy of the second file fails
    try:
        with pytest.raises(ConfigError):
            apply_delete(scan, backup_dir=backups, turn_id="turn-1", now=NOW)
    finally:
        os.chmod(root / "b.json", 0o600)
    assert (root / "a.json").exists()
    assert (root / "b.json").exists()
    assert (backups / FOLDER / "a.json").exists()


def test_delete_skips_gone_file(tmp_path: Path) -> None:
    root, backups = tmp_path / "repo", tmp_path / "backups"
    root.mkdir()
    scan = _make_scan(root, "a.json", "b.json")
    (root / "a.json").unlink()
    backup, deleted = apply_delete(scan, backup_dir=backups, turn_id="turn-1", now=NOW)
    assert deleted == (Path("b.json"),)
    assert not (backup / "a.json").exists()
    assert (backup / "b.json").exists()
    assert not (root / "b.json").exists()


def test_delete_symlink_backs_up_target_removes_link_only(tmp_path: Path) -> None:
    root, backups, outside = tmp_path / "repo", tmp_path / "backups", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = outside / "real.json"
    target.write_text("target content")
    (root / ".mcp.json").symlink_to(target)
    scan = _make_scan(root, ".mcp.json")
    backup, _ = apply_delete(scan, backup_dir=backups, turn_id="turn-1", now=NOW)
    copy = backup / ".mcp.json"
    assert copy.read_text() == "target content"
    assert not copy.is_symlink()
    assert not (root / ".mcp.json").is_symlink()
    assert not (root / ".mcp.json").exists()
    assert target.read_text() == "target content"


def test_backup_dir_missing_or_inside_area_raises(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    scan = _make_scan(root, ".mcp.json")
    with pytest.raises(ConfigError):
        apply_delete(scan, backup_dir=None, turn_id="t", now=NOW)
    for inside in (root, root / "sub" / "backups"):
        with pytest.raises(ConfigError):
            apply_delete(scan, backup_dir=inside, turn_id="t", now=NOW)
    assert (root / ".mcp.json").exists()


def test_check_backup_dir_missing_or_inside_area_raises(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    scan = _make_scan(root, ".mcp.json")
    with pytest.raises(ConfigError, match=r"needs TradewindConfig\.customization_backup_dir"):
        check_backup_dir(scan, None)
    for inside in (root, root / "sub" / "backups"):
        with pytest.raises(ConfigError, match="inside the scanned area"):
            check_backup_dir(scan, inside)
    # Outside the area: no error, and nothing is created.
    check_backup_dir(scan, tmp_path / "backups")
    assert not (tmp_path / "backups").exists()


def test_backup_folder_mode_0700(tmp_path: Path) -> None:
    root, backups = tmp_path / "repo", tmp_path / "backups"
    root.mkdir()
    old = os.umask(0)
    try:
        scan = _make_scan(root, ".claude/settings.local.json")
        backup, _ = apply_delete(scan, backup_dir=backups, turn_id="turn-1", now=NOW)
    finally:
        os.umask(old)
    for folder in (backups, backup, backup / ".claude"):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_path_outside_root_is_an_assertion_error(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "evil.json").write_text("x")
    scan = CustomizationScan(
        root=root, files=(FoundFile(Path("../evil.json"), "h", "k"),), fingerprint="fp1"
    )
    with pytest.raises(AssertionError):
        apply_delete(scan, backup_dir=tmp_path / "b", turn_id="t", now=NOW)
    assert (tmp_path / "evil.json").exists()


def test_decision_model_values() -> None:
    for action in ("stop", "delete", "proceed"):
        assert CustomizationDecision(action=cast(Any, action), fingerprint="f").action == action
    with pytest.raises(ValidationError):
        CustomizationDecision(action=cast(Any, "ignore"), fingerprint="f")
    with pytest.raises(ValidationError):
        _decision("stop").action = cast(Any, "proceed")  # frozen
    import tradewind

    assert tradewind.CustomizationDecision is CustomizationDecision


def test_readme_keyword_call_works() -> None:
    from tradewind import CustomizationDecision as PublicDecision

    # As in the README: the action comes from the caller's own store.
    action: Literal["stop", "delete", "proceed"] = "delete"
    decision = PublicDecision(action=action, fingerprint="fp1")
    assert (decision.action, decision.fingerprint) == ("delete", "fp1")
    # A pydantic model: positional arguments are not accepted.
    with pytest.raises(TypeError):
        PublicDecision(action, "fp1")  # type: ignore[misc]


def test_outcome_is_frozen_dataclass() -> None:
    outcome = DecisionOutcome(run=True, notice=None, delete=(), files_text="")
    with pytest.raises(dataclasses.FrozenInstanceError):
        outcome.run = False  # type: ignore[misc]


def test_partial_delete_failure_says_what_happened(tmp_path: Path) -> None:
    root, backups = tmp_path / "repo", tmp_path / "backups"
    root.mkdir()
    scan = _make_scan(root, "a.json", "sub/b.json", "c.json")
    os.chmod(root / "sub", 0o500)  # unlink of sub/b.json fails
    try:
        with pytest.raises(ConfigError) as exc:
            apply_delete(scan, backup_dir=backups, turn_id="turn-1", now=NOW)
    finally:
        os.chmod(root / "sub", 0o700)
    msg = str(exc.value)
    assert "deleted: a.json" in msg
    assert "failed: sub/b.json (" in msg
    assert "not attempted: c.json" in msg
    assert f"backup at {backups / FOLDER}" in msg
    assert not (root / "a.json").exists()
    assert (root / "c.json").exists()
    assert (backups / FOLDER / "c.json").exists()


def test_absolute_path_is_an_assertion_error(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    other = tmp_path / "evil.json"
    other.write_text("x")
    scan = CustomizationScan(root=root, files=(FoundFile(other, "h", "k"),), fingerprint="fp1")
    with pytest.raises(AssertionError):
        apply_delete(scan, backup_dir=tmp_path / "b", turn_id="t", now=NOW)
    assert other.exists()


def test_symlinked_parent_folder_outside_root_is_refused_before_any_copy(tmp_path: Path) -> None:
    root, outside = tmp_path / "repo", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "x.json").write_text("x")
    (root / "link").symlink_to(outside, target_is_directory=True)
    (root / "ok.json").write_text("ok")
    scan = CustomizationScan(
        root=root,
        files=(FoundFile(Path("ok.json"), "h", "k"), FoundFile(Path("link/x.json"), "h", "k")),
        fingerprint="fp1",
    )
    with pytest.raises(ConfigError) as exc:
        apply_delete(scan, backup_dir=tmp_path / "b", turn_id="t", now=NOW)
    assert str(exc.value) == (
        f"cannot delete through a symlinked folder: link -> {outside.resolve()}; "
        "remove the link and scan again"
    )
    assert (root / "ok.json").exists()
    assert (outside / "x.json").exists()
    assert not (tmp_path / "b").exists()
