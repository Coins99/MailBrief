# ADR 0002: Use uv for Python project management

- Status: Accepted
- Date: 2026-08-31

## Context

The desktop application needs reproducible developer, CI, and release
environments. Dependency resolution must include Windows-specific wheels and a
large Qt dependency tree.

## Decision

Use `pyproject.toml` for direct dependency declarations and `uv.lock` as the
committed exact resolution. Use `uv sync --locked` in CI and release builds.
Keep the project virtual environment at `.venv` and exclude it from Git.

## Consequences

- A clean checkout can reproduce the complete environment from one lockfile.
- Direct dependencies remain readable while transitive versions remain exact.
- Dependency changes must update both `pyproject.toml` and `uv.lock`.
- Contributors need `uv`, but do not need to manage `pip` or virtual environments
  manually.

