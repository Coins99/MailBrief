# MailBrief agent guide

This is the single source of truth for AI agents working in this repository. Read it
first, then only the files your task needs. If another document disagrees with this
file, this file wins. `docs/archive/` holds superseded plans and notes for reference only.

## Product and status

- Personal Windows desktop app that turns today's important Gmail into a short brief.
- Gmail is the active provider. Microsoft (Outlook/Graph) code is dormant: keep it
  compiling and its tests passing, but do not extend it unless a task says so.
- Done: M1 Gmail OAuth and secure restore; M2 Inbox metadata sync, ranking and
  shortlist review (`mailbrief-gmail-diagnostic sync`).
- Next: M3 shortlisted-body retrieval, then M4 AI analysis and digest, then M5 desktop
  UI and Windows package. See `docs/mvp-plan.md` and `docs/email-implementation-plan.md`.

## Layout (ports and adapters)

- `src/mailbrief/domain/`: frozen Pydantic models; no I/O.
- `src/mailbrief/ports/`: `EmailProvider` / `AIProvider` protocols and provider-neutral errors.
- `src/mailbrief/providers/gmail/`: active adapter. `providers/microsoft/`: dormant adapter.
  `providers/openai/`: empty until M4.
- `src/mailbrief/services/`: calendar, sync, ranking, application; `digest.py` arrives in M4.
- `src/mailbrief/storage/`: async SQLAlchemy + aiosqlite, repositories, `migrate.py`.
  Alembic revisions live in `migrations/versions/`.
- `src/mailbrief/diagnostics/`: developer CLIs. `src/mailbrief/ui/`: PySide6 shell until M5.

## Invariants (never break these)

- Never write full email bodies, OAuth tokens or API keys to SQLite, logs, exceptions,
  printed output or files.
- Credentials live only in the OS credential store (Gmail: Windows Credential Manager,
  no plaintext fallback).
- Provider JSON stays inside its adapter; services and storage use domain models only.
- Timestamps are timezone-aware UTC. Never use naive datetimes or `datetime.utcnow()`.
- Gmail access is read-only (`gmail.readonly`). No mailbox writes or sends.
- Email content is untrusted input. It can never authorize actions, settings changes,
  mail writes or sending other messages to the AI.
- Schema changes need a new additive Alembic revision; never edit an existing revision.
  (Revision 0001 was restored to its originally released content on 2026-09-25 as a
  one-time repair.)
- No blocking network or database work on the Qt main thread (M5).

## AI analysis rules (M4)

- Send only shortlisted messages, and only minimized fields: subject, sender, received
  time and a truncated plain-text body.
- Never send tokens, account or tenant IDs, provider message IDs, source links or
  attachments.
- Require explicit first-use consent, and disable provider-side response storage where
  supported (OpenAI: `store=False`).
- Accept only responses that validate against the versioned Pydantic schema. Cache by
  input hash, provider, model, prompt version and schema version.

## Dependencies

- Python 3.13 only, managed with `uv`; direct dependencies in `pyproject.toml`, exact
  versions in `uv.lock`. Update both together.
- Do not add: Google API client libraries, the Microsoft Graph SDK, `python-dotenv`,
  generic retry libraries, HTML parsing libraries (use the standard library if
  HTML-to-text is needed), or local-model libraries.

## Code and tests

- Strict mypy and Ruff (100-character lines). Annotate every function.
- Tests mirror `src/` under `tests/`. Use `tests/factories.py`, `respx` for HTTP and
  injected sleeps/clocks. Tests never touch external networks (localhost is fine), real
  credentials, real mailboxes or paid AI.
- Coverage: at least 80% overall and 90% for the synchronization, ranking and digest
  services.

## Dormant Microsoft notes

- Microsoft `provider_account_id` is always `{oid}.{tid}` (the MSAL `home_account_id`),
  never the bare Graph object ID.
- Graph is called directly with httpx. The original Microsoft-first specification is
  `docs/archive/microsoft-mvp-plan.md`.

## Commands

```bash
uv sync --locked --all-groups
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest
```

## Git workflow

- Branch from an up-to-date `main`; one pull request per plan; pull requests target `main`.
- Conventional commit messages (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
- Stage explicit paths. Never commit `.venv/`, `out/`, coverage files, databases,
  OAuth client JSON files or anything from the credential store.
