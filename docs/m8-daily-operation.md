# M8 thread continuity and daily operation

M8 turns the daily brief into something to rely on every day: the owner's preferences, a
history of saved briefs, follow-up on open actions as their threads continue, and refresh
while MailBrief runs. The milestone's scope is in
[email-implementation-plan.md](email-implementation-plan.md#m8--thread-continuity-and-daily-operation).
It ships in stages, one pull request each.

| Stage | What it adds | Migration | Status |
| --- | --- | --- | --- |
| M8.1 Owner preferences | Time zone, messages per brief, sender exclusions, drafting defaults, AI limits | 0009 | In progress |
| M8.2 Brief history and bounded catch-up | Browse saved days; brief one missed day (within the last 7) per explicit run | None expected | Planned |
| M8.3 Thread tracking for open actions | Metadata of later messages in their threads, including the owner's replies; "Add to existing action" in the brief | 0010 | Planned |
| M8.4 Follow-up proposals from later replies | A new deadline, a cancellation or a delivery, applied only by the owner | 0011, analysis schema 7 | Planned |
| M8.5 Refresh on launch and while running | Automatic analysis only by explicit opt-in; missed runs coalesce | 0012 | Planned |
| M8.6 Optional Gmail history cursor | Incremental reconciliation of the Inbox | 0013 | Planned |

## M8.1 Preferences
