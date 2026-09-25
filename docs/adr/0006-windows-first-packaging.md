# ADR 0006: Package Windows first with PyInstaller

- Status: Accepted
- Date: 2026-08-31
- Amended by: ADR 0008 (macOS is also a supported platform)

## Context

The first users are on Windows, Microsoft account integration is the initial
provider, and desktop packaging needs to include Python, Qt, migrations, and
application resources.

## Decision

Build MailBrief 0.1.0 on Windows with PyInstaller in `onedir` mode. Produce the
artifact only from a clean checkout and `uv sync --locked`. Do not promise a
signed installer, automatic updater, macOS package, or Linux package in the MVP.

## Consequences

- Test and release builds run on Windows because PyInstaller is not a
  cross-compiler.
- `onedir` is easier to inspect and debug than a single-file executable.
- A clean Windows VM is part of the release acceptance process.
- Additional operating systems require their own build and QA pipelines.
