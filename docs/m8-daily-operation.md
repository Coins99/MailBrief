# M8 thread continuity and daily operation

M8 turns the daily brief into something to rely on every day: the owner's preferences, a
history of saved briefs, follow-up on open actions as their threads continue, and refresh
while MailBrief runs. The milestone's scope is in
[email-implementation-plan.md](email-implementation-plan.md#m8--thread-continuity-and-daily-operation).
M8 is one pull request ([#17](https://github.com/Coins99/MailBrief/pull/17), branch
`feat/m8-continuity`), built in nine parts.

| Part | What it adds | Schema | Status |
| --- | --- | --- | --- |
| 1. Preferences core | Time zone, messages per brief, sender exclusions, drafting defaults, AI limits in SQLite; CLI | 0009, ADR 0014 | Done |
| 2. Preferences in the desktop | The Settings Preferences tab; the owner's zone and drafting defaults in the window | None | Done |
| 3. Brief history and bounded catch-up | Browse saved briefs; brief one missed day (within the last 7) per explicit run | None | In progress |
| 4. Thread tracking backend | Metadata of later messages in open actions' threads, including the owner's replies | 0010, ADR 0015 | Planned |
| 5. Thread tracking in the desktop | Tracked threads in the window; "Add to existing action" in the brief | None expected | Planned |
| 6. Follow-up proposals backend | A new deadline, a cancellation or a delivery from later replies, applied only by the owner | 0011, analysis schema 7 | Planned |
| 7. Follow-up proposals in the desktop | Reviewing and applying proposals | None expected | Planned |
| 8. Daily operation | Refresh on launch and while running; automatic analysis only by explicit opt-in; missed runs coalesce | 0012, ADR 0016 | Planned |
| 9. Closeout | Acceptance, documentation and cleanup | None expected | Planned |

The optional Gmail history cursor for incremental Inbox reconciliation is not part of this
pull request.

## Preferences (Parts 1–2)

Parts 1 and 2 are implemented and await live acceptance. The policy is
[ADR 0014](adr/0014-owner-preferences.md).

### What it does

Your preferences are saved in the MailBrief database, so the desktop app and the
diagnostic CLI use the same ones, and a backup of the database includes them:

- **Time zone:** when your day starts and ends, and the zone for "carried over", overdue
  and today's date in AI drafting. UTC or a region such as `America/Toronto`; abbreviations
  (`EST`) and fixed offsets (`Etc/GMT+5`) are refused. Unset means the system time zone.
- **Messages per brief** (1 to 10, default 10): the most messages the automatic selection
  picks and the most you can select in review.
- **Excluded senders:** an exact address, or `@domain`, which also covers its subdomains
  (`@example.com` covers `news.example.com` but not `notexample.com`). A message from an
  excluded sender is never selected automatically, can't be included in review or with
  `--include`, never has its body downloaded and is never offered to AI drafting. It still
  appears in review, marked as excluded. Briefs saved before a rule keep their analyses;
  new briefs leave the message out.
- **Drafting defaults:** the tone and length Write with AI starts with.
- **AI limits:** body characters sent, output tokens, requests per run, messages per AI
  request and the timeout, within the ranges in [ai-analysis.md](ai-analysis.md).

Device settings stay where they were: the OAuth client file and Groq model in
`desktop-settings.json`, and secrets in the OS credential store.

### Desktop

**Settings** has two tabs. **Connection and AI** is the earlier dialog. **Preferences**
holds the choices above: the time zone list starts with "System time zone (…)", excluded
senders take one address or `@domain` per line, and each AI limit's lowest setting reads
"Default (…)". **Save preferences** saves them. **Reset to defaults** restores every
default, whatever is saved, and also repairs preferences that can't be read. If another
save happened since Settings opened, the save is refused with "Preferences changed since
they were loaded; reopen Settings."

The window applies your time zone and drafting defaults at startup and after each save or
reset, and redraws the brief, actions and drafts. Changing the time zone changes where
today starts: today's messages are analyzed again once, because the analysis includes the
zone, and the change can start a new day's brief.

If the saved preferences can't be read, the window says so, and briefs and AI drafting
refuse to run until you reset them in Settings > Preferences. Running with defaults
instead would drop your exclusions. Views that only display local data, such as the
actions lists and offline browsing, use the system time zone meanwhile.

### CLI

- `mailbrief-gmail-diagnostic preferences show [--database PATH]` prints the time zone,
  messages per brief, excluded senders one per line, drafting defaults, and each AI limit
  with where its value comes from (environment, saved or default). It uses no network and
  never creates a missing database.
- `sync`, `bodies`, `brief`, `actions list` and `drafts generate` read your preferences
  before building any provider. A database that doesn't exist yet is still not created
  before Gmail sign-in succeeds.
- `sync --show-metadata` marks messages from excluded senders `excluded`.
- `brief --include <ID>` for an excluded sender exits 3 without downloading anything.
- Unreadable preferences exit 3 wherever data could be sent; `actions list` only displays,
  so it says so and uses the system time zone.

### Precedence

- AI limits: an explicit `MAILBRIEF_AI_*` variable, then the saved preference, then the
  built-in default. A variable set to the default's value still counts as set.
- `--timezone`, `--tone` and `--length`: the flag, then the saved preference, then the
  default (the system time zone, neutral, medium).

### Limits

At most 200 excluded senders of at most 320 characters each; a time zone name of at most
64 characters; messages per brief 1 to 10. The AI limits keep the ranges of their
`MAILBRIEF_AI_*` variables.

### Live acceptance

1. Add a rule for a sender in today's Inbox: review shows the row excluded and it can't be
   checked; the brief has no item from it.
2. `brief --include <that ID>` exits 3; `preferences show` lists the rule.
3. Set messages per brief to 3: the automatic selection has at most 3 messages, and review
   refuses a fourth.
4. Choose a time zone where today's date differs from the system's: the brief's date,
   "carried over" and overdue labels, and drafting's "today" follow it, and `brief`
   without `--timezone` uses it.
5. A saved AI limit applies; setting the same `MAILBRIEF_AI_*` variable wins in the CLI.
6. Write with AI opens on the saved tone and length; a reply to an excluded sender doesn't
   offer the email.
7. After a restart everything persists, and the packaged app lists time zones.

### For developers

- `domain/preferences.py`: the model, bounds, `is_region_zone`, `normalize_exclusion` and
  `sender_excluded`. `storage/preferences.py` and `OwnerPreferencesTable`: one row with
  `id = 1`, changed only through ORM objects; migration `20260929_0009`.
- `services/preferences.py`: `PreferencesService` (`get`, `save` with the expected
  revision, `reset`), `effective_settings`, `ai_limits` and `owner_zone`. Only
  `AI_LIMIT_FIELDS` are taken from preferences, and only when absent from
  `Settings.model_fields_set`: `DesktopPreferences.settings()` passes the OAuth path and
  model by name, so their presence there says nothing.
- Exclusions are enforced by services: `review_shortlist` and
  `ApplicationService.prepare_daily_shortlist` raise `ExcludedSenderError` before any body
  is read, and `DraftingService` withholds the email. `ShortlistGate.review` takes
  keyword-only `blocked_ids` and `limit`. Per-run `exclude_ids` are not backfilled; blocked
  messages never take a shortlist slot.
- Sender rules are the owner's text: `preferences show` and Settings display them, but
  logs carry only their count.
