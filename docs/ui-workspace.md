# The three-pane workspace: live checklist

The desktop window became the three-pane workspace in PR #21: a header with **Sync and
review** and **Cancel**, a sidebar of pages (Today, Actions, Waiting, Drafts, Briefs) over
Saved mail, Data, Settings and Connect or Disconnect, the page itself, and a status strip
with **Undo**. The automated tests render it offscreen and on macOS Cocoa with synthetic
data; the checks below need a person, real displays and a real Gmail account. The design
rules and component spec are in `.claude/skills/mailbrief-ui/`.

Run each check on Windows and on macOS, and note the date, platform and build.

## Keyboard only

- [ ] Without the mouse: **Sync and review**, step 1 (Space toggles a row, Continue),
      step 2 (Approve), then **Accept** a suggestion and **Undo** it.
- [ ] Tab runs Sync and review → Cancel (while busy) → the sidebar's pages → Saved mail →
      Data → Settings → Connect or Disconnect → the brief list → the selected email's
      buttons in display order → Undo (when shown) → back to Sync and review.
- [ ] Ctrl+1 to Ctrl+5 (Cmd+1 to Cmd+5 on macOS) open Today, Actions, Waiting, Drafts
      and Briefs; Briefs says MailBrief is busy while a run is in progress.
- [ ] The focus ring is visible on every stop, and Return or Enter opens a row once.
- [ ] In the brief list, Page Up and Page Down scroll the selected email while the whole
      list fits, and page the list when it scrolls.
- [ ] On Actions and Waiting, Tab runs the Open / Waiting / Completed tabs → the list →
      the selected action's buttons in display order; the arrow keys switch tabs, and
      Return on a row opens the editor once.
- [ ] In an action list, Page Up and Page Down scroll the selected action while the whole
      list fits, and page the list when it scrolls.

## Reading the brief

- [ ] The review step and the selected email show each sender as "Name <address>" (the
      address alone when there is no name, or when the name looks like an address).
- [ ] In a narrow review step, a cut address keeps the end of its domain: "bill…@" and the
      whole domain, else "…@…" and the domain's last part, never its first part alone.
- [ ] A timed deadline within the coming week reads "Due Fri 17:00"; a later or past one
      reads "Due Oct 23 17:00", counted from today, even on an older brief.
- [ ] Left open past midnight, within a minute: the header's "Checked Gmail at 23:50"
      becomes "Checked Gmail Oct 7 at 23:50"; the deadline chips recount without moving
      the selection or keyboard focus; the Actions page shows yesterday's actions as
      carried over (and overdue when they were due); and the Briefs page, if it shows,
      reloads. During a sync, that reload waits until the sync ends.

## Dates and language

- [ ] With the Mac set to another language (System Settings → General → Language &
      Region, another language first in the list), dates read in English everywhere,
      including the month and weekday names in the calendar pop-ups (Saved mail's date
      and the action editor's target date). Check it both after opening MailBrief from
      Finder and after starting it from Terminal. On Windows, check the same with another
      Windows display language.
- [ ] A draft or brief from another year shows its year: in Drafts ("updated Wed Dec 3,
      2025, 14:00"), in the draft editor's versions, and on the Briefs page ("Wed Dec 31,
      2025 · Complete · …").

## Screen readers

- [ ] VoiceOver on macOS reads the sidebar rows with their counts ("Actions, 5"), the
      brief rows (subject, sender and chips), the selected email in the detail pane and
      the status line.
- [ ] Narrator on Windows reads the same four.
- [ ] Both read the review step's rows by their full text, including "excluded in
      Settings", a tracked reply and a message left out earlier.

## Displays and window sizes

- [ ] On a 1x display, hairlines are one pixel and the text is sharp.
- [ ] On a 2x display, the same, with crisp icons and check boxes.
- [ ] At 1100 × 720 nothing is clipped, and a long shortlist scrolls inside the list
      while the page stays put.
- [ ] At the smallest window the OS allows, the sidebar and the status strip stay usable,
      long addresses wrap, and nothing scrolls sideways.

## Pages

- [ ] Actions and Waiting show rows beside the selected action, edge to edge like Today:
      the meta line (completed day, target without its reason, due date, steps), the
      Overdue, "N new in thread" and proposal chips, and muted Completed rows. The tab
      counts match the sidebar's.
- [ ] The detail shows the state, dates (an overdue deadline in the warning colour), the
      plan with ✓ and ○, notes, thread activity with **Mark seen**, pending proposals with
      **Proposals…**, the sources with **Open source** (it opens Gmail), and Edit,
      Complete or Reopen, Delete and Draft; each still works, with **Undo**.
- [ ] Each view with no actions shows its own empty message and no buttons.
- [ ] A long title, note or step wraps in the detail and never widens the window.

- [ ] The Briefs page lists saved briefs and missed days; opening a past brief returns to
      Today with the "Viewing the brief for …" banner and **Back to latest**.
- [ ] **Saved mail** opens the offline browser on today in your time zone, and works
      without a connection.
- [ ] **Data** opens Data and recovery; a backup and its verification succeed, and
      MailBrief reports itself busy while one runs.
