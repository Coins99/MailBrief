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
- Implemented for M3, live acceptance recorded in [M3 validation](docs/m3-validation.md):
  shortlisted body reading and preparation ([Gmail bodies](docs/gmail-bodies.md)).
- Implemented for M4, awaiting live acceptance: consented Groq analysis and the saved
  daily brief ([AI analysis](docs/ai-analysis.md)).
- Implemented, awaiting acceptance: M5 desktop workflow, with full-Inbox shortlist
  review, consent, cancellation, saved-brief restoration, offline metadata browsing,
  graphical settings, and native packaging scripts.
  See [desktop usage, builds and remaining acceptance](docs/m5-desktop.md).
  macOS package execution and packaged live-account checks remain pending.
- Implemented, awaiting live acceptance: M6 action suggestions on the brief, accepted
  actions with editable plans and target dates, open/waiting/completed lists and undo.
  See [M6 actions](docs/m6-actions.md).
- M7 implemented, awaiting live acceptance: local drafts and notes, and AI drafting with
  Groq from context you choose.
  See [M7 drafts, notes and messages](docs/m7-drafts.md).
- M8 in progress on `feat/m8-continuity` (draft PR #17); preferences (Parts 1–2)
  implemented, awaiting live acceptance: your time zone, messages per brief, excluded
  senders, drafting defaults and AI limits, shared by the desktop and CLI. Brief history
  and catch-up for missed days (Part 3), tracking the threads of open actions (Part 4),
  thread activity and proposed updates to actions in the desktop, with replies outside
  today's Inbox offered in review (Parts 5–7), implemented, awaiting live acceptance.
  See [M8 daily operation](docs/m8-daily-operation.md).

## Start here

- [Scope](docs/mvp-scope.md)
- [Implementation plan](docs/mvp-plan.md)
- [Detailed email milestones and acceptance checks](docs/email-implementation-plan.md)
- [Later desktop ecosystem and website guidelines](docs/ecosystem-roadmap.md)
- [Gmail setup and authentication diagnostic](docs/gmail-setup.md)
- [Gmail metadata synchronization and shortlist review](docs/gmail-metadata.md)
- [Gmail bodies (M3) and live acceptance](docs/gmail-bodies.md)
- [AI analysis, consent and the daily brief (M4)](docs/ai-analysis.md)
- [Actions, target dates and plans (M6)](docs/m6-actions.md)
- [Drafts, notes and messages (M7)](docs/m7-drafts.md)
- [Preferences and daily operation (M8)](docs/m8-daily-operation.md)
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

Keep the checkout and its `.venv` out of folders synced by iCloud Drive (Desktop &
Documents), OneDrive or Dropbox: they set the macOS hidden flag on `.venv` files, and then
Python 3.13 skips hidden `.pth` files and Qt skips hidden plugins.
