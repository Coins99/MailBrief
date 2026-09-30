# ADR 0015: Tracking the threads of open actions

Date: 2026-09-30. Status: accepted.

Builds on ADR 0011 (actions and suggestions) and ADR 0014 (owner preferences).

## Context

An accepted action comes from an email, and the conversation often continues: the other
person answers, or the owner replies from Gmail. Those later messages may never be in a
brief (they can arrive on another day, be archived at once, or be the owner's own), so the
owner can't see that an action's thread moved on. M8 needs that signal without widening
what MailBrief reads, and without letting a reply change the owner's actions.

## Decision

- **What's tracked:** the threads of the sources of live, open actions, both the owner's
  and those waiting for someone, in the connected Gmail account. At most 25 threads per
  run, most urgent action first (the action lists' order). Threads are checked only during
  today's sync or brief; past-day runs and anything scheduled don't check threads.
- **How it's read:** Gmail `threads.get` with `format=metadata`, the same four headers
  (From, To, Subject, Message-ID), labels, received time and snippet as the Inbox sync.
  Never bodies or attachments. Messages labelled DRAFT, TRASH or SPAM are ignored.
- **What's stored:** only messages received after the action's latest source in that
  thread, the newest 20 per thread, as ordinary cached metadata: Inbox membership from their
  labels, and "sent" from the SENT label. The owner's own messages are cached only inside
  tracked threads; the Sent folder is never listed, and no label or folder is listed.
- **Snapshots:** action sources gain provider, account and thread snapshots, backfilled from
  cached messages. A source whose email had already left local mail stays untracked.
- **Activity is derived:** thread activity is computed from cached messages, never stored on
  the action. The only new action field is the owner's "seen" watermark, and moving it is a
  revisioned change like any other.
- **A reply is never proof of anything:** neither another person's reply nor the owner's
  completes or changes an action. Thread checks don't use AI.
- **Failures:** a failed or stopped thread check never fails the sync or the brief; it is
  counted and reported. Sign-in, permission and rate-limit errors stop the check and keep
  what was already stored.
- **Sender exclusions** keep governing AI only; metadata from excluded senders is cached
  like any Inbox message.
- **Microsoft** has no thread reader; the dormant adapter is untouched.

## Consequences

- Migration 0010 adds `messages.is_sent`, an index on each account's threads, the source
  snapshots and `actions.thread_seen_until_utc`. Downgrading drops them and keeps every row.
- Cached sent messages never reach a brief: the shortlist reads Inbox messages only, so a
  sent message counts only when Gmail also labels it INBOX in today's window, as before
  (mail to yourself, for example).
- Sources accepted before 0010 whose email had already left local mail aren't tracked.
- The desktop shows thread activity from Part 5; Part 4 shows it in the CLI.
