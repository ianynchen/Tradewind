"""Tests for the workspace customization scan (`application/workspace_scan.py`)
and the per-backend `customization_patterns()` declarations
(docs/components/06-workspace-safety.md §1, §2). Real temp folders, no
engine, no mocks of the scan itself."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, cast

import pytest

from tradewind.adapters.claude_backend import ClaudeBackend
from tradewind.adapters.codex_backend import CodexBackend
from tradewind.adapters.cursor_backend import CursorBackend
from tradewind.adapters.langchain_backend import LangchainBackend
from tradewind.application.config import NativeStoreConfig
from tradewind.application.workspace_scan import (
    CustomizationPattern,
    find_root,
    scan,
)
from tradewind.domain.errors import ConfigError
from tradewind.domain.models import ApiKeyAuth, ModelSpec, Profile, SubscriptionAuth

SETTINGS = CustomizationPattern(".claude/settings.json", "claude-settings", False)
HOOKS = CustomizationPattern(".claude/hooks", "claude-hook", True)
MCP = CustomizationPattern(".mcp.json", "claude-mcp", False)
PATTERNS = [SETTINGS, HOOKS, MCP]


def _write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _names(result: Any) -> set[str]:
    return {f.path.as_posix() for f in result.files}


def test_scan_area_is_folder_root_and_between(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    mid = root / "a"
    folder = mid / "b"
    folder.mkdir(parents=True)
    _write(root / ".mcp.json")
    _write(mid / ".claude" / "settings.json")
    _write(folder / ".claude" / "hooks" / "x.sh")
    result = scan(folder, PATTERNS, "claude")
    assert result.root == root
    assert _names(result) == {".mcp.json", "a/.claude/settings.json", "a/b/.claude/hooks/x.sh"}
    kinds = {f.path.as_posix(): f.kind for f in result.files}
    assert kinds[".mcp.json"] == "claude-mcp"


def test_nothing_above_root_is_scanned(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(tmp_path / ".mcp.json")
    _write(root / ".claude" / "settings.json")
    result = scan(root, PATTERNS, "claude")
    assert _names(result) == {".claude/settings.json"}


def test_no_repo_scans_folder_only(tmp_path: Path) -> None:
    folder = tmp_path / "work"
    _write(tmp_path / ".mcp.json")
    _write(folder / ".claude" / "settings.json")
    assert find_root(folder) is None
    result = scan(folder, PATTERNS, "claude")
    assert result.root == folder
    assert _names(result) == {".claude/settings.json"}


def test_git_file_marks_worktree_root(tmp_path: Path) -> None:
    root = tmp_path / "wt"
    _write(root / ".git", "gitdir: /elsewhere")
    folder = root / "sub"
    folder.mkdir()
    _write(root / ".mcp.json")
    assert find_root(folder) == root
    assert _names(scan(folder, PATTERNS, "claude")) == {".mcp.json"}


def test_folder_pattern_counts_every_file_and_skips_symlinked_dirs(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    hooks = root / ".claude" / "hooks"
    _write(hooks / "a.sh")
    _write(hooks / "deep" / "b.sh")
    outside = tmp_path / "outside"
    _write(outside / "c.sh")
    (hooks / "link").symlink_to(outside, target_is_directory=True)
    result = scan(root, [HOOKS], "claude")
    assert _names(result) == {".claude/hooks/a.sh", ".claude/hooks/deep/b.sh"}


def test_symlinked_file_hashes_target_content(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    target = _write(tmp_path / "real.json", "hello")
    (root / ".mcp.json").symlink_to(target)
    result = scan(root, [MCP], "claude")
    assert result.files[0].sha256 == hashlib.sha256(b"hello").hexdigest()


def test_unreadable_file_raises_config_error_with_path(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / ".mcp.json").symlink_to(tmp_path / "missing.json")  # broken link
    with pytest.raises(ConfigError, match=r"\.mcp\.json"):
        scan(root, [MCP], "claude")
    secret = _write(root / ".claude" / "settings.json")
    secret.chmod(0)
    try:
        if os.access(secret, os.R_OK):  # running as root: chmod does not block
            pytest.skip("file stays readable")
        with pytest.raises(ConfigError, match=r"settings\.json"):
            scan(root, [SETTINGS], "claude")
    finally:
        secret.chmod(0o600)


def test_fingerprint_stable_and_changes_with_content_root_and_backend(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    mcp = _write(root / ".mcp.json", "one")
    first = scan(root, [MCP], "claude").fingerprint
    assert first
    assert scan(root, [MCP], "claude").fingerprint == first
    assert scan(root, [MCP], "codex").fingerprint != first
    mcp.write_text("two")
    assert scan(root, [MCP], "claude").fingerprint != first
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    _write(other / ".mcp.json", "one")
    assert scan(other, [MCP], "claude").fingerprint != first


def test_fingerprint_encoding(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root / ".mcp.json", "one")
    digest = hashlib.sha256(b"one").hexdigest()
    lines = ["claude", str(root.resolve()), f".mcp.json {digest}"]
    expected = hashlib.sha256(os.fsencode("\n".join(lines))).hexdigest()
    # Pins the documented encoding: backend, root, then one line per pair.
    assert scan(root, [MCP], "claude").fingerprint == expected


def test_no_files_gives_empty_fingerprint(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    result = scan(root, PATTERNS, "claude")
    assert result.files == ()
    assert result.fingerprint == ""


def test_empty_patterns_touch_no_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("file system touched")

    for name in ("iterdir", "exists", "is_file", "is_dir", "stat", "resolve", "read_bytes", "open"):
        monkeypatch.setattr(Path, name, boom)
    monkeypatch.setattr(os, "scandir", boom)
    monkeypatch.setattr(os, "walk", boom)
    monkeypatch.setattr(os, "stat", boom)
    result = scan(tmp_path, [], "claude")
    assert result.files == ()
    assert result.fingerprint == ""
    assert result.root == tmp_path


def test_instruction_files_not_flagged(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root / "CLAUDE.md")
    _write(root / "AGENTS.md")
    _write(root / ".claude" / "skills" / "x" / "SKILL.md")
    _write(root / ".claude" / "commands" / "x.md")
    assert scan(root, PATTERNS, "claude").files == ()


def _profile(backend: Any, auth: Any) -> Profile:
    return Profile(backend=backend, auth=auth, models={"standard": ModelSpec(model="m")})


def test_backend_patterns() -> None:
    native = NativeStoreConfig()
    sub = SubscriptionAuth()
    claude = ClaudeBackend(_profile("claude", sub), native)
    assert claude.customization_patterns() == [
        CustomizationPattern(".claude/settings.json", "claude-settings", False),
        CustomizationPattern(".claude/settings.local.json", "claude-settings", False),
        CustomizationPattern(".claude/hooks", "claude-hook", True),
        CustomizationPattern(".mcp.json", "claude-mcp", False),
    ]
    assert CodexBackend(_profile("codex", sub), native).customization_patterns() == [
        CustomizationPattern(".codex/config.toml", "codex-config", False),
    ]
    assert CursorBackend(_profile("cursor", sub), native).customization_patterns() == []
    lc = LangchainBackend(
        _profile("langchain", ApiKeyAuth(api_key=cast(Any, "k"))),
        native,
        chat_model_factory=cast(Any, lambda *_a, **_k: None),
    )
    assert lc.customization_patterns() == []


def _skip_if_root(path: Path) -> None:
    if os.access(path, os.R_OK | os.X_OK):
        pytest.skip("permissions not enforced (running as root)")


def test_unreadable_dir_on_file_pattern_path_raises(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root / ".claude" / "settings.json")
    (root / ".claude").chmod(0)
    try:
        _skip_if_root(root / ".claude")
        with pytest.raises(ConfigError, match=r"\.claude"):
            scan(root, [SETTINGS], "claude")
        with pytest.raises(ConfigError, match=r"\.claude"):
            scan(root, [HOOKS], "claude")
    finally:
        (root / ".claude").chmod(0o700)


def test_unreadable_subdir_in_folder_pattern_raises(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root / ".claude" / "hooks" / "a.sh")
    sub = root / ".claude" / "hooks" / "sub"
    _write(sub / "b.sh")
    sub.chmod(0)
    try:
        _skip_if_root(sub)
        with pytest.raises(ConfigError, match="sub"):
            scan(root, [HOOKS], "claude")
    finally:
        sub.chmod(0o700)


def test_file_symlink_loop_raises(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / ".mcp.json").symlink_to(root / ".mcp.json")
    with pytest.raises(ConfigError, match=r"\.mcp\.json"):
        scan(root, [MCP], "claude")


def test_symlinked_pattern_folder_is_followed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    _write(elsewhere / "a.sh")
    nested_target = tmp_path / "nested"
    _write(nested_target / "n.sh")
    (elsewhere / "nested").symlink_to(nested_target, target_is_directory=True)
    (root / ".claude").mkdir()
    (root / ".claude" / "hooks").symlink_to(elsewhere, target_is_directory=True)
    result = scan(root, [HOOKS], "claude")
    assert _names(result) == {".claude/hooks/a.sh"}
