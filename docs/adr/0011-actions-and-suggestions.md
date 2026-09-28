# ADR 0011: AI suggestions and owner-accepted actions

Date: 2026-09-28. Status: accepted; live acceptance pending.

Extends ADR 0009's extraction contract (schema version 6) and ADR 0005's local storage.

## Context

M6 turns briefs into work the owner keeps: an email may ask for several things, the owner
decides which to take on, and accepted work must outlive the day's brief, later AI runs
and even the cached email. The AI remains untrusted: it may invent requests, dates or
text meant to mislead the owner's screen or terminal.

## Decision

- **Suggestions are AI output; actions are the owner's.** Each analysis may carry up to
  five suggestions: title, whose it is, effort, a deadline of its own, up to five plan
  steps and an evidence quote. Accepting copies a suggestion into an action with its own
  steps and a snapshot of each source email (subject, sender, link, received time).
  Nothing an AI run produces ever writes to an action.
- **Python, not the model, suggests target dates:** one working day (Monday to Friday)
  before a dated deadline, or the deadline itself when no earlier working day is left.
  Suggestions without a dated deadline get no target. There is no holiday calendar and no
  availability model, so MailBrief suggests dates rather than claiming free time.
- **Decisions are remembered per email and normalized title** (NFKC, case-folded,
  punctuation and symbols removed), not per suggestion row, because rows are replaced
  whenever an analysis is. A dismissed suggestion is never offered again for that email,
  and an accepted one never becomes a second action.
- **Evidence has a per-email budget.** Each suggestion quote is at most 160 characters,
  and all stored quotes for one email (its own and its suggestions') total at most 600
  characters and stay under 80% of the body, so quotes can never rebuild an email. A valid
  quote that does not fit is checked but not stored.
- **Text the AI writes itself** (summary, action text, suggestion titles and steps) and
  deadline phrases are stripped of control and format characters before storage, so model
  output steered by an email can neither drive a terminal nor disguise text on screen.
- **Actions belong to no account.** Source links use `ON DELETE SET NULL`; deleting a
  message, the cache or an account leaves the action with "source no longer in local
  mail". Deleting an action is a soft delete that restore brings back.
- **Every change is one revision.** Callers pass the revision they saw (restoring a deleted
  action needs none); a mismatch raises `ActionConflictError`, so a stale screen never
  overwrites a newer change. Saving an edit and its plan together is a single revision.
  Undo reverses only the latest accept, dismiss, complete, reopen or delete; edits have no
  undo.
- **Proposed revisions move to M8.** Gmail messages never change after arrival, so a real
  source change arrives as a new reply in the thread, which M8's thread continuity handles.

## Consequences

- Schema version 6 and a new prompt version make every cached analysis a miss once, so the
  first brief after upgrading re-sends today's shortlist.
- Migration 0005 adds five tables. The app from before M6 refuses a database upgraded to
  0005; downgrading below 0005 deletes every action and decision.
- Known limit: after a model or prompt change, a reworded suggestion (a different
  normalized title) may be offered again even though an earlier wording was dismissed.
  Reruns with the same model and prompt reuse the cache, so they cannot do this.
- Decisions and actions must be changed through ORM objects, never bulk statements:
  loaded rows would otherwise go stale within a session.

## Amendment (2026-09-28)

After the PR #15 review:

- **Decisions are keyed by provider account, Gmail message ID and title fingerprint**,
  with no foreign key to messages or accounts. They survive deleting a message, clearing
  the cache and deleting an account, so re-syncing the same email, or reconnecting the
  same Gmail account, finds them again: a dismissal stays dismissed, and an accepted
  suggestion still points at its action instead of becoming a second one. A decision from
  another account never matches, even for the same message ID. Migration 0006 rebuilds the
  table and copies every decision unchanged; downgrading to 0005 drops the decisions whose
  message is no longer cached, because that schema keys them by message row.
- **Suggestion IDs are never reused.** Re-saving an analysis replaces its suggestion rows,
  and `action_suggestions` is now AUTOINCREMENT, so an old ID (from an earlier
  `brief --show`, say) is not found instead of naming another suggestion.
- **In the desktop app, a deleted action comes back only through Undo.** There is no list
  of deleted actions yet.
- **Deleted actions and orphaned decisions are kept** until M9's retention controls; a
  decision whose email is gone is simply never matched.
- **Accepting a dismissed suggestion by its ID in the CLI overrides the dismissal**, by
  design: naming the ID is an explicit choice. When the suggestion's action was deleted,
  accepting it restores that action rather than copying it.
