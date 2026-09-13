# Gmail-first plan

Each phase should be a reviewable change. Keep the Microsoft suite passing.

## 1. Provider seam

- Make Gmail the configured default and keep Microsoft selectable for diagnostics.
- Add Gmail to domain/storage tests and reject unknown persisted providers.
- Preserve the database schema and existing `microsoft` values.

Exit: all existing tests pass with both provider identities understood.

## 2. Gmail authentication

- Add Desktop OAuth using the system browser, PKCE, loopback callback, and
  `gmail.readonly`.
- Keep refresh credentials encrypted in the OS credential store.
- Add safe `fetch`, `--silent-only`, and `disconnect` diagnostics.

Exit: one real Gmail account connects, restores after restart, and disconnects.

## 3. Gmail messages

- List `INBOX` IDs using epoch boundaries; enforce the exact UTC window locally.
- Fetch bounded metadata concurrently and map it to `NormalizedMessage`.
- Follow page tokens, handle retries/cancellation, and build account-safe links.

Exit: today's metadata reaches SQLite and the existing shortlist without body fetches.

## 4. Shortlisted bodies

- Fetch full payloads only for shortlisted IDs.
- Decode MIME and charsets; prefer plain text and sanitize HTML-only messages.
- Never download file attachments or persist full bodies.

Exit: body fixtures and data-minimization integration tests pass.

## 5. Finish the product

- Complete AI analysis, digest assembly, UI wiring, consent, and packaging.
- Run clean-machine and live-mailbox checks before releasing 0.1.

Exit: connect -> sync -> rank -> analyze -> display -> open source works end to end.
