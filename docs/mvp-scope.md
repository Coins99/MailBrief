# MailBrief MVP Scope

Status: Frozen for version 0.1.0

This document is the scope contract for the first MailBrief release. New work
that is not explicitly included below must be deferred unless the scope is
formally revised.

## Product statement

MailBrief 0.1.0 is a Windows desktop application that connects one Microsoft
mail account, collects metadata for messages received in the Inbox during the
user's current local calendar day, ranks those messages locally, sends only a
shortlist to a configured cloud AI provider, and presents a daily brief with
links to the original messages.

## Included

- One Microsoft 365, Outlook, or personal Microsoft account.
- Interactive Microsoft OAuth with a securely persisted token cache.
- A manual **Generate today's brief** action.
- Time-zone-aware retrieval of the current day's Inbox messages.
- Complete Graph pagination and message deduplication.
- Local SQLite storage for account data, message metadata, ranking results,
  analyses, digests, and synchronization history.
- Deterministic local ranking before any full message body is fetched.
- Plain-text body retrieval for shortlisted messages only.
- One OpenAI implementation behind an `AIProvider` protocol.
- Structured highlights, actions, decisions, and deadlines.
- A link from every digest item to the original Microsoft message.
- A responsive, cancellable PySide6 workflow.
- A Windows `onedir` package built with PyInstaller.

## Explicitly deferred

- Gmail, IMAP, and multiple connected accounts.
- Local AI models and additional cloud AI implementations.
- Attachment download or analysis.
- Background sync, scheduled briefs, notifications, and auto-start.
- User-configurable ranking rules.
- Replying, deleting, moving, or marking email as read.
- Outlook add-ins, mobile clients, web clients, and cross-platform packages.
- Installer signing, automatic updates, and telemetry.

## Privacy boundary

- Access tokens, refresh tokens, and AI API keys must use operating-system-backed
  encrypted storage and must never be written to SQLite or logs.
- Full message bodies are held in memory only.
- Only shortlisted messages may be sent to the AI provider.
- Attachments, tenant identifiers, Graph message identifiers, and `webLink`
  values are not AI inputs.
- Cloud analysis requires explicit first-use consent.
- AI requests use provider controls that disable response storage where
  supported.

## Acceptance criteria

- **AC-01 — Installation:** The packaged application starts on a clean supported
  Windows machine without a separately installed Python or Qt runtime.
- **AC-02 — Connection:** A user can connect one supported Microsoft account
  through interactive OAuth without a client secret embedded in the app.
- **AC-03 — Session restoration:** A valid cached session reconnects silently
  after an application restart.
- **AC-04 — Date correctness:** The application converts the user's local-day
  boundaries to UTC correctly, including daylight-saving transitions.
- **AC-05 — Complete retrieval:** Every Inbox metadata page in the requested
  interval is followed until Graph returns no `nextLink` or the user cancels.
- **AC-06 — Idempotency:** Repeating a sync does not create duplicate messages.
- **AC-07 — Local ranking:** Ranking is deterministic, runs before body retrieval,
  and records readable reasons for every score adjustment.
- **AC-08 — Data minimization:** Full bodies are fetched only for shortlisted
  messages, are limited before AI submission, and are not persisted.
- **AC-09 — Structured analysis:** Every accepted AI response satisfies the
  versioned Pydantic response schema.
- **AC-10 — Analysis reuse:** Unchanged message input analyzed with the same
  provider, model, prompt, and schema reuses the cached analysis.
- **AC-11 — Daily brief:** A completed or partially completed run displays
  highlights, actions, decisions, and deadlines in a deterministic order.
- **AC-12 — Source navigation:** Every digest item with a valid source link opens
  the original Microsoft message in the system browser or Outlook client.
- **AC-13 — Responsiveness:** Sync and AI work do not block UI repainting, and a
  user can cancel an active generation operation.
- **AC-14 — Recoverability:** Authentication, permission, throttling, timeout,
  offline, invalid-AI-response, and partial-result failures produce clear and
  recoverable states without exposing sensitive content.
- **AC-15 — Quality gate:** CI passes Ruff formatting and linting, strict mypy,
  and pytest with at least 90% coverage for synchronization, ranking, and digest
  services and at least 80% overall coverage.

## Release boundary

Version 0.1.0 is ready only when every acceptance criterion above is satisfied,
the Windows package has passed a clean-machine test, and all critical or
high-severity defects are closed.

