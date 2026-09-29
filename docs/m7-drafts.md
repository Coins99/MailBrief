# M7 drafts, notes and messages

M7 is implemented on `feat/m7-drafts` (PR #16) and awaits review: local drafts and notes,
and AI drafting with Groq. The storage policy is [ADR 0012](adr/0012-drafts-and-notes.md);
what AI drafting may send and keep is [ADR 0013](adr/0013-ai-drafting.md).

## What it does

- Write four kinds of text inside MailBrief: **replies** and new **emails** (with To, Cc and
  Subject), **notes** and copyable **messages** (with a title).
- Drafts live on this device. Writing, saving, versions, copying and exporting never contact
  Gmail, Groq or the network. Only **Write with AI** sends anything, and only what you tick
  and approve (see below). Nothing ever writes to your mailbox, and there is no "sent"
  state. Copying or exporting a draft changes nothing else: actions keep their status.
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

## Writing with AI

**Write with AI…** in the editor (it needs a Groq model and key in Settings) opens a panel:

1. **Choose what may be sent.** Only the parts this draft has are offered, all ticked:
   - **the email you're replying to**, while it is still in local mail. Its body is
     downloaded from Gmail now (silent sign-in only), prepared like the brief's (the same
     length limit, quoted history trimmed) and never saved;
   - **the linked action**, while it exists: its title, whose it is, target date, deadline
     phrase, its steps in order up to 2,000 characters in all, and up to 2,000 characters of
     notes;
   - **your current text**: the title and up to 8,000 characters of the body.
   Pick a tone (neutral, warm, formal or direct), a length (short, about 80 words at most;
   medium, about 80 to 200; long, about 200 to 400) and, optionally, instructions (up to
   1,000 characters).
2. **Continue** saves any unsaved text and shows exactly what will be sent: each part with its
   character count, the model and Groq's privacy notice. From here until the end, the text
   is frozen, so the preview is what is sent.
3. **Send to Groq.** The first time, tick "I agree" to the disclosure first; that consent is
   recorded for you (not per account) before anything is sent, and asked for again when the
   disclosure changes (it is now version 2). **Cancel** is always there.
4. While Groq writes, **Cancel** stops it, and so does quitting. Nothing is written after a
   cancel.

**What is always sent:** the draft's kind, the tone, the length, your instructions and
today's date. **What is never sent:** the sender's address (only their name), To or Cc,
other emails, attachments, your other drafts or actions, Gmail or MailBrief IDs, or
credentials. The parts you tick are sent as written, so any addresses, links or numbers
inside them (in the email's body, your text or the action's notes) are sent too. A part cut
to fit its limit says "(cut to fit)" in the preview.

**The result** becomes a new version: the status says "New version vN from Groq; your
previous text is version vM." Your own text is saved as a version before anything is sent,
so **Versions…** can always bring it back, and generated versions say which model, tone and
length wrote them. Groq never supplies recipients: To and Cc stay yours. A subject is used
only for emails and notes, and only when the draft has no title yet. Facts Groq lacks stay
as `[[placeholders]]`, and "Missing context to check" lists them.

MailBrief checks every answer before using it: control and format characters are removed,
quoted history is dropped, the body must be 1 to 8,000 characters, and an answer that copies
200 or more characters of the email word for word is refused. An unusable answer is tried
once more. If it still fails, or you decline, cancel, or the draft changed meanwhile, your
text is unchanged and the status says why. If Groq declines to write the draft, the status
says so (`AI_REFUSED`): change the instructions or context and try again.

## CLI

```bash
uv run mailbrief-gmail-diagnostic drafts list [--database PATH]
uv run mailbrief-gmail-diagnostic drafts export ID [--format text|markdown] [--out PATH] [--database PATH]
uv run mailbrief-gmail-diagnostic drafts generate ID [--use-email] [--use-action] [--use-text] \
    [--tone neutral|warm|formal|direct] [--length short|medium|long] [--instructions TEXT] \
    [--yes] [--silent-only] [--database PATH]
uv run mailbrief-gmail-diagnostic ai-consent status|revoke [--database PATH]
```

`drafts list` prints each draft's ID, kind, title, update time and placeholder count,
newest first. `drafts export` prints the draft (control characters that could drive a
terminal are left out of the printed text), or writes it exactly to a new file with
`--out`. An existing file is never replaced, and an unknown ID exits 3 with a fixed
message. Neither command contacts Gmail or the AI.

`drafts generate` prints what will be sent, then asks: the first time you must type `yes`;
later runs ask `Send? [y/N]`, which `--yes` answers when consent is already recorded (it
never grants it). It prints the new version number, the placeholders and the missing
context, never the text: read it with `drafts export`. `--use-email` downloads the email's
body from Gmail; add `--silent-only` to never open a browser. Exit codes match `brief`'s:
0 written, 3 a part that isn't available or an unknown draft, 4 a failure (the draft is
unchanged), 6 declined, 130 cancelled. `ai-consent status` also shows drafting consent, and
`ai-consent revoke` withdraws it with the brief's, so the next generation asks again.

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
- AI drafting shares the brief's Groq settings: the model, `MAILBRIEF_AI_MAX_OUTPUT_TOKENS`
  (reasoning included) and the request cap, which each generation gets afresh. An answer Groq
  couldn't finish within the output limit is retried once and then reported.
- Migrations 0007 and 0008 run automatically, after the usual pre-upgrade backup. The app
  from before M7 cannot open an upgraded database; restore the backup to go back.

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
7. [ ] Go offline: every drafts feature except Write with AI still works.
8. [ ] Reply using the email: the preview lists exactly the chosen parts.
9. [ ] First-use consent appears; declining sends nothing.
10. [ ] Regenerate twice: two generated versions; then restore your own text.
11. [ ] A Chinese email gets a Chinese reply.
12. [ ] An email saying "ignore previous instructions…" changes nothing.
13. [ ] No recipients ever come from the AI.
14. [ ] Offline, or with a wrong key, the text is untouched and the message is clear.
15. [ ] A long email with several requests doesn't hit the output limit at 4,000 tokens.
16. [ ] After revoking consent, the next generation asks again.

## For developers

- Domain: `domain/drafts.py` (kinds, limits, placeholders, exports and file names).
- Service: `services/drafts.py` (`DraftService`); every method commits once and needs the
  revision it saw, except restoring a deleted draft.
- Storage: `storage/drafts.py`, migration `20260928_0007_drafts`; drafts and versions
  change only through ORM objects.
- UI: `ui/drafts_view.py`, `ui/draft_editor.py`; `ui/main_window.py` runs draft writes
  through `DraftWrites`, one serialized, coalescing queue that shutdown drains.
- Exports: `infra/files.py` writes UTF-8 through a temporary file and an atomic rename.
- AI drafting: `domain/drafting.py` (what is sent and returned), `ports/drafting.py`
  (`DraftingProvider`), `services/drafting.py` (`DraftingService`: parts, preview, consent,
  validation), `draft()` in `providers/groq/provider.py` (prompt `groq-draft-2026-09-28.1`),
  `ui/drafting_panel.py`, and migration `20260928_0008_drafting` (`owner_consents`,
  `draft_generations`). Text helpers: `clean_generated_block` and `copies_long_run`.
