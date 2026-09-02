"""Architecture tests for layer enforcement."""

import subprocess


def test_import_contracts_enforced() -> None:
    """Verify import layer contracts are enforced via lint-imports."""
    result = subprocess.run(
        ["lint-imports"],
        cwd="/Users/yining/programming/ai/tradewind",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"lint-imports failed with exit code {result.returncode}\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
