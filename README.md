# MailBrief

MailBrief is a Python-first desktop app that creates a daily highlight of important emails, action items, and deadlines across connected inboxes.

## Goals

- Combine multiple email accounts into one daily brief.
- Surface important messages, actions, and deadlines.
- Support Microsoft 365/Outlook first, Gmail next, and IMAP providers later.
- Keep storage local by default.
- Allow cloud or local AI through a provider interface.

## Planned Stack

- Python 3.13+
- PySide6 / Qt
- asyncio + httpx
- Pydantic
- SQLite + SQLAlchemy
- Microsoft Graph + MSAL
- Gmail API + Google OAuth
- Optional IMAP support

## Architecture

Mail providers are isolated behind a common interface. Messages are normalized before ranking, AI analysis, storage, and display.

See [`docs/architecture.puml`](docs/architecture.puml).

## MVP

1. Connect a Microsoft account.
2. Fetch the day's inbox messages.
3. Normalize and cache messages locally.
4. Rank messages using local rules.
5. Send shortlisted messages for structured AI analysis.
6. Generate a daily brief.
7. Open the original email from a highlight.

## Constraints

- OAuth is required per provider and account.
- Email content is sensitive; stored and transmitted data should be minimized.
- Provider APIs differ and must be normalized.
- API keys must not be embedded in distributed builds.
- Sync and AI work must not block the desktop UI.
- API quotas and rate limits must be handled.

## Future Scope

- Gmail and multi-account support
- IMAP provider support
- Local AI models
- Configurable ranking rules
- Background daily summaries
- Optional Outlook add-in
