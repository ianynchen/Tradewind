"""Architecture tests for layer enforcement."""

import subprocess
from pathlib import Path


def test_import_contracts_enforced() -> None:
    """Verify import layer contracts are enforced via lint-imports."""
    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["lint-imports"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"lint-imports failed with exit code {result.returncode}\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
