# MVP scope

MailBrief 0.1 is a Windows desktop app for one personal Gmail account. A user can
connect with Google OAuth, generate a brief for today's Inbox, and open each source
message in Gmail.

## Included

- Gmail read-only access and secure session restoration
- Local-day Inbox pagination, deduplication, SQLite metadata, and local ranking
- Body retrieval only for shortlisted messages
- Explicit consent before shortlisted content is sent to the configured AI
- Structured highlights, actions, decisions, and deadlines
- Responsive, cancellable PySide6 workflow and Windows `onedir` package
- Retained but dormant Microsoft provider and diagnostic

## Deferred

- Public Gmail distribution and its verification/CASA assessment decision
- Outlook as a supported UI option, multiple accounts, IMAP, and mail changes
- Attachments, background sync, local AI, telemetry, installers, and auto-update

## Release checks

The packaged app must connect, restore, sync every page in the exact local-day
window, avoid duplicates, rank deterministically, fetch only shortlisted bodies,
cache valid AI results, handle cancellation/failures, and open source messages.
Tokens, API keys, and full bodies must be absent from SQLite and logs. CI requires
Ruff, strict mypy, tests, 90% service coverage, and 80% overall coverage.
