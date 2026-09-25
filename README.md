# MailBrief

MailBrief is a Windows desktop app that turns today's important email into a
short brief. Gmail is the first supported mailbox. Outlook code is retained for
later use after Microsoft Entra access is resolved.

## Current state

- Implemented: provider-neutral models, SQLite storage, daily sync, ranking, and
  a tested Microsoft Graph adapter.
- Implemented for M1: Gmail OAuth, Windows credential storage, and an authentication
  diagnostic. Real-account sign-in validation is pending.
- Next: Gmail API message mapping and metadata synchronization (M2).
- Pending: AI analysis, digest assembly, desktop workflow, and packaging.

## Start here

- [Scope](docs/mvp-scope.md)
- [Implementation plan](docs/mvp-plan.md)
- [Detailed email milestones and acceptance checks](docs/email-implementation-plan.md)
- [Later desktop ecosystem and website guidelines](docs/ecosystem-roadmap.md)
- [Gmail setup and authentication diagnostic](docs/gmail-setup.md)
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
