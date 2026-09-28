# M6 actions, target dates and plans

M6 is implemented on `feat/m6-actions` (PR #15) and awaits live acceptance. Decisions are
recorded in [ADR 0011](adr/0011-actions-and-suggestions.md).

## What it does

- Each analyzed email can carry up to five **suggestions**: something the email asks you
  to do, or something you are waiting for someone else to deliver. A suggestion has a
  short title, an optional effort, its own deadline and up to five plan steps.
- **Accept** turns a suggestion into an **action** you own; **Dismiss** hides it for that
  email for good. Accepted actions stay open across days until you complete them,
  whichever emails arrive later, and later AI runs never change them.
- Suggested **target dates** are one working day before a dated deadline, or the deadline
  itself when no earlier working day is left. You can change or clear any target.
- Actions appear in **Open** (yours), **Waiting** (on someone else) and **Completed**
  lists, marked "overdue" once their deadline passes and "carried over" from an earlier
  day. An action whose email is gone from the local cache says so and keeps its details.

## Desktop

- Under each email in the brief, pending suggestions show **Accept** and **Dismiss**
  links; accepted ones say so. The links work from the keyboard.
- **Your actions** lists the three views. **Edit…** (or Return on a row) changes the
  title, whose it is, effort, target date, notes and the plan: add, rename (F2; Return on
  macOS), reorder, check off or remove steps. One save is one revision.
  **Complete**/**Reopen**, **Delete** and **Open source** (Gmail only) act on the selected
  action.
- **Undo** reverses the latest accept, dismiss, complete, reopen or delete. It is withdrawn
  by an edit or a new brief, and refuses when the action changed in between.
- **Continue** in the shortlist review needs at least one checked message, so an empty
  selection can no longer overwrite today's saved brief.

## CLI

```bash
uv run mailbrief-gmail-diagnostic brief --show          # suggestions show as [pending #N]
uv run mailbrief-gmail-diagnostic actions accept N
uv run mailbrief-gmail-diagnostic actions dismiss N
uv run mailbrief-gmail-diagnostic actions list [--view open|waiting|completed] [--timezone ZONE]
```

`actions` never contacts Gmail or the AI. Pass `--database PATH` to use another database.
An unknown ID, or a change that isn't allowed (such as dismissing an accepted suggestion),
exits 3 with a fixed message. `brief --show` prints suggestion titles, targets and steps,
never evidence.

## Limits

- Suggestion titles and steps are at most 120 characters; a suggestion evidence quote at
  most 160. All stored quotes for one email total at most 600 characters and stay under
  80% of its body. Your own action text allows 200-character titles, 500-character steps,
  30 steps and 10,000 characters of notes.
- After a model or prompt change, a reworded suggestion may be offered again even though
  an earlier wording was dismissed (see ADR 0011).
- Targets ignore holidays and your calendar; there is no scheduling yet.
- The first brief after upgrading re-analyzes today's shortlist once (schema version 6).
- Migration 0005 runs automatically, after the usual pre-upgrade backup. The app from
  before M6 cannot open an upgraded database; restore the backup to go back.

## Live acceptance checklist

1. [ ] On a throwaway database, send yourself an email with two requests and a Friday
   deadline, then run `brief --database <tmp path> --show`. Two `[pending #N]` lines
   appear with targets and steps; the `AI:` line shows no Groq rejection, and output
   tokens per request stay well under 2,000.
2. [ ] `actions accept N`, `actions dismiss M` and `actions list` behave as described.
   Rerun `brief --database <tmp path> --yes --show`: the accepted suggestion shows
   `[accepted]`, the dismissed one is gone, and `AI: nothing sent this run`.
3. [ ] Desktop: accept a suggestion, then edit its target date and steps and check one
   step off. Restart MailBrief: the action is unchanged. Sync and review again: the
   edits survive and the dismissed suggestion is not offered.
4. [ ] The next day, the open action shows "carried over" without any new email.
5. [ ] Undo works once for each of accept, dismiss, complete, reopen and delete.
6. [ ] An email whose text tries to plant links or terminal codes in a suggestion shows
   only plain text in the app, and `brief --show` prints no escape characters.
7. [ ] On macOS, Return on an action row opens the editor; the Keychain prompts are no
   different from M5's.
8. [ ] The packaged Windows and macOS builds from CI start and show the actions pane.

## For developers

- Domain: `domain/actions.py`; suggestions live in `domain/analysis.py` (schema 6).
- Services: `services/actions.py` (`ActionService`); target rule in
  `services/deadlines.py`; suggestion validation in `services/analysis.py`.
- Storage: `storage/actions.py`, migration `20260927_0005_actions`.
- UI: `ui/actions_view.py`, `ui/action_editor.py`; the desktop runtime opens one session
  per action call.
