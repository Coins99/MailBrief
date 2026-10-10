# M6 actions, target dates and plans

M6 is merged into `main` from PR #15; live acceptance is still pending. Decisions are
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
  An exact deadline is shown in your time zone, in the brief and in the lists.

## Desktop

- Under each email in the brief, pending suggestions show **Accept** and **Dismiss**
  links; accepted ones say so. The links work from the keyboard. Since M8 Part 5, an email
  that continues an open action's thread also offers **Add to “<title>”**, which accepts
  the suggestion into that action as another source instead of creating a new one
  ([m8-daily-operation.md](m8-daily-operation.md#thread-activity-and-adding-to-an-action-part-5)).
- **Your actions** shows the Open, Waiting and Completed views as tabs over a list of
  rows, beside the selected action's detail: its dates, plan, notes, thread activity,
  proposals and sources. **Edit…** (or Return on a row) changes the title, whose it is,
  effort, target date, notes and the plan: add, rename (F2; Return on macOS), reorder,
  check off or remove steps. One save is one revision. The editor stays open until the
  save succeeds; if it fails, or the action changed in the meantime, your edits stay in
  the dialog with a message saying what to do.
  **Complete**/**Reopen**, **Delete** and **Draft…** sit at the foot of the detail, and
  **Open source** (Gmail only) follows the action's first Gmail source.
- **Undo** reverses the latest accept, add, dismiss, complete, reopen or delete. It is
  withdrawn by an edit or a new brief, and refuses when the action changed in between.
- **Continue** in the shortlist review needs at least one checked message, so an empty
  selection can no longer overwrite today's saved brief.

## CLI

```bash
uv run mailbrief-gmail-diagnostic brief --show          # suggestions show as [pending #N]
uv run mailbrief-gmail-diagnostic actions accept N
uv run mailbrief-gmail-diagnostic actions accept N --into PUBLIC_ID   # M8: add to an action
uv run mailbrief-gmail-diagnostic actions dismiss N
uv run mailbrief-gmail-diagnostic actions list [--view open|waiting|completed] [--timezone ZONE]
```

`actions` never contacts Gmail or the AI. Pass `--database PATH` to use another database.
An unknown ID, or a change that isn't allowed (such as dismissing an accepted suggestion),
exits 3 with a fixed message. `brief --show` prints suggestion titles, targets and steps,
never evidence.

- Suggestion IDs are never reused, so an ID from an older `brief --show` whose suggestion
  has since been replaced is not found, rather than accepting a different suggestion.
- `actions accept N` on a suggestion you dismissed accepts it anyway: naming the ID is
  your choice. If you deleted the action it became, accepting it brings that action back.

## Limits

- Suggestion titles and steps are at most 120 characters; a suggestion evidence quote at
  most 160. All stored quotes for one email total at most 600 characters and stay under
  80% of its body. Your own action text allows 200-character titles, 500-character steps,
  30 steps and 10,000 characters of notes.
- After a model or prompt change, a reworded suggestion may be offered again even though
  an earlier wording was dismissed (see ADR 0011).
- Targets ignore holidays and your calendar; there is no scheduling yet.
- The first brief after upgrading re-analyzes today's shortlist once (schema version 6
  and a new prompt version).
- Accept and dismiss decisions are kept per Gmail account and message, so they survive
  removing the email from the local cache, and disconnecting and reconnecting the same
  account: when the email is synced again, it shows the same decisions.
- Open and Waiting list every action. Completed shows the newest 200; when there are
  more, its tab reads "Completed (200+)" and `actions list --view completed` ends with a
  line saying how many there are.
- In the desktop app, a deleted action comes back only through Undo. Deleted actions,
  and decisions whose email is gone, stay in the database until M9's retention controls.
- Migrations 0005 and 0006 run automatically, after the usual pre-upgrade backup. The app
  from before M6 cannot open an upgraded database; restore the backup to go back.

## Live acceptance checklist

1. [ ] On a throwaway database, send yourself two emails: one with five separate requests,
   one of them due Friday, and one written in Chinese. Run
   `brief --database <tmp path> --show` with `openai/gpt-oss-120b`. The first shows five
   `[pending #N]` lines and the second its own, with targets and steps. No message is
   reported as "Incomplete" or rejected by Groq. Record the output tokens per request
   (the `AI:` line's output tokens divided by its requests): ______; they stay under
   4,000.
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
- Storage: `storage/actions.py`, migrations `20260927_0005_actions` and
  `20260928_0006_stable_decisions` (decisions keyed by `DecisionKey`, suggestion IDs never
  reused).
- UI: `ui/actions_view.py`, `ui/action_editor.py`; the desktop runtime opens one session
  per action call.
