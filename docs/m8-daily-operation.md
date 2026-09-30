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
| 4. Thread tracking backend | Metadata of later messages in open actions' threads, including the owner's replies | 0010, ADR 0015 | Done |
| 5. Thread tracking in the desktop | Tracked threads in the window; "Add to existing action" in the brief | None | Done |
| 6. Follow-up proposals backend | A new deadline, a cancellation or a delivery from later replies, applied only by the owner | 0011, analysis schema 7, ADR 0016 | Planned |
| 7. Follow-up proposals in the desktop; earlier tracked replies offered in review | Reviewing and applying proposals | None expected | Planned |
| 8. Daily operation | Refresh on launch and while running; automatic analysis only by explicit opt-in; missed runs coalesce | 0012, ADR 0017 | Planned |
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

## Thread tracking (Part 4)

Part 4 is implemented and awaits live acceptance. The policy is
[ADR 0015](adr/0015-thread-continuity.md); the desktop shows it from Part 5.

### What it does

- After today's sync or brief, MailBrief reads the Gmail threads of your open actions, both
  yours and those waiting for someone, and caches the messages that arrived after each
  action's email. `actions list` then shows how many are new, the latest one and whether
  you replied.
- **Bounds:** at most 25 threads per run, most urgent action first; at most the newest 20
  messages per thread, and only those after the action's latest email in that thread.
  Completed and deleted actions stop being tracked. Past-day briefs and anything scheduled
  don't check threads.
- **Nothing changes by itself:** a reply, from someone else or from you, never completes or
  changes an action, and thread checks use no AI. The only change is yours: marking the
  activity seen.
- A failed or stopped check (a sign-in, permission or rate-limit problem) never fails the
  sync or the brief; it is counted and reported, and what was already saved stays.

### Privacy

- Metadata only: the same four headers (From, To, Subject, Message-ID), labels, received
  time and Gmail's preview snippet as the Inbox sync. Never bodies or attachments.
- Drafts, trash and spam in a thread are ignored.
- Your own messages are cached only inside tracked threads. The Sent folder, and every
  other folder or label, is never listed.
- Cached sent messages never reach a brief: the shortlist reads Inbox messages only, as
  before.
- Sender exclusions keep governing AI only; metadata from excluded senders is cached like
  any Inbox message.

### CLI

- `sync` and `brief` print `Tracked threads: C checked, F failed, S messages saved` when any
  threads were tracked, plus the reason if the check stopped.
- `actions list` adds `; thread: N new, latest <time> from <sender>` and
  `; you replied <date>`, in your time zone.
- `actions seen PUBLIC_ID [--database PATH]` marks them seen ("Marked seen." or "Nothing
  new in its threads."); an unknown ID exits 3. It is offline.

### Limits

- Sources accepted before migration 0010 whose email had already left local mail aren't
  tracked.
- The desktop display, and adding an email to an existing action, are Part 5 (below).
- A message that arrives later but carries an earlier received time than your "seen" mark
  isn't counted as new (see Part 5's limits).

### Live acceptance

1. Accept an action from an email, reply to that email from another account, then run
   `brief`: `actions list` shows "1 new".
2. Reply yourself from Gmail: "you replied …" appears, and `actions seen <id>` clears both.
3. Archive the other account's reply: it's still counted, because tracking isn't
   Inbox-bound.
4. Save a Gmail draft in the thread: it isn't counted.
5. Complete the action: its thread is no longer checked (the tracked count drops).
6. Search the database: none of the replies' text is stored beyond Gmail's preview
   snippet.

### For developers

- `ports/threads.py`: `ThreadReader.fetch_thread(thread_id)`. `GmailProvider` implements it
  with `threads.get`, `format=metadata` and a field mask; `providers/microsoft/` has none.
- `services/threads.py`: `ThreadService.tracked()` and `check()`, with
  `MAX_TRACKED_THREADS`, `MAX_THREAD_MESSAGES` and `THREAD_CONCURRENCY`. It commits per
  thread and never writes to actions.
- `ApplicationService(threads=...)` runs the check after a complete or partial sync of
  today's window and adds its counts to `SyncResult`.
- Migration 0010 adds `messages.is_sent`, the `(account_id, conversation_id)` index, source
  snapshots (`provider`, `provider_account_id`, `provider_thread_id`) and
  `actions.thread_seen_until_utc`.
- `ActionRepository.load()` derives `Action.thread` with a fixed number of queries;
  `ActionService.mark_thread_seen()` is the only change thread activity allows.

## Thread activity and adding to an action (Part 5)

Part 5 is implemented and awaits live acceptance. It needs no migration. The policy is
[ADR 0015](adr/0015-thread-continuity.md#adding-to-an-existing-action).

### What it does

- **Continuations in the brief.** Under an email, "Continues: “<title>” (mine)" or
  "(waiting for)" names each live, open action with a source in the same Gmail thread of
  the same account: at most 3, most urgent first. An action the email already belongs to
  isn't named.
- **Add to an existing action.** After **Accept** and **Dismiss**, a pending suggestion
  offers **Add to “<title>”** for each of those actions, including one the email already
  belongs to. It accepts the suggestion into that action instead of creating another: the
  email becomes one more source unless it already is one, the suggestion shows as
  accepted, and the action gets one new revision. Its title, plan, dates and notes don't
  change.
- **Thread activity in your actions.** A row adds " · 2 new in thread, latest Tue 14:02 from
  Sam" and " · you replied Wed", in your time zone. For a day more than a week back, the
  date replaces the weekday.
- **Mark seen** clears both. It is enabled only when the selected action
  has something new, and makes one revision. **Open source** still opens the thread in
  Gmail.
- **The status line** after Sync and review, or a brief from Briefs…, adds one sentence when
  threads were tracked: "Checked 3 tracked threads.", "Checked 3 of 5 tracked threads;
  2 failed." or "Thread checks stopped early." It never shows an error code, and says
  nothing when no threads were tracked or the run was cancelled.
- Nothing here is automatic or uses AI: only you add an email to an action or mark
  activity seen.

### Undo

- **Undo add** reverses an addition while the action is exactly as the addition left it.
  The suggestion is pending again, and the email stops being a source if the addition made
  it one. An action's last source always stays.
- Any later change to the action (an edit, completing it, marking it seen, another
  addition) makes Undo refuse with "Can't undo: it has changed since then." As before,
  Undo covers only the latest change and is withdrawn by an edit or a new brief.
- If the action changed or was deleted before **Add to** runs, the window says so and
  reloads. A suggestion already accepted into another action shows "That suggestion already
  belongs to another action."

### CLI

- `actions accept N --into PUBLIC_ID [--database PATH]` accepts suggestion N into that
  action and prints "Added to: <title> (<id>)". An unknown action or suggestion exits 3; a
  suggestion accepted into another action exits 3 with "That suggestion already belongs to
  another action." Only an open action takes an email: a completed one exits 3 with "Reopen
  the action before adding to it." When you name the action, any thread is allowed; the
  brief offers only those that continue the email's thread.
- `brief --show` and `briefs show` print "Continues: <title> (<id>)" under an item for each
  action it continues, so you can pass that ID to `--into`.

### Limits

- The "seen" mark is a received time. A message cached late whose received time is before
  the mark isn't counted as new. That takes a missed thread check (a failed check, or a
  thread with more than 20 later messages), and it only hides the "new" count: the message
  is still cached and visible in Gmail.
- At most 3 actions are named, and offered, per email.
- Continuations are read when a brief is shown, so a past brief names the actions open now.
- An action's source accepted before migration 0010 whose email had already left local mail
  has no thread snapshot, so it isn't matched.

### Live acceptance

1. Accept an action from an email, reply to it from another account, then Sync and review:
   the reply's card says "Continues: …", and the action row shows "1 new in thread".
2. On the reply's suggestion, click Add to “…”: no new action appears, the action has two
   sources, and its "new" count clears.
3. Undo add: the suggestion is pending again, and the action is back to one source.
4. Reply yourself from Gmail and sync: "you replied …" appears, and Mark seen clears it.
5. `actions accept N --into <id>` does the same from the CLI.

### For developers

- `ActionService.thread_links(account_email, message_keys)` returns `ThreadLink`s
  (`public_id`, `title`, `revision`, `ownership`, `is_source`), at most `MAX_THREAD_LINKS`
  per message, ordered by `urgency()`. `ActionRepository.thread_link_rows()` reads them with
  one query per 100 keys (a brief has at most 10), matching sources on provider, account and
  thread snapshot; `is_source` is an `EXISTS` on the action's sources.
- `ActionService.accept_into()` returns `AcceptedInto(action, source_added)`.
  `undo_accept_into(suggestion_id, public_id, expected_revision, remove_source)` deletes the
  decision and, when asked, the source (`ActionRepository.remove_source()`), but never the
  last one.
- Desktop: `DesktopBackend` gains `brief_links`, `accept_into`, `undo_accept_into` and
  `mark_thread_seen`. `MainWindow._show_digest()` shows every brief, with its links when
  they can be read. `DigestView.accept_into_requested(suggestion_id, public_id, revision)`
  comes from internal `mailbrief:into/<n>` links; `thread_check_text()` builds the status
  sentence; `ActionsPanel.seen_button` emits `SEEN`.
