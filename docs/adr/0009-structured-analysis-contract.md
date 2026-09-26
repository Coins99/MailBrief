# ADR 0009: Structured analysis contract

- Status: Accepted
- Date: 2026-09-26
- Builds on: ADR 0004

## Context

M4 sends minimized email to OpenAI and saves the answers as a daily brief. Model output is
untrusted, deadlines are easy to get wrong, and email must go only where the owner agreed.

## Decision

- The provider extracts facts as written: the deadline phrase, date, time and zone. It
  never computes instants.
- Python checks that the deadline phrase and the evidence quote appear in the email, then
  resolves instants with `zoneinfo`.
- A date-only deadline stays a date; the brief treats it as due at the end of that day.
- Abbreviated or offset zones (EST, PT, UTC+2, Etc/GMT+5) are never resolved; only UTC and
  IANA region zones are.
- One card per message until M6 introduces editable plans.
- The cache identity is six columns: message, input hash, provider, model, prompt version
  and schema version. The hash excludes the per-run message key and includes the time zone.
- Consent is stored in SQLite per account, provider and disclosure version.
- SDK retries are off; the app's classifier retries at most three times and waits at most
  30 seconds.
- Requests set `store=false`, and the client pins `https://api.openai.com/v1` so neither
  `OPENAI_BASE_URL` nor proxy variables can redirect email content.

## Consequences

- A deadline the model cannot support from the email is rejected or downgraded, never shown
  as exact.
- A new model, prompt or schema re-analyzes each message once; unchanged input costs nothing.
- Revoking consent, or changing the disclosure version, makes the next run ask again.
