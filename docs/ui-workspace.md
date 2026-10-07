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

- [ ] The Briefs page lists saved briefs and missed days; opening a past brief returns to
      Today with the "Viewing the brief for …" banner and **Back to latest**.
- [ ] **Saved mail** opens the offline browser on today in your time zone, and works
      without a connection.
- [ ] **Data** opens Data and recovery; a backup and its verification succeed, and
      MailBrief reports itself busy while one runs.
