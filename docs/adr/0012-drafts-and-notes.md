# ADR 0012: Owner-owned drafts and notes in local storage

Date: 2026-09-28. Status: accepted.

Amends ADR 0005's local storage rule and builds on ADR 0011's owner-owned actions.

## Context

M7 lets the owner write email replies, new emails, notes and copyable messages inside
MailBrief, so work is not lost between sessions. ADR 0005 says full message bodies are never
stored. Drafts are not message bodies: they are the owner's own writing. This ADR states
what a draft may hold, so drafting never becomes a way to store incoming mail.

## Decision

- **May be stored:** the owner's writing: a draft's title or subject, recipients exactly as
  typed, its body and its saved versions; a snapshot of each source email (subject, sender
  address, Gmail link and received time); and a link to an action with a snapshot of its
  title.
- **Never stored: a downloaded incoming body.** A reply is built from the cached message
  row alone (subject and sender), and MailBrief never inserts quoted incoming history into
  a draft. Text the owner types or pastes is theirs and is stored exactly as entered, with
  no trimming.
- **Drafts belong to no account.** Their source and action links use `ON DELETE SET NULL`
  and keep snapshots, so a draft survives deleting its message, the cache, its account or
  its action. The source then reads "no longer in local mail". Deleting a draft is a soft
  delete that restore brings back.
- **At most 100 versions per draft.** A version is saved when the owner asks, before a
  restore, after a restore and when the editor closes, and only when the content changed.
  Autosave updates the draft itself without adding a version. The oldest versions beyond
  100 are pruned.
- **Draft text stays private.** It never reaches logs, exceptions or error messages, and
  never reaches a file except an export the owner chooses. Like the rest of the database,
  drafts are not encrypted; the OS account protects them.
- **Drafting never writes to the mailbox and has no "sent" state.** Copying or exporting a
  draft changes nothing else: actions keep their status, and nothing is marked done.
- **Every change needs the revision the caller saw**, except restoring a deleted draft. A
  mismatch raises `DraftConflictError`; the editor keeps the text and offers to save it as a
  new draft. Drafts and versions change only through ORM objects.
- **AI drafting (M7 stage 2) will send only owner-selected context**, after a preview and
  explicit consent. Text the AI writes will follow the same storage rules.

## Consequences

- Migration 0007 adds `drafts`, `draft_versions` and `draft_sources`. Downgrading below
  0007 drops every draft.
- Draft text is the owner's own content, so it is stored in full; a very long pasted email
  is stored because the owner put it there, not because MailBrief fetched it.
- Soft-deleted drafts are kept until M9's retention controls.
