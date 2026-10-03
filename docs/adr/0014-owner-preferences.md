# ADR 0014: Owner preferences in SQLite

Date: 2026-09-29. Status: accepted.

Builds on ADR 0005 (SQLite local storage), ADR 0010 (Groq) and ADR 0013 (AI drafting).

## Context

M8 needs choices that change what a brief contains and when "today" starts: the owner's
time zone, how many messages a brief may analyze, senders whose mail must never be sent to
an AI provider, drafting defaults and AI limits. The desktop and the diagnostic CLI must
agree on them, and M9 backups must include them. Until now, the only saved choices were
device settings in `desktop-settings.json` (the OAuth client path and the Groq model) and
`MAILBRIEF_*` environment variables.

## Decision

- **One revisioned SQLite row.** Owner preferences are a single row in `owner_preferences`,
  shared by the desktop and the CLI, so both agree on "today" and a database backup
  includes them. Every save needs the revision the caller saw; a stale save is refused.
  Device settings stay in `desktop-settings.json`, and secrets stay in the OS vault.
- **What is saved:** the time zone, messages per brief, sender exclusions, the default
  drafting tone and length, and five AI limits (batch size, body characters, output tokens,
  requests per run, timeout).
- **Precedence.** For AI limits, an explicit `MAILBRIEF_*` variable wins over a saved
  preference, which wins over the built-in default. For the CLI's `--timezone`, `--tone`
  and `--length`, the flag wins over the saved preference, which wins over the default.
- **Time zone:** UTC or an IANA region zone such as `America/Toronto`. Abbreviations such as
  `EST` and fixed offsets such as `Etc/GMT+5` are refused. Unset means the system time zone.
- **Sender exclusions are a privacy rule, enforced by services, not the UI.** A rule is an
  exact address or `@domain`, which also covers its subdomains. A message from an excluded
  sender is never selected automatically, can't be included (in review or with
  `--include`), never has its body downloaded and is never offered to AI drafting. Cached
  analyses stay, but blocked messages leave new briefs.
- **Messages per brief** (1 to 10) caps both the automatic selection and the reviewed one.
- **Unreadable preferences fail closed.** Brief generation and AI drafting refuse to run
  rather than fall back to defaults, which would drop the owner's exclusions. Views that
  only display local data fall back to the system time zone.

## Consequences

- Migration 0009 adds `owner_preferences`; a fresh database has no row, which means the
  defaults (system time zone, ten messages, no rules and the built-in AI limits).
- Changing the time zone changes where today starts. Today's shortlist is analyzed again
  once, because the analysis input includes the zone, and the change can start a new day's
  brief.
- Downgrading below 0009 drops the table and the preferences in it.
