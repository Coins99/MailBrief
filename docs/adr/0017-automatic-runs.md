# ADR 0017: Automatic runs while MailBrief is open

Date: 2026-09-30. Status: accepted.

Builds on ADR 0005 (SQLite local storage), ADR 0010 (Groq analysis), ADR 0014 (owner
preferences) and ADR 0016 (follow-up proposals).

## Context

Until now a brief exists only when the owner presses Sync and review. M8 should let
MailBrief keep itself fresh while it is open: check Gmail, follow the threads of open
actions and say how much mail is waiting. Sending mail to an AI provider is different. ADR
0010 requires the owner's consent and a preview before anything is sent, and an unattended
run has nobody to ask. The feature must therefore be useful without sending anything, and
sending without asking must be a separate, explicit, bounded grant that the owner can end
at any moment.

## Decision

- **When.**
  - On launch (optional), and at a fixed interval (off, 60, 120 or 240 minutes), only while
    the desktop is open. Nothing runs when it is closed: no OS scheduler, tray icon or
    background process, and no timer outlives the window.
  - After sleep, or any time the app was not running, however many intervals were missed,
    there is exactly one run. Missed runs are never replayed.
  - A run is skipped while another operation runs, or while one of the window's dialogs
    (settings, an editor, saved briefs and so on) is open, and tried again a minute later.
  - A run is skipped while Gmail is disconnected, with a message on the status line.
  - A run covers today only. Past days are never briefed automatically; catching up stays
    the owner's explicit choice (Briefs… in the desktop, or `brief --date`).
  - The CLI runs one automatic run on request (`brief --automatic`) and has no scheduler.
    `--automatic` implies `--silent-only`: an automatic run never opens a browser, and with
    an expired session it fails with the usual sign-in message.
- **What.** An automatic run uses the automatic selection with no review: the top messages
  by rank, as many as the owner's messages-per-brief limit allows, never from an excluded
  sender and never one the owner declined (below). It syncs metadata, checks the threads of
  open actions and ranks, exactly like the first half of Sync and review.
- **Sending.**
  - **Without permission** (the default) an automatic run downloads no bodies, sends
    nothing to any provider and saves no brief. It reports how many messages are ready to
    review.
  - **Permission** is an explicit grant, separate from consent itself, recorded on the
    active consent: the provider, the account and the disclosure version. It is a number of
    messages from 1 to 10, or 0 for none.
  - The permission belongs to the **connected account**, the one a run reads its consent
    for, and at most one account holds one. Granting it needs that account's active consent
    and clears every other account's permission. The desktop takes the account from the
    Gmail connection, and with none connected the permission can't be changed there; the
    CLI reads it from the stored Gmail credential, without connecting.
  - **Off is off for every account:** setting 0 clears every account's permission and needs
    neither a connection nor a consent.
  - With permission, an automatic run may send up to that many messages without asking, and
    never more than messages per brief. Messages already analyzed and cached cost nothing,
    so only messages that would be sent count toward the cap.
  - Over the cap, the lowest-ranked messages are **deferred**: never sent and never cached,
    counted in the brief's coverage, and left for the next review. The owner's review later
    lists them like any other message.
  - Carried messages (Consequences, below) whose cached analysis no longer matches take the
    places first, in rank order, ahead of new messages. An automatic run never saves a brief
    that would lose a message carried from the day's earlier brief. When the cap can't cover
    them, or a carried message's body can't be read, it sends nothing and saves nothing; when
    a carried message's analysis fails, it writes no brief and leaves the analyses that
    succeeded cached for the review. Either way it reports that today's brief needs the
    owner's review. A manual run can still leave out a carried message whose refresh fails;
    the coverage counts it as failed.
  - Revoking consent ends the permission at once, and a new disclosure version starts
    without it, because it belongs to one version of one consent. Turning it off takes
    effect at once: every run reads it from the active consent.
  - Granting it needs an active consent, so the owner has seen the disclosure and analyzed
    once with Sync and review. The dialog and `ai-consent auto-send` repeat the disclosure
    and say that automatic runs may send that many messages without asking.
- **Declines.** When the owner unchecks a message the automatic selection picked (in the
  desktop review, or with `--exclude`), that is remembered on the cached message.
  - The automatic selection then skips it, in automatic runs and as a review's default,
    until the owner selects it again (checking it in a review, or `--include`).
  - An uncheck is remembered as soon as the review is confirmed, even if the owner then
    cancels or declines consent and nothing is sent: a decline can only make automatic runs
    send less.
  - Manual reviews still list it, unchecked and marked "you left this out earlier".
  - A declined reply from outside today's Inbox (ADR 0016) fills one of the three places for
    such replies only when no undeclined one wants it, so declined replies never starve new
    ones.
- **No interruptions.** No review panel, consent panel or modal dialog ever opens for an
  automatic run, and a run never opens, raises or focuses anything. Its results go to the
  status line. A newly saved brief replaces the latest one shown, unless the owner is
  reading a past brief.
- **Diagnostics.** Each automatic run writes one counts-only line to `desktop.log`: its
  outcome, how many new messages were ready, how many carried messages it couldn't refresh,
  how many it analyzed and deferred, and how many AI requests it made. Never mail text.

## Consequences

- Migration 0012 adds `owner_preferences.refresh_on_launch` and `refresh_interval_minutes`,
  `ai_consents.auto_send_limit` and `auto_send_granted_at_utc`, `digests.deferred_count` and
  `messages.review_declined_at_utc`. Downgrading drops them and keeps every row.
- Runs through a day are cumulative (added in Part 9): a day keeps one brief, and every run
  of that day, automatic or not, carries the saved brief's messages forward ahead of the
  automatic selection, which fills the free slots within messages per brief. A carried
  message drops out only when it is no longer cached, was an Inbox message that has been
  archived, is now blocked or was declined; one the brief listed as a reply from outside
  the Inbox keeps that status. So an automatic run never removes a message the owner chose
  earlier in the day.
- A run that only checks Gmail still writes cached metadata and ranks, and follows tracked
  threads; it sends nothing and changes no action.
- "Ready to review" counts only new messages: the automatic selection minus the ones the
  day's saved brief already carries (Part 9). With none, an automatic run says "Nothing new
  to review" and, even with permission, reads and saves nothing.
- Wall-clock time drives the schedule, so a computer that sleeps runs once on waking rather
  than catching up.
