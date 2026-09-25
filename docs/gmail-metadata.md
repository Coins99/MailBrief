# M2: today's Inbox metadata and ranked shortlist

M2 adds paginated Gmail metadata retrieval, local SQLite persistence and existing
deterministic ranking. It does not retrieve full bodies, download attachments,
call AI, or modify the mailbox. The desktop UI is still a foundation shell; use
the diagnostic until UI wiring in M5.

## Run a daily sync

Keep `MAILBRIEF_GMAIL_OAUTH_CLIENT_PATH` set to the Desktop OAuth JSON used for M1.
From the project directory, run:

```powershell
uv run mailbrief-gmail-diagnostic sync --silent-only --timezone America/Toronto
```

Omit `--timezone` to use the system timezone. Omit `--silent-only` if you need an
interactive sign-in. Existing `fetch` and `disconnect` commands retain their M1
behavior; `fetch` remains a connection check.

The new `sync` command upgrades the app-data SQLite database using Alembic before
syncing. `--database 'C:\private\mailbrief-test.sqlite3'` selects a separate
database for testing; this explicit path takes precedence over the Alembic URL
environment setting. Credentials still use the same OS vault, not that database.
Do not launch overlapping sync commands against the same database.

Default output shows the local date/timezone, status, page count, retrieved
metadata count, failed-item count, selected count and last complete sync time.
No addresses, subjects or snippets are printed by default. A partial/failed run
returns exit code 4; database/file failures return 5. Other codes match the
[authentication guide](gmail-setup.md).

## Review the shortlist

To intentionally display IDs, subjects, senders, scores and account-aware Gmail
source links in your terminal:

```powershell
uv run mailbrief-gmail-diagnostic sync --silent-only --show-metadata
```

Each item is labelled selected or omitted. Review the omitted items too: local
ranking is a heuristic, not proof that every important message was selected.
Copy a displayed message ID to adjust the next run's selection:

```powershell
uv run mailbrief-gmail-diagnostic sync --silent-only --include MESSAGE_ID
uv run mailbrief-gmail-diagnostic sync --silent-only --exclude MESSAGE_ID
```

Repeat flags for multiple IDs. Includes take priority over automatic ranking;
exclusions are not automatically backfilled. IDs must belong to the current
account/day's cached Inbox and cannot appear in both lists. The shortlist stays
bounded to ten messages. These choices apply to the current run; persistent
review preferences and the visual review screen come later. No body or cloud
analysis follows this command yet.

## Coverage and cache behavior

- Query the Inbox with epoch boundaries broad enough to include boundary
  messages, then apply the exact local-day half-open UTC window using Gmail's
  `internalDate`. The sender-supplied Date header does not control membership.
- Follow all page tokens, deduplicate IDs across pages, and fetch at most five
  metadata records concurrently. Requests use `format=metadata`, selected
  headers and a field mask that excludes MIME bodies/attachment data.
- Store bounded Gmail snippets as previews. These are sensitive mail-derived
  metadata, even though full message bodies are not stored.
- Use finite request/retry budgets, cancellation, and one refresh attempt for
  rejected access tokens. Large Retry-After values produce a retry-later state.
- Mail deleted between listing and retrieval or no longer in the Inbox is
  skipped. Malformed/failed individual metadata items count as failures while
  valid items can still be stored. Fatal permission/list failures stop the run.
- Only a complete error-free enumeration reconciles unseen Inbox membership and
  advances last-complete-sync time. Archived/deleted records remain cached for
  future history/source references but are excluded from the current shortlist.
- Partial results may include previously cached Inbox records; their membership
  cannot be confirmed until a complete refresh. The CLI labels coverage incomplete.
- Gmail listing is not a transactional mailbox snapshot. Changes during paging
  may require another refresh. Exact history-based reconciliation follows in M8.

Metadata format does not reliably reveal file attachments, so M2 does not award
the attachment ranking bonus. Gmail's IMPORTANT label maps to high importance;
it is Gmail prioritization rather than a sender's explicit importance declaration.
Missing/invalid sender headers use `unknown@invalid`; optional malformed addresses
are skipped. Source links select the mailbox via `authuser` and open the thread.
Verify browser behavior with your own signed-in Google accounts.

## Live acceptance

1. Run a complete sync, inspect the date and counts, and optionally compare its
   displayed metadata against Gmail's Inbox for that date.
2. Repeat the same sync; cached message records should not duplicate.
3. If you independently archive a message in Gmail, refresh: its cached record
   remains but it should disappear from the current shortlist.
4. Test an include/exclude choice and cancel a sync with Ctrl+C. Do not interpret
   a cancelled/partial refresh as proof that unseen mail was removed.
5. Open a displayed source link and check that it uses the correct Google account.

References: [Gmail list API](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list),
[metadata retrieval](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get),
[message timestamps](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages),
and [Gmail error handling](https://developers.google.com/workspace/gmail/api/guides/handle-errors).
