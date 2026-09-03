# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Lowered the supported Python floor from ≥3.13 to ≥3.12 so downstream
  projects declaring `requires-python = ">=3.12"` (e.g. sextant) can resolve
  tradewind. No source changes were needed; the full suite, mypy strict,
  ruff, and the import-linter contracts pass on both 3.12 and 3.13.
