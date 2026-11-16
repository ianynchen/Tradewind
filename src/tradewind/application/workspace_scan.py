"""Workspace customization scan (docs/components/06-workspace-safety.md §1, §2).

Finds the files in a working folder (and its repository) that an engine
would load and that can run code or loosen permissions without a broker
question. Each backend declares what to look for
(`Backend.customization_patterns()`); this module is the one generic
mechanism that applies those patterns and fingerprints the result.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from tradewind.domain.errors import ConfigError

if TYPE_CHECKING:
    # TYPE_CHECKING only: `application.ports` imports `CustomizationPattern`
    # from this module, so a runtime import here would loop.
    from tradewind.application.ports import Backend
    from tradewind.domain.models import SessionRow


@dataclass(frozen=True)
class CustomizationPattern:
    """What a backend flags. `path` is relative to each scanned folder.
    For a folder pattern (`is_dir`), every regular file below it counts;
    symlinked folders inside it are not followed."""

    path: str
    kind: str
    is_dir: bool


@dataclass(frozen=True)
class FoundFile:
    """A flagged file. `path` is relative to the scan root; `sha256` is the
    hash of the content the engine would read (symlinks are followed)."""

    path: Path
    sha256: str
    kind: str


@dataclass(frozen=True)
class CustomizationScan:
    """Result of `scan`. `fingerprint` is `""` when `files` is empty."""

    root: Path
    files: tuple[FoundFile, ...]
    fingerprint: str


def find_root(folder: Path) -> Path | None:
    """The nearest folder, starting at `folder` itself, that holds a `.git`
    file or folder (a file marks a worktree or submodule). None if there is
    no repository."""
    folder = folder.resolve()
    for candidate in (folder, *folder.parents):
        if _lstat(candidate / ".git") is not None:
            return candidate
    return None


def scan(folder: Path, patterns: Sequence[CustomizationPattern], backend: str) -> CustomizationScan:
    """Scan `folder`, the repository root and every folder between them
    (only `folder` when there is no repository) for `patterns`.

    Empty `patterns` return an empty scan without touching the file system.

    Raises:
        ConfigError: a flagged file cannot be read (no permission, broken
            symlink). The message names the file; it is never skipped.
    """
    if not patterns:
        return CustomizationScan(root=folder, files=(), fingerprint="")
    folder = folder.resolve()
    root = find_root(folder) or folder
    area = [folder, *(p for p in folder.parents if p.is_relative_to(root))]
    found: dict[Path, FoundFile] = {}
    for base in area:
        for pattern in patterns:
            for path in _candidates(base / pattern.path, pattern.is_dir):
                rel = path.relative_to(root)
                if rel not in found:
                    found[rel] = _found(path, rel, pattern.kind)
    files = tuple(found[rel] for rel in sorted(found, key=Path.as_posix))
    if not files:
        return CustomizationScan(root=root, files=(), fingerprint="")
    lines = [backend, str(root.resolve()), *(f"{f.path.as_posix()} {f.sha256}" for f in files)]
    fingerprint = hashlib.sha256(os.fsencode("\n".join(lines))).hexdigest()
    return CustomizationScan(root=root, files=files, fingerprint=fingerprint)


def scan_session(session: SessionRow, backend: Backend) -> CustomizationScan:
    """Scan the engine's working folder for `backend`'s patterns: the
    session's `cwd` when it is set, else the process cwd (06 §2 "One
    folder"). `Session.scan_customizations()` and the turn runner both call
    this, so they give the same fingerprint. With no patterns it touches
    nothing, not even the process cwd.

    Raises:
        ConfigError: as `scan`.
    """
    patterns = backend.customization_patterns()
    if not patterns:
        root = Path(session.cwd) if session.cwd is not None else Path()
        return CustomizationScan(root=root, files=(), fingerprint="")
    folder = Path(session.cwd) if session.cwd is not None else Path.cwd()
    return scan(folder, patterns, backend.name)


def _lstat(path: Path) -> os.stat_result | None:
    """`os.lstat`, or None when the path is absent. Only a missing path
    (or a file where a folder was expected) counts as absent; any other
    error, such as an unreadable parent folder, raises `ConfigError`."""
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise ConfigError(f"cannot inspect workspace path {path}: {exc}") from exc


def _walk_failed(exc: OSError) -> None:
    where: object = exc.filename
    raise ConfigError(f"cannot read workspace folder {where}: {exc}") from exc


def _candidates(path: Path, is_dir: bool) -> list[Path]:
    """Existing paths for one pattern. A broken symlink still counts as
    present, so that reading it fails loudly. A pattern folder that is
    itself a symlink is followed (the engine follows it); symlinked folders
    nested inside it are not."""
    st = _lstat(path)
    if st is None:
        return []
    if not is_dir:
        return [path]
    if stat.S_ISLNK(st.st_mode):
        try:
            st = os.stat(path)
        except OSError as exc:
            raise ConfigError(f"cannot read workspace folder {path}: {exc}") from exc
    if not stat.S_ISDIR(st.st_mode):
        return []
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(path, onerror=_walk_failed, followlinks=False):
        dirnames.sort()
        out.extend(Path(dirpath) / name for name in sorted(filenames))
    return out


def _found(path: Path, rel: Path, kind: str) -> FoundFile:
    try:
        regular = stat.S_ISREG(path.stat().st_mode)
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if regular else ""
    except OSError as exc:
        raise ConfigError(f"cannot read workspace customization {path}: {exc}") from exc
    if not regular:
        raise ConfigError(f"workspace customization is not a regular file: {path}")
    return FoundFile(path=rel, sha256=digest, kind=kind)
