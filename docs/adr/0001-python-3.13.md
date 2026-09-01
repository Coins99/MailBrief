# ADR 0001: Use CPython 3.13

- Status: Accepted
- Date: 2026-08-31

## Context

MailBrief needs current typing features, reliable Windows binary wheels, and a
runtime supported by PySide6, qasync, SQLAlchemy, MSAL, and the selected AI SDK.
The qasync version selected for the MVP does not support Python 3.14.

## Decision

MailBrief 0.1.0 will use 64-bit CPython 3.13 and declare
`requires-python = ">=3.13,<3.14"`. The exact patch release is selected by `uv`
and recorded by the developer environment.

## Consequences

- Development and CI use the same Python minor version.
- Python 3.14 cannot be selected accidentally while qasync remains incompatible.
- Supporting a later Python version requires a dependency-compatibility review
  and an explicit update to this decision.

