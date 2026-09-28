# M7 drafts, notes and messages

M7 is in progress on `feat/m7-drafts`: local drafts and notes (stage 1) are implemented;
AI drafting (stage 2) is next. The storage policy is
[ADR 0012](adr/0012-drafts-and-notes.md).

## What it does

- Write four kinds of text inside MailBrief: **replies** and new **emails** (with To, Cc and
  Subject), **notes** and copyable **messages** (with a title).
- Everything is local. Drafting never contacts Gmail, Groq or the network, never writes to
  your mailbox and has no "sent" state. Copying or exporting a draft changes nothing else:
  actions keep their status.
- A reply is filled in from the cached email only: "Re: <subject>" (unless the subject
  already starts with "Re:") and the sender in To. MailBrief never downloads the email's
  body for a draft and never inserts quoted history. Text you type or paste is yours and
  is stored exactly as entered.
- Drafts belong to no account. They keep a snapshot of their email (subject, sender, link,
  received time) and of their action's title, so they survive deleting the email from the
  local cache, disconnecting the account or deleting the action. The editor then says
  "source no longer in local mail".

## Desktop

- **Start a draft** from a brief item (**Draft a reply**), from the actions panel
  (**Draft…**: Reply to its email, Email, Note or Message; Reply needs the action's email
  still in local mail), or from **Your drafts and notes** (**New**: Email, Note or Message).
- **Open** a draft with the button, Return or a double-click. **Delete draft** hides it;
  **Undo delete** brings it back.
- Each row shows the kind, title, when it changed, how many placeholders are left and the
  action it is for.
- **The editor** shows where the draft came from (with **Open in Gmail** for Gmail links
  only), the fields for its kind, a character counter, recipient warnings for items that
  are not one plausible address, and a live "Placeholders to fill" line for `[[...]]`
  tokens.
- **Autosave** runs 1.5 s after you stop typing, and at least every 10 s while you keep
  typing. The status line reads "Saved HH:MM", "Saving…" or the problem. Saving never makes
  the main window busy.
- **Save version** keeps the current text as a version. **Versions…** lists them with a
  preview; **Restore this version…** asks first, saves your current text as a version,
  then brings the old text back, so a restore can itself be undone.
- **Copy text**, **Copy subject** (emails) and **Export…** (`.md` or `.txt`, UTF-8, replaced
  atomically) never change the draft. When placeholders are left, the status says how
  many still need filling.
- **Close** saves any unsaved text and a version, then closes. If that fails, the editor
  stays open with your text; press Close again to close without saving the latest changes.
- **If the draft changed elsewhere** (another window, say), autosave stops and the editor
  says "This draft changed elsewhere. Your text is still here." **Save as new draft** keeps
  your text as a new draft with the same kind, email and action, and editing continues
  there.
- **If a save fails**, the text stays and the status reads "Not saved; retrying"; it retries
  on your next change or after 5 s.
- **Quitting** saves the open draft's latest text before the database closes.

## CLI

```bash
uv run mailbrief-gmail-diagnostic drafts list [--database PATH]
uv run mailbrief-gmail-diagnostic drafts export ID [--format text|markdown] [--out PATH] [--database PATH]
```

`drafts list` prints each draft's ID, kind, title, update time and placeholder count,
newest first. `drafts export` prints the draft (control characters that could drive a
terminal are left out of the printed text), or writes it exactly to a new file with
`--out`. An existing file is never replaced, and an unknown ID exits 3 with a fixed
message. Neither command contacts Gmail or the AI.

## Limits

- Title or subject: 200 characters. To and Cc: 2,000 characters each. Body: 20,000
  characters; above that the editor warns and pauses autosave until the text is shorter.
- At most 100 versions per draft; the oldest are pruned. Autosave updates the draft without
  adding a version; versions are saved when you ask, around a restore and on Close.
- Only replies and emails have recipients. Recipients are free text, so a half-typed address
  still saves; warnings are hints, not checks.
- The database is not encrypted; your OS account protects it, as for the rest of MailBrief.
- Deleted drafts stay in the database until M9's retention controls. In the desktop app a
  deleted draft comes back only through Undo.
- Migration 0007 runs automatically, after the usual pre-upgrade backup. The app from before
  M7 cannot open an upgraded database; restore the backup to go back.

## AI drafting (stage 2, in progress)

Stage 2 will add AI drafting on this branch and pull request. It will send only the context
you select, after a preview and explicit consent, and add its own migration (0008). AI-written
text will follow the same storage rules as your own; `generated` is already reserved as a
version origin.

## Live acceptance checklist

1. [ ] Create a reply from a brief item (To and Subject prefilled), a note from an action,
   and a blank message.
2. [ ] Type, wait, kill the process, relaunch: everything up to about 2 s before the kill
   survives.
3. [ ] Save versions, restore one, then restore back.
4. [ ] Export `.md` and `.txt` and open both files.
5. [ ] Delete a draft, then undo.
6. [ ] On a throwaway database, delete a draft's source message: the draft keeps its
   snapshot, marked unavailable.
7. [ ] Go offline: every drafts feature still works.

## For developers

- Domain: `domain/drafts.py` (kinds, limits, placeholders, exports and file names).
- Service: `services/drafts.py` (`DraftService`); every method commits once and needs the
  revision it saw, except restoring a deleted draft.
- Storage: `storage/drafts.py`, migration `20260928_0007_drafts`; drafts and versions
  change only through ORM objects.
- UI: `ui/drafts_view.py`, `ui/draft_editor.py`; `ui/main_window.py` runs draft writes
  through `DraftWrites`, one serialized, coalescing queue that shutdown drains.
- Exports: `infra/files.py` writes UTF-8 through a temporary file and an atomic rename.
