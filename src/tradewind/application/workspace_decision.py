"""Customization decision and delete (docs/components/06-workspace-safety.md §3).

Pure logic: `resolve` applies the decision table to a scan; `apply_delete`
backs up and removes the scanned files. The turn runner (not this module)
decides when to call them.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tradewind.application.workspace_scan import CustomizationScan
from tradewind.domain.errors import ConfigError
from tradewind.domain.models import CustomizationDecision


@dataclass(frozen=True)
class DecisionOutcome:
    """What the runner does. `delete` holds absolute paths to back up and
    remove; `notice` is the text for the `proceed` row (the runner builds
    the `delete` notice once it knows the backup path); `files_text` is the
    relative paths joined with ", "."""

    run: bool
    notice: str | None
    delete: tuple[Path, ...]
    files_text: str


def resolve(scan: CustomizationScan, decision: CustomizationDecision | None) -> DecisionOutcome:
    """Apply the §3 table.

    Raises:
        ConfigError: files found with no decision, a stale fingerprint, or
            a `stop` decision.
    """
    if not scan.files:
        return DecisionOutcome(run=True, notice=None, delete=(), files_text="")
    files_text = ", ".join(str(f.path) for f in scan.files)
    if decision is None:
        raise ConfigError(f"workspace customizations found: {files_text}; scan and pass a decision")
    if decision.fingerprint != scan.fingerprint:
        raise ConfigError("customizations changed since the scan")
    if decision.action == "stop":
        raise ConfigError(f"stopped by the caller: {files_text}")
    if decision.action == "delete":
        paths = tuple(scan.root / f.path for f in scan.files)
        return DecisionOutcome(run=True, notice=None, delete=paths, files_text=files_text)
    return DecisionOutcome(
        run=True,
        notice=f"workspace customizations loaded without broker control: {files_text}",
        delete=(),
        files_text=files_text,
    )


def _mkdir_0700(folder: Path) -> None:
    """Create `folder` and any missing parents, each with mode 0700."""
    missing = [p for p in (folder, *folder.parents) if not p.exists()]
    for path in reversed(missing):
        path.mkdir(mode=0o700)
        path.chmod(0o700)


def apply_delete(
    scan: CustomizationScan, *, backup_dir: Path | None, turn_id: str, now: datetime
) -> tuple[Path, tuple[Path, ...]]:
    """Copy every scanned file to `<backup_dir>/<UTC stamp>-<turn id>/<relative path>`,
    then delete them. Returns the backup folder and the paths (relative to
    `scan.root`) that were really deleted.

    A symlink is backed up as its target's content and removed as a link
    only. A file that is already gone is skipped. If any copy fails nothing
    is deleted and the backup is kept.

    Raises:
        ConfigError: no `backup_dir`, `backup_dir` inside the scan area, or
            a copy or a delete failed.
        AssertionError: a scanned path is outside `scan.root`.
    """
    root_real = scan.root.resolve()
    for f in scan.files:
        norm = os.path.normpath(f.path)
        outside = os.path.isabs(norm) or norm == ".." or norm.startswith(".." + os.sep)
        if outside:
            raise AssertionError(f"scanned path outside the scan root: {f.path}")
        # The last component may be a symlink; its parent folders may not lead out.
        if not (scan.root / norm).parent.resolve().is_relative_to(root_real):
            current = Path(".")
            for part in Path(norm).parent.parts:
                current = current / part
                link = scan.root / current
                if link.is_symlink():
                    raise ConfigError(
                        f"cannot delete through a symlinked folder: {current} -> "
                        f"{link.resolve()}; remove the link and scan again"
                    )
            raise AssertionError(f"scanned path outside the scan root: {f.path}")
    if backup_dir is None:
        raise ConfigError("delete needs TradewindConfig.customization_backup_dir")
    if backup_dir.resolve().is_relative_to(scan.root.resolve()):
        raise ConfigError(f"customization_backup_dir {backup_dir} is inside the scanned area")

    backup = backup_dir / f"{now.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}-{turn_id}"
    present = [f.path for f in scan.files if os.path.lexists(scan.root / f.path)]
    try:
        _mkdir_0700(backup)
        for rel in present:
            dest = backup / rel
            _mkdir_0700(dest.parent)
            shutil.copyfile(scan.root / rel, dest)  # follows a symlink
    except OSError as exc:
        raise ConfigError(f"customization backup failed, nothing deleted: {exc}") from exc
    deleted: list[Path] = []
    for index, rel in enumerate(present):
        try:
            (scan.root / rel).unlink()  # a symlink: the link only
        except FileNotFoundError:
            pass
        except OSError as exc:
            not_attempted = ", ".join(str(p) for p in present[index + 1 :]) or "none"
            raise ConfigError(
                f"deleted: {', '.join(str(p) for p in deleted) or 'none'}; "
                f"failed: {rel} ({exc}); not attempted: {not_attempted}; backup at {backup}"
            ) from exc
        else:
            deleted.append(rel)
    return backup, tuple(deleted)
