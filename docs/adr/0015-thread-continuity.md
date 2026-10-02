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
- **Trash and Spam are forgotten** (added in the M8 review fixes): a reply cached earlier
  and since moved to Trash or marked spam would otherwise look archived. When a thread check
  sees a message labelled TRASH or SPAM, its cached row in that account is deleted, with its
  analyses, suggestions and brief items; action sources, draft sources and proposals keep
  their snapshots. Taking it back out caches it again on the next check. Independently, a
  body labelled TRASH or SPAM is never read: the body download asks for the labels, treats
  such a message as unavailable, and so it is skipped and never sent.
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

## Adding to an existing action

Added with the desktop display (M8 Part 5).

- **Only the owner** adds an email to an existing action, by choosing **Add to** on one of
  its pending suggestions (or `actions accept N --into`). Nothing does it automatically.
- **What the brief offers:** only live, open actions with a source in the email's thread in
  the same account (the same provider, account and thread snapshot), at most three, most
  urgent first. Nothing is offered across accounts, or for completed or deleted actions.
- **Only open actions take an email.** `ActionService.accept_into` refuses a completed or
  deleted action ("Reopen the action before adding to it."), so the CLI and the desktop
  agree: adding to finished work would hide the suggestion in it. When the owner names the
  action (`actions accept N --into`), any thread or account is allowed, since a related
  email often starts a new thread.
- **One revision:** the suggestion is accepted into the action, the email becomes a source
  unless it already is one, and nothing else about the action changes.
- **Undo** works while the action is unchanged since the addition: the suggestion is pending
  again and the source the addition made is removed. It never removes an action's last
  source.

## Consequences

- Migration 0010 adds `messages.is_sent`, an index on each account's threads, the source
  snapshots and `actions.thread_seen_until_utc`. Downgrading drops them and keeps every row.
- Cached sent messages never reach a brief: the shortlist reads Inbox messages only, so a
  sent message counts only when Gmail also labels it INBOX in today's window, as before
  (mail to yourself, for example).
- Sources accepted before 0010 whose email had already left local mail aren't tracked.
- The desktop shows thread activity from Part 5; Part 4 shows it in the CLI.
- A cached reply the owner trashes is forgotten only when its thread is next checked: the
  thread must still be tracked and among the 25 read. Until then the body download's own
  refusal keeps it from being read or sent.
- "The owner's own message" has one definition, `is_own_message` in `domain/messages.py`:
  Gmail's SENT label, or a sender that is one of the account's addresses. Thread activity,
  ranking's tracked-thread bonus, outside replies and proposals all use it, so a reply sent
  from an alias without the SENT label counts as the owner's everywhere.
