# Gmail bodies (M3)

M3 reads the text of the emails on the reviewed shortlist, so the AI step (M4) can analyze
them. The text exists in memory only, for the current run.

## What is read, and what never is

- Only messages on the reviewed shortlist: at most 10, after any `--include` or `--exclude`.
- Gmail's message structure (`format=full`). The plain-text version is preferred. HTML is used
  when the plain version is missing or only a stub, and is converted to text with Python's
  built-in parser: no scripts, styles, images, link addresses or hidden text, and nothing is
  fetched.
- Attachments are counted and never downloaded. Text that Gmail stores separately is fetched
  only up to 1 MB.
- Bodies are never written to SQLite, logs, files or error messages, and M3 sends nothing
  anywhere.

## How the text is prepared

1. The text is cleaned:
   - Link addresses are removed, because tracking links often identify you. A link inside a
     sentence becomes `[link]`.
   - HTML entities, comments and stray markup that some senders leave in plain-text versions
     are removed.
   - So are invisible padding, control characters, repeated long paragraphs and extra blank
     lines.
2. Quoted history after a reply is removed:
   - the trailing `>` block, with the "On ... wrote:" line above it, in any language;
   - Outlook's "-----Original Message-----" or its "From:" / "Sent:" / "Subject:" reply
     header (a "From:" / "Date:" block in a receipt or itinerary is kept);
   - Gmail's HTML quote blocks.

   Forwards are never trimmed. They are recognized by the subject ("Fwd:", "FW:", "WG:",
   "转发:") or by a forwarded-message marker. If trimming would leave nothing, the text is kept.
3. The text is limited to 8,000 characters, cut at a line or word break.
   `MAILBRIEF_AI_BODY_CHARACTER_LIMIT` can lower this limit, but never raise it.

Each email gets a status and flags:
- **Statuses:** `ready`, `empty` (no text at all), `unavailable` (deleted) or `failed`
  (the message, or every text part in it, couldn't be read).
- **Flags:** quoted history removed, truncated, attachments skipped, and unreadable parts.

## Review command

macOS (Terminal) or Windows (PowerShell), from the project folder, with the OAuth client path
set as in `docs/gmail-setup.md`:

```
uv run mailbrief-gmail-diagnostic bodies --silent-only
uv run mailbrief-gmail-diagnostic bodies --silent-only --show-text
```

The command first syncs today's metadata, exactly like `sync`, then prints one line per
shortlisted email.

`--show-text` also prints each email's sender, subject and prepared text, in your terminal
only. It is never saved or logged, so don't use it on a shared screen.

To review a specific email, add it with `--include <message ID>`. `mailbrief-gmail-diagnostic
sync --show-metadata` lists today's IDs.

Exit codes: 0 complete; 2 sign-in needed; 3 setup problem; 4 incomplete sync or a failed body;
5 database problem.

## Live acceptance (required before M4)

Send yourself these emails, or pick matching ones already in today's Inbox. Run
`bodies --show-text` and check every row.

| Test email | Expected |
| --- | --- |
| A reply in a thread | Only the new reply text; "quoted history removed" |
| A forward ("Fwd:") | The forwarded content is kept |
| A newsletter or marketing email | Readable text; no code, no invisible text |
| A non-English email (for example Chinese) | Characters display correctly |
| An email with a PDF attachment | "attachments skipped: 1"; no attachment content |
| An email longer than 8,000 characters | "truncated"; the text ends at a line or word break |
| An email containing only an image | `empty` or very short text, and no error |

Record the results in `docs/m3-validation.md`. M3 is done when every row passes on the Mac.
The same check on Windows is optional.

## Known limits

- Beyond `>` quotes, reply headers are recognized only in Outlook's English format.
- Image-only emails have no text to analyze.
- A message whose Gmail response exceeds 2 MB is reported as `failed`.
