# M1 implementation and validation record

Date: 2026-09-25. Implementation branch: `feat/gmail-m1-auth`.

## Delivered

- System-browser Desktop OAuth with PKCE, state checking, a bounded loopback
  listener, timeout/cancellation and read-only Gmail scope.
- Refresh/session restoration and mailbox identity validation before persistence.
- Windows Credential Manager storage, with no plaintext fallback or token file.
- Safe `fetch`, `fetch --silent-only`, and local `disconnect` diagnostics.
- [Setup and live acceptance instructions](gmail-setup.md).

The diagnostic verifies the Gmail profile only. Message retrieval remains M2.
No shared provider contract, SQLite schema or Microsoft implementation changed.
Existing httpx/keyring dependencies suffice; no new dependency is required.

## Automated validation

- Baseline: 232 tests passed; strict mypy passed before implementation.
- Final: 291 tests passed (59 new), 95.48% overall coverage.
- Strict mypy: 86 source/test files passed.
- Ruff lint and format checks passed; generated `out/` artifacts excluded.
- Installed diagnostic entry point and both help screens verified.
- One existing SQLAlchemy connection-cleanup warning occurs in
  `test_message_repository_upsert_and_ranking`, also observed at baseline.

Tests use fake credentials and mocked Google responses. OAuth listener tests use
actual localhost requests, covering PKCE/state, consent denial, invalid callbacks,
timeout and cancellation. Other tests cover token refresh/revocation, account
mismatch, cache failure, safe errors, silent mode, disconnect and resource cleanup.
No real credentials, mailbox contents or Google sign-in were used.

## Validation environment

The existing OneDrive virtual environment had missing/locked Ruff files. A fresh
environment installed successfully from the unchanged lockfile outside OneDrive:

```powershell
$env:UV_PROJECT_ENVIRONMENT = Join-Path $env:TEMP 'mailbrief-m1-validation-20260925'
uv sync --locked --all-groups --link-mode copy
uv run ruff check . --extend-exclude out
uv run ruff format --check . --extend-exclude out
uv run mypy src tests --cache-dir out/m1-mypy-cache
```

The full pytest run used the normal strictness/coverage requirements, with temp,
cache and coverage paths redirected to fresh locations under `out/` to avoid
locked artifacts. A normal development environment can use README commands.
Do not commit validation environments or generated outputs.

## Remaining live gate

The owner has not yet created/downloaded the Google Desktop OAuth client file.
Interactive sign-in, restart restoration, local disconnect and Google-side
revocation/recovery must still be checked with the intended account. M1 is
implemented and automatically tested, but its live acceptance gate is pending.
