# ADR 0016: Follow-up proposals from later emails

Date: 2026-09-30. Status: accepted.

Builds on ADR 0010 (Groq analysis), ADR 0011 (actions and suggestions) and ADR 0015
(thread continuity).

## Context

A conversation often continues after the owner accepts an action from it: the other person
moves the deadline, withdraws the request, or sends what was asked for. ADR 0015 shows that
a thread moved on, but the owner still has to read the reply and update the action by hand.
M8 should offer the update without letting an email change the owner's work, and without
sending anything about the owner's actions to the AI.

## Decision

- **Signal, from the email alone.** Each analyzed email gets `follow_up`: `none`,
  `new_deadline`, `cancelled` or `delivered`, judged from that email only, with a quote of at
  most 160 characters that must appear in the email.
  - The quote is checked before the suggestions and counts toward the email's
    600-character evidence budget. A signal whose quote is missing, too long, not in the
    email or over budget becomes `none`; the message never fails because of it.
  - `new_deadline` needs a deadline the email states, resolved in Python like any other
    deadline; without one the signal becomes `none`.
  - The request sends the same seven fields as before, so the consent disclosure stays at
    version 2. Schema version 7 and a new prompt version re-analyze cached emails once.
- **Proposals.** After a brief is saved, a signal becomes a pending proposal for each
  action it may update:
  - only live, open actions with a source in the email's thread in the same account, where
    the latest such source is older than the email, and the email isn't already a source;
  - only when applying would change something (a new deadline equal to the current one
    proposes nothing and uses no slot), most urgent first, and for at most three actions
    per email ever: the actions that already have a proposal from the email, in any state
    and even if since closed, use up its slots, so applying one never frees a place;
  - never from the owner's own messages (sent, or from one of the account's addresses);
  - after any saved brief, a past day's included;
  - one proposal per action, email and kind. A newer analysis of the same email replaces a
    still-pending proposal of another kind for that action; applied and dismissed ones are
    kept;
  - proposals keep a snapshot of the email (provider, account, message and thread IDs,
    subject, sender, link, received time), like decisions and sources, so they survive the
    email leaving local mail.
- **Applying is the owner's choice** and makes one action revision:
  - `new_deadline` sets the deadline and the suggested target date. It moves the target
    date only if the owner never changed it, that is, when the target equals the old
    suggested target;
  - `cancelled` and `delivered` complete the action;
  - the email becomes a source of the action;
  - title, notes, steps and ownership are never touched;
  - Undo restores the previous deadline, targets and status while the action is unchanged
    since the apply, and removes the source the apply added unless it is the last one;
  - a dismissed proposal never comes back on its own; the owner can restore it.
- **A reply is never proof.** `cancelled` and `delivered` only offer to complete the action;
  nothing completes or changes it without the owner.
- **Ranking.** A message in a tracked thread (ADR 0015) that is newer than that thread's
  sources and wasn't sent by the owner gets +20, "reply in a thread you track", so it is
  usually analyzed.

## Replies outside today's Inbox

A reply that proposes an update is often archived at once, or arrived on an earlier day, so
today's Inbox sync never lists it and it could not be analyzed. The cache that thread
tracking (ADR 0015) fills already holds it, so the run may offer it:

- **Which and how many.** Cached messages in tracked threads that are newer than the
  thread's baseline, not the owner's own, not yet analyzed at the current schema and not
  both received in the day's window and in the Inbox. At most three per run, newest first,
  and only in today's run, never a past day's. Only threads the same run's check read
  successfully are drawn on (ADR 0015), since only reading a thread reveals a reply since
  trashed or marked spam.
- **Same gates as any message.** They are ranked with the tracked-thread bonus and compete
  for the automatic selection, the review, the consent preview and the messages-per-brief
  limit; nothing is sent that the owner didn't approve, and the seven sent fields are
  unchanged.
- **Never excluded senders.** A message from an excluded sender is dropped before it is
  counted, and again by the application, like any message.
- **Labelled.** In the review ("reply in a tracked thread, not in today's Inbox"), in the
  brief's own last section "Replies in threads you track", and in the coverage line.
- Nothing is listed or read from Gmail to find them: only the cache is queried.

## Consequences

- Migration 0011 adds `analyses.follow_up_kind` (default `none`) and `follow_up_evidence`,
  and the `action_proposals` table. Downgrading drops them and keeps every other row.
- The first brief after upgrading sends today's shortlist to Groq again once, under the
  existing consent.
- A follow-up signal can come from any analyzed email, but becomes a proposal only for an
  open action whose thread it continues; the brief's own suggestions are unchanged.
- The CLI lists, applies and dismisses proposals; the desktop shows them in the brief, in
  the actions pane and in a Proposals dialog, with Undo (Part 7).
- Part 7 needs no migration. The brief gains a section whose stored value is
  `follow_ups`; an older app reading such a brief would not recognize it.
