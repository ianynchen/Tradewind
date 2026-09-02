#!/usr/bin/env bash
set -euo pipefail

# Quality gate checks for tradewind package

echo "Running code format check..."
uv run ruff format --check src tests

echo "Running linter..."
uv run ruff check

echo "Running type checker..."
uv run mypy src

echo "Running import linter..."
uv run lint-imports

echo "Running tests..."
uv run pytest

echo "✓ All checks passed"
