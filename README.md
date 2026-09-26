# MailBrief

MailBrief is a desktop app for Windows and macOS that turns today's important
email into a short brief. Gmail is the first supported mailbox. Outlook code is retained for
later use after Microsoft Entra access is resolved.

## Current state

- Implemented: provider-neutral models, SQLite storage, daily sync, ranking, and
  a tested Microsoft Graph adapter.
- Implemented for M1: Gmail OAuth, OS credential storage (Windows Credential Manager or
  macOS Keychain), and an authentication
  diagnostic. The owner verified sign-in, restoration, disconnect and reconnect.
- Implemented for M2: Gmail Inbox metadata, pagination, safe reconciliation,
  local ranking and a metadata sync/review diagnostic.
- Next: shortlisted body retrieval (M3).
- Pending: AI analysis, digest assembly, desktop workflow, and packaging.

## Start here

- [Scope](docs/mvp-scope.md)
- [Implementation plan](docs/mvp-plan.md)
- [Detailed email milestones and acceptance checks](docs/email-implementation-plan.md)
- [Later desktop ecosystem and website guidelines](docs/ecosystem-roadmap.md)
- [Gmail setup and authentication diagnostic](docs/gmail-setup.md)
- [Gmail metadata synchronization and shortlist review](docs/gmail-metadata.md)
- [File change map](docs/change-map.md)
- [Microsoft work retained for later](docs/microsoft-setup.md)

## Development

```powershell
uv sync --locked --all-groups
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest
```

Python is pinned to 3.13. Runtime code lives under `src/mailbrief`; tests mirror
that structure under `tests`.
