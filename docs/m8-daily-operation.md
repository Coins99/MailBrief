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
| 3. Brief history and bounded catch-up | Browse saved briefs; brief one missed day (within the last 7) per explicit run | None | Done |
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

## Brief history and catch-up (Part 3)

Part 3 is implemented and awaits live acceptance. It needs no migration.

### What it does

- **Saved briefs by day.** Every saved brief can be opened again, newest day first. The
  brief shown at startup is the one for the newest day, not the newest save, so catching
  up on yesterday never hides today's brief.
- **Bounded catch-up.** A day with no brief within the last 7 days is a missed day. You can
  brief one missed day at a time, and briefing a day that already has a brief replaces it
  after you confirm. Nothing briefs a past day by itself.
- **What each brief covers.** Every brief says so under its heading, in its own zone:
  - made on its own day: "Covers messages received on 2026-09-29 up to 09:14
    (America/Toronto) that were in your Inbox then.";
  - made later: "Covers messages received on 2026-09-28 (America/Toronto) that were still
    in your Inbox on 2026-09-29 at 10:02."
- A past day's brief uses the same review, consent, messages per brief and sender rules as
  today's. Its messages are ranked with the real current time.

### Desktop

- **Briefs…** opens a dialog with **Saved briefs** (date · status · items · account;
  Return or a double-click opens one) and **Missed days** for the connected account. Without
  a connection it reads "Connect Gmail to brief missed days."
- **Open** shows a saved brief. Unless it is the latest, a banner reads "Viewing the brief
  for <date>." with **Back to latest**. Accept, Dismiss and Undo keep you on that brief.
- **Brief this day…** is offered for a missed day, or for a saved brief within the last 7
  days other than today's; it briefs the connected account, so another account's brief
  can't be replaced from here. For a saved day it asks first: "This replaces the saved
  brief for <date>." It needs a connected Gmail account, like **Sync and review**; offline
  it says so.
- **Sync and review** always briefs today and returns to the latest brief.

### CLI

- `brief --date YYYY-MM-DD` briefs one of the previous 7 days. An unreadable date, or one
  outside today and the previous 7 days, exits 3 before Gmail is contacted. With `--show`,
  the brief's coverage line comes before its items.
- `briefs list [--database PATH] [--limit N] [--timezone ZONE]` lists saved briefs, newest
  first (default 30), each with its coverage line, then each Gmail account's missed days. It
  is offline and never creates a missing database.
- `briefs show DATE [--account EMAIL] [--database PATH]` prints one saved brief as
  `brief --show` does, with its coverage line. No brief exits 3 with "No saved brief for
  that date."; briefs from several accounts exit 3 until you pass `--account`.
- "Today" is in the `--timezone` zone, else your saved time zone, else the system's.

### Limits

- At most 7 days back, one day per run, and never automatic.
- A past day covers only the messages still in the Inbox when its brief is made; messages
  archived or deleted since are not in it.
- Only today's sync moves the account's last complete sync time. A past day's sync still
  records which of that day's cached messages are still in the Inbox.

### Live acceptance

1. Brief today, then brief yesterday from Briefs…: after a restart, today's brief shows,
   not yesterday's.
2. Yesterday's brief says it covers the messages still in the Inbox when it was made.
3. Archive one of yesterday's messages in Gmail and brief yesterday again: the replace
   confirmation appears first, and the archived message is gone.
4. Accept a suggestion while viewing a past brief, then Undo: the view stays on that brief.
5. `briefs list` shows the same briefs and missed days; `brief --date` for 8 days back exits
   3.
6. Offline: Briefs… still opens saved briefs, and "Brief this day…" says a connection is
   needed.

### For developers

- `services/calendar.py`: `day_window(local_date, zone)` gives any local day's UTC bounds;
  `local_day_window()` delegates to it.
- `services/history.py`: `CATCH_UP_DAYS`, `check_brief_date` (raises `BriefDateError` with a
  static message), `catch_up_days`, `coverage_line` and `BriefHistory` (`list_saved`, `get`,
  `accounts_for`, `missed_days`). Any saved brief, even an empty or partial one, counts as
  saved.
- `DigestRepository.get_latest()` orders by local date, then save time; `list_summaries()`
  counts items in one grouped query.
- `BriefService.generate(local_date=...)` checks the date before contacting Gmail;
  `ApplicationService.prepare_daily_shortlist(local_date=...)` syncs that day's window and
  passes `record_last_sync` to `SyncService.sync_day`, true only for the window containing
  now.
- The desktop window remembers the connected account and the brief shown (`None` for the
  latest); `_reload_brief()` reloads the shown brief.
