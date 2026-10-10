# MailBrief UI specification

## Tokens

DARK is measured from the approved mockup screenshot
(`docs/ui/mockup-three-pane-dark.png`). LIGHT is provisional until a light mockup is
approved. Text tokens meet WCAG AA (4.5:1) on `panel` and `selection` in both modes;
`tests/ui/theme/test_theme.py` checks this.

| field | DARK (measured) | LIGHT (provisional) | used for |
|---|---|---|---|
| canvas | #151515 | #f5f4f0 | header bar |
| panel | #1a1a19 | #ffffff | window, panes, cards |
| selection | #151515 | #f5f4f0 | selected row and nav item, hover |
| hairline | #313130 | #e3e2dd | dividers, card borders |
| border_strong | #484847 | #c9c8c2 | outline buttons |
| accent_border | #193567 | #9dbbe8 | selected-row bar |
| text | #f0efec | #1f1e1b | titles, body |
| text_secondary | #c3c2b8 | #5f5e5a | senders, meta |
| text_muted | #898782 | #6f6e69 | section labels, notes |
| warning_fg / warning_bg | #d19633 / #2e1b04 | #8a5a00 / #fbefd9 | deadlines |
| accent_fg / accent_bg | #7aa5e6 / #0b1f40 | #1d4f91 / #e3edfb | proposal chips, focus |

Metrics (logical px): RADIUS 8, SIDEBAR_WIDTH 128, TEXT_PX 13, SMALL_PX 12, CAPTION_PX 11,
TITLE_PX 15.

## Hairlines (`ui/hairline.py`)

All read colours from `current_tokens()` at paint time.
- `HairlineFrame(QFrame)`: card container (`radius=RADIUS`, `strong=False`), contents
  margins 12, 10, 12, 10. Paints an antialiased rounded rect with `QPen(color, 0)`
  (cosmetic: one device pixel at any scale), inset by `0.5 / devicePixelRatioF()`.
- `HairlineDivider(QWidget)`: horizontal or vertical, 1 logical px, cosmetic line, no
  antialiasing.
- `HairlineSplitter(QSplitter)`: `handleWidth(5)` so it can be grabbed; its handle fills
  with panel and draws one cosmetic hairline at its centre.
- `paint_focus_ring(painter, rect, state, color)`: the focus ring of the sidebar, brief
  list and shortlist delegates, a 1px cosmetic rect inset by 1, painted only when
  `keyboard_focus(state)` is true (focus that the keyboard moved, not focus on first
  show).

## Brief list (`ui/brief_list.py`)

- Rows: a header row (from `SECTION_TITLES`) whenever the digest section changes, then
  item rows in digest order. `BriefRow(kind, title, item, sender, chips)`.
- Sender: the contact's name, else its address (`sender_text`). The review step and the
  detail pane use `sender_with_address`: "Name <address>", or the address alone when the
  name is empty, is the address (ignoring case) or contains "@", so a display name can
  never pass for someone else's address.
- Deadline chip (WARNING), its text in the digest's zone (as the detail pane shows it),
  `deadline_chip(item, zone, today, owner_zone)` with the owner's current local day as
  today (`build_rows(digest, proposals, today, owner_zone)`, from `show_digest`), whatever
  day the brief covers. "Within the week" is judged by the deadline's date in the owner's
  zone: DATETIME from today to today + 6 days `Due {local:%a %H:%M}`, any other DATETIME
  (past ones too) `Due {%b} {day} {%H:%M}`;
  DATE `Due {%b} {day}`; UNRESOLVED `Due ` + the phrase cut to 24 characters at a grapheme
  boundary (`cut_text`), with "…";
  NONE no chip.
- Proposal chip (ACCENT), from the first pending proposal: NEW_DEADLINE "Proposes a new
  deadline"; CANCELLED or DELIVERED "Proposes completing an action".
- Model: `KIND_ROLE = UserRole + 1`, `ROW_ROLE = UserRole + 2`. Display is the header
  title or the subject ("(no subject)" when empty). Accessible text is the header title
  or "{subject}, from {sender}" plus the chip texts. Header rows are `NoItemFlags`; item
  rows are enabled and selectable.
- Delegate (`drawText` only): header rows 12px muted at left 12, 12 above and 4 below.
  Item rows padded 8 vertical, 12 horizontal: selected rows fill with selection and draw
  a 2px accent_border bar at the left; title 13px Medium (text); sender 13px
  (text_secondary); chips after a 4px gap, 11px, padding 1 × 6, radius `min(8, h/2)`, 6px
  apart, warning or accent fg/bg; a cosmetic hairline at the bottom; with focus, a 1px
  cosmetic accent_fg rect inset by 1. Title and sender elide to the row width minus 24.
- Shared with the run page's shortlist and the Actions page's rows: `paint_selection`,
  `paint_lines` (a title and a `subtitle`: a sender or a meta line), `paint_chips`,
  `paint_separator`, `row_height`, `elided`, `flat`, `SINGLE_LINE`, `ROW_PAD_V`,
  `ROW_PAD_H`, `ELIDE_MARGIN` and `CHIP_GAP`; the focus ring is `paint_focus_ring()` from
  hairline.py. Never copy painting code: paint new rows with these.
- View: objectName `briefList`, accessible name "Brief items", NoFrame,
  ScrollPerPixel, SingleSelection; `item_selected(DigestItem)`; `show_rows(rows)` selects
  the first item row; Return and Enter emit `activated` once and are consumed.
  While the whole list fits (its vertical scroll bar has no range), Page Up and Page
  Down (no modifier) scroll the detail pane by a page (`set_page_scroll`, set by the
  workspace); when the list scrolls, they page the list. The rule is `page_detail()`,
  shared with the Actions page's lists.

## Wrapping labels (`ui/labels.py`)

`WrapLabel(QLabel)`, made with `wrap_label(text, *, tone=None, px=None, medium=False)`
(like `plain_label`): plain text laid out with `QTextLayout` and
`WrapAtWordBoundaryOrAnywhere`, so an unbroken token (an address, a link, a long word)
wraps instead of widening its container. Line breaks in the text are kept, and the layout
(never `text()` or the accessible name) gets a zero-width space after every `@` and `/`,
so an address wraps after its `@` and a path after a slash before anywhere else.
`hasHeightForWidth()` is True and `heightForWidth(w)` includes the contents margins;
`sizeHint()` is at most 40 average characters wide with its matching height;
`minimumSizeHint()` is 0 wide and one line high. Layouts are cached per width for the
current text and font, at most 8 widths; a change of text or font, even by
`QLabel.clear()`, drops them. It paints in the palette's
`WindowText`, so the stylesheet's `tone` colours apply. `setText` keeps `text()` and makes
the whole text the accessible name; no tooltip, no links.

## Detail pane (`ui/brief_detail.py`)

`BriefDetailPane(QScrollArea)`, objectName `briefDetail`, accessible name "Selected
email". Signals: `suggestion_requested(str, int)` (ACCEPT or DISMISS, from this module),
`accept_into_requested(int, str, int)`, `proposal_requested(str, int, int)` (APPLY or
DISMISS), `reply_requested(str, str)` and `source_requested(str)`; it never opens URLs
itself. `rebuilt()` follows each `show_item` and `show_empty`, and `buttons()` lists the
content's buttons in display order. The pane has `ClickFocus`: Tab goes from the brief
list straight to its buttons, and a click still lets the arrow keys scroll it.
Its content is at most 720 px wide (`DETAIL_MAX_WIDTH`) and stays at the left on wide
windows. Content margins 16, 14, 16, 14, spacing 8, in order (every mail- or AI-derived
line is a `WrapLabel`):
1. Subject (or "(no subject)"), 15px Medium.
2. Sender, secondary, `sender_with_address` (objectName `detailSender`). No received
   time is invented.
3. Summary.
4. Action text, secondary, if present and different from the summary.
5. Deadline: 14px clock icon (warning_fg) and a warning label, `Due ` + `deadline_text`
   in the brief's zone (see Dates).
6. `Continues: “{title}”`, secondary, for each link that isn't a source.
7. Suggestions. PENDING: a `HairlineFrame` card with the title (13px Medium), a 12px
   secondary meta line ("Yours." or "Waiting for someone.", then the target with its
   reason, else the deadline, else "No deadline stated."), the plan's steps in order
   (`· {step}`, 12px secondary), Accept and Dismiss, and one `Add to “{title}”` button
   per link. ACCEPTED: `Accepted: {title}`, secondary, with no plan.
   DISMISSED: nothing.
8. Each pending proposal: a card with `Proposes for “{action_title}”: “{evidence}”`, an
   `effect_text` button (APPLY) and Dismiss.
9. Footer: "Draft a reply" (pencil) and, only for https links on mail.google.com,
   "Open in Gmail" (external-link).
10. A stretch.
Buttons are made with `outline_button()` (`button_label()` text, `variant="outline"`,
`setAutoDefault(False)`) and have an objectName: `acceptButton`, `dismissButton`,
`addToButton`, `applyProposalButton`, `dismissProposalButton`, `replyButton` and
`openInGmailButton`.

## Actions page (`ui/actions_view.py`, `ui/action_detail.py`, `ui/action_text.py`)

`ActionsPanel` (objectName `actionsPanel`) is the `actions` page, edge to edge like Today:
a `HairlineSplitter` (stretch 4 : 5, children not collapsible) holding the list side
(`actionsListPane`, at least 220 px) and `ActionDetailPane` (at least 280 px).
- List side, top to bottom: "Your actions" (15px Medium, `actionsHeading`, margins 12,
  10, 12, 4); `tabs`, a `QTabBar` (`actionsTabs`, "Action views": "Open (N)",
  "Waiting (N)", "Completed (N)" or "(200+)", no base, not expanding, `StrongFocus` so Tab
  reaches it on macOS too); `stack`, a `QStackedWidget` (`actionsStack`) holding the three
  `lists[view]`. `show_view` sets the tab bar's index and the stack; a click on a tab
  moves the stack; `view()` reads the tab bar.
- Each list is an `ActionList` (an `ActivatingList`; objectName `actionList`, accessible
  name "{Open|Waiting|Completed} actions"; NoFrame, ScrollPerPixel, no horizontal scroll
  bar) painted by `ActionRowDelegate`. Each item's text is exactly `describe()`'s, and
  it carries `action_row()`'s `ActionRow(title, meta, chips, muted)` under `ROW_ROLE`
  (brief_list's). Page Up and Page Down page the detail while the list fits
  (`page_detail`).
- `ActionRow`: meta joins, with " · ", "Completed Tue Oct 6" (in the owner's zone), the
  target ("Target Fri Oct 2", never its reason: the detail gives that), "Due
  {deadline_text}" ("Due Mon Oct 5") and "2/5 steps", and nothing else (the detail pane
  shows "Carried over" and "Source no longer in local mail"); with none, "No target or
  deadline". Chips: "Overdue" (WARNING,
  open actions only), "N new in thread" (ACCENT, while new messages are unseen) and
  `proposal_chip` for pending proposals. Completed rows are muted: title text_secondary,
  meta text_muted.
- `ActionRowDelegate`: the brief list's row, from the shared helpers only; painting is
  clipped to the row; `sizeHint` is `QSize(0, row_height(has_chips))`, so a row never
  widens the page.
- `action_details()` (`ui/action_text.py`) works out an action's dates, progress, state,
  thread activity and pending proposals once; `describe()`, `action_row()` and the
  detail pane all read it, so they never disagree.
- `ActionDetailPane(QScrollArea)`: objectName `actionDetail`, "Selected action",
  `ClickFocus`, content (`actionDetailContent`) at most 720 px wide at the left, margins
  16, 14, 16, 14, spacing 8. Its seven buttons are made once and stay (the panel exposes
  them under its old names), so only the text between them is rebuilt. In order, every
  mail-, AI- or owner-written line a `WrapLabel`:
  1. Title, 15px Medium (`actionTitle`).
  2. State, secondary: "Yours · open", "Waiting for someone · open" (either with " · carried over" while it
     applies) or "Completed Tue Oct 6".
  3. "Due {deadline_text}", warning with " · Overdue" when overdue, else secondary
     (`actionDeadline`); the target with its reason while it is still the suggested one
     ("Target Fri Oct 2, one working day before the deadline"), secondary
     (`actionTarget`).
  4. "Plan · 2 of 5 done" (12px muted heading), then each step in order: "✓ {text}"
     muted when done, "○ {text}" otherwise.
  5. "Notes" and the notes.
  6. "Thread" and `describe()`'s activity lines, first letter capitalised ("2 new in
     thread, latest Tue 14:02 from Sam", "You replied Sun Sep 20"); then `seenButton`
     (Mark seen).
  7. A sentence about the pending proposals; then `proposalsButton` (Proposals…).
  8. "Source" or "Sources": each subject ("(no subject)" when empty), then 12px
     secondary "{address} · Received Fri Oct 2" (`day_text`), plus " · No
     longer in local mail". `sourceButton` (Open source, external-link) follows the first
     Gmail source.
  9. Footer: `editButton`, `completeButton` (Complete, or Reopen on Completed),
     `deleteButton`, `draftButton` (pencil, with the draft menu).
  Empty states, muted (`actionEmpty`), with no buttons: Open "No open actions. Accept a
  suggestion in a brief to start one.", Waiting "Nothing you're waiting for.", Completed
  "No completed actions yet."
- Tab: the tab bar, the list shown, then the detail's buttons in display order.
- Behaviour is unchanged: `_update_buttons` keeps its rules, every button emits
  `action_requested` or `draft_requested` for MainWindow's `start()`, and only Gmail
  links open.

## Dates (`ui/deadline_text.py`)

One style across the window, as the brief reads: every date or time a person sees uses
`day_text` or `moment_text`, in the owner's zone. Only the action editor's date fields
stay ISO, since dates are typed there, and the CLI is unchanged
(`git grep -nE "isoformat\(\)|%Y-%m-%d" src/mailbrief/ui` lists only `action_editor.py`).
The brief list's chips keep their shorter forms (see Brief list).
- Weekday and month names are the fixed English `WEEKDAYS` and `MONTHS` of
  `deadline_text.py`, never `%a` or `%b`: Qt applies the system language to C date
  formatting, so a French Mac would otherwise print "lun." and "août". No file anywhere in
  `src/mailbrief` uses a strftime directive that follows the system language (`%a`, `%A`,
  `%b`, `%B`, `%p`, `%c`, `%x`): `tests/unit/test_source_rules.py` fails, naming the file and
  line, if one appears. Wording a person reads therefore lives in `ui`, which can use these
  helpers; services keep ISO dates (`coverage_line`, which the CLI prints).
- Qt's own widgets take their month and weekday names (the calendar pop-ups of Saved mail
  and the action editor) from the default `QLocale`, which follows the system language.
  `use_english_locale()` in `app.py` sets it to English, and `app.main()` calls it right
  after the QApplication exists: a widget takes the default locale when it is created, so
  no widget may be created before it. The date fields keep their explicit `yyyy-MM-dd`
  display format; English is en_US, so the calendars start the week on Sunday.
- `today` (the owner's day, in the owner's zone) is a required keyword argument of every
  formatter below; the window passes its injected clock's day.
- `weekday(day)`: "Mon". `month_day(day, *, today)`: "Oct 5"; when the year isn't
  `today`'s, "Jan 4, 2027". `day_text(day, *, today)`: "Mon Oct 5" or "Mon Jan 4, 2027".
- `moment_text(moment, zone, *, today)`: "Thu Oct 8, 17:00" in `zone`, the year as
  `day_text` adds it.
- `deadline_text(item, zone, *, today)`: DATETIME `moment_text` in the owner's zone,
  DATE `day_text` of the day the email names, UNRESOLVED the email's phrase in quotes,
  NONE None. The brief detail's deadline, suggestion meta lines, proposal effects ("Set
  the deadline to Mon Oct 5") and the Actions page all use it; a deadline in another year names it.
- Thread activity in the past week reads by weekday ("Tue 14:02", "Tue"); older, by
  `day_text` with the owner's day ("Sun Sep 20, 10:30", "Sun Dec 20, 2026, 15:30").
  Proposal rows show when the email arrived with `moment_text`.
- Everywhere else a date appears it is the same style, with the owner's day (so a day in
  another year names it): Today's coverage footer (`coverage_short` in `ui/workspace.py`:
  "Inbox on Oct 6, up to 09:14", "Inbox on Dec 31, 2025, up to 09:14"); the Briefs page's
  saved-brief rows ("Thu Sep 3 · Complete · 1 item · …"), missed days ("Fri Sep 4 · no
  brief") and the Replace sentence; MainWindow's "Viewing the brief for Thu Sep 3." banner
  and the "Showing the brief for …" and "the Inbox for …" status lines; Drafts' "updated
  Mon Sep 28, 10:05" and the draft editor's version times; the Preferences
  automatic-analysis line ("since Tue Sep 29"); the Data dialog's backup time ("Valid
  backup from Thu Oct 8, 17:00", in the owner's zone, set the way `cached_dialog.zone`
  is), and a completed action ("Completed Tue Oct 6").
- An action row's second line is only the completed day, target, deadline and steps; "Carried
  over" and "Source no longer in local mail" are the detail pane's, not the row's.

## Workspace (`ui/workspace.py`)

- `HeaderBar`: objectName `headerBar`, margins 14, 8, 14, 8; "MailBrief" 13px Medium, a
  stretch, a 14px refresh icon (text_secondary), the status and the window's widgets.
  The status is an `ElidedLabel` (12px, secondary, `headerStatus`): it asks for its text's
  width but shrinks to nothing, so a long status never widens the header; its accessible
  name is the whole text. `set_status(text)`; an empty status hides the refresh icon,
  which keeps its space. `add_widget(widget)` appends widgets right of the status,
  spacing 8.
- `SidebarNav`: objectName `sidebar`, fixed width `SIDEBAR_WIDTH`, margins 6, 10, 6, 10,
  spacing 2. A fixed-height `QListView#sidebarNav` with pages today (Today, sun), actions
  (Actions, checkbox), waiting (Waiting, hourglass), drafts (Drafts, pencil) and briefs
  (Briefs, calendar). Rows 31px: selected rows get a rounded selection fill (radius 8);
  a 14px icon at x 10 (text when selected, else text_secondary); a 13px label; the count
  right-aligned in text_secondary; the brief list's focus ring. Below: a stretch, then
  `NavButton` rows painted like page rows: `saved_mail` (Saved mail, mail, "Saved &mail"),
  `data` (Data, database, "&Data", accessible name "Data and recovery") and `settings`
  (Settings, settings, "Se&ttings"); an 11px muted note, hidden while empty; then
  `footer`, a QVBoxLayout (spacing 4) for the window's widgets. `page_requested(str)` (on
  selection, a click and Return/Enter; for `DIALOG_PAGES`, only a click or Return),
  `settings_requested()`, `set_counts(actions, waiting, drafts)` (None hides a count; a
  row with a count is read as "{label}, {count}", one without as its label),
  `set_current(key)`, `current_key()`, `set_note(text)`.
- `NavButton(text, icon_name, *, mnemonic=None, accessible_name=None)`: with a mnemonic
  (app-authored text such as "Se&ttings") Alt and that key press it; it still paints
  `text`.
- `BriefHeading` (`briefHeading`), at the top of the list pane, margins 12, 10, 12, 4,
  hidden until a brief is shown: `title` (13px Medium, `brief_title`: "Tue Oct 6", plus
  " · Partial" or " · Empty") and `meta` (11px muted, elided, `brief_meta`): "{account} ·
  saved HH:MM" ("saved Oct 7 HH:MM" when saved on a later day in the brief's zone), plus
  " · N failed", " · N deferred" and " · Inbox sync incomplete". The save time is in the
  brief's own zone, like its coverage footer, with that zone named in parentheses when the
  owner's zone would show a different time. Its accessible name is the full sentence:
  account, save time in the brief's zone, the coverage counts and whether the Inbox sync
  was complete.
- `ThreePaneWorkspace`: objectName `workspace`. HeaderBar, a horizontal divider, then
  SidebarNav, a vertical divider and `pages`, a QStackedWidget. Page `today`
  (`todayPage`): `today_top` (a QVBoxLayout for banners) above a `HairlineSplitter`
  holding the list pane (the heading, `BriefListView` and an 11px muted coverage footer,
  margins 12, 10, 12, 10: one elided line of `coverage_short(digest, today)`, with the
  services' `coverage_line` as its accessible name) and the `BriefDetailPane`; stretch
  4:5, not collapsible, minimum widths 220 and 280. `add_page(key, widget)`,
  `show_page(key)` (KeyError for an unknown key) and `current_page()`.
  `show_digest(digest, *, links, proposals, owner_zone, today)` builds the rows (chips
  counted from `today`), the heading and the coverage, shows "No analyzed messages in this
  brief." when empty, and, for the same saved brief (account, day and save time), keeps
  the selected email and the detail's scroll position. `set_today(today, owner_zone)`
  recounts the list's chips for a new day in place (`BriefListModel.update_rows`,
  `dataChanged`, no model reset) and rewrites the heading and the coverage footer, which
  name a year other than today's, so the selection, the detail pane and keyboard focus
  stay; nothing else in the brief depends on the day. `clear(message)` shows no brief.

## Main window (`ui/main_window.py`)

- Central widget: the workspace (stretch), a horizontal `HairlineDivider`, then
  `statusStrip` (margins 12, 6, 12, 6): the status (a 12px secondary `WrapLabel`) and an
  outline Undo. No outer scroll area; the window opens at 1100 × 720 and its minimum
  width stays at most 900 px with the theme applied.
- Header: after a run whose Inbox sync was complete or partial, when that sync finished
  (when the review opened, else when the run ended), in the owner's zone: "Checked Gmail
  at HH:MM" that day, "Checked Gmail Oct 7 at HH:MM" on a later one (`_render_checked`).
  Then outline Sync and review and Cancel. A one-minute `day_timer` calls `_minute_tick`:
  when the owner's day differs from the one the views were built for (`initialize` sets
  it), the check time gains its date, the brief's chips recount in place (`set_today`),
  and `_refresh_for_new_day` reloads the actions (their "overdue", "carried over" and
  weekday labels) and, when it shows, the Briefs page, through `start()`. While another
  operation runs, that reload waits (`_day_refresh_pending`) and is retried each tick.
  `closeEvent` stops the timer.
- Sidebar footer, top to bottom: Saved mail, Data, Settings, then the Gmail and AI lines
  (11px muted `WrapLabel`s, inset 10 px to line up with the row icons, read by their
  text), then outline Connect Gmail or Disconnect (12px): Connect shows only without a
  connected account, Disconnect only with one. Rows and outline buttons are muted while
  disabled.
- Cancel shows in the header only while an operation runs. `ThemeStyle`, Fusion behind a
  `QProxyStyle`, never underlines mnemonics; Alt and the letter still work.
- Pages: `today` (the "Viewing the brief for …" banner and the retry row in `today_top`),
  `run` (`runPage`, a scroll area, margins 16, 14, 16, 14, holding `runColumn`, a centred
  column at most 760 px wide), `actions` (the Actions page, no margins), `drafts` and
  `briefs` (the panels, margins 12). Today shows `run` while the review or consent
  waits. Actions opens the Open tab and Waiting the Waiting tab;
  changing the tab moves the sidebar. Briefs is a page like the others: choosing it loads
  the saved briefs and missed days through `start()` (refused with a reason while busy or
  before local storage loads), then shows `BriefHistoryPanel` (`ui/history_view.py`): a
  15px Medium "Briefs" heading, the saved and missed lists, outline Open and Brief this
  day…, and a Replace / Keep it confirmation, each button row ending in a stretch.
  Opening a saved brief returns to Today. An automatic refresh waits while a replacement
  is being confirmed there, and reloads Briefs when it ends.
- Drafts: with no drafts the list is hidden and `draftsEmpty`, a muted, centred, wrapping
  message, fills the page; with drafts the list shows and the message hides.
- Shortcuts: `QShortcut`s on the window, Ctrl+1 to Ctrl+5 (Cmd on macOS), call
  `_request_page` with today, actions, waiting, drafts and briefs (`PAGE_SHORTCUTS`,
  `page_shortcuts`), so Briefs refuses as the sidebar does.
- Tab order (`_link_tab_order`, at startup and on each `rebuilt`): Sync and review,
  Cancel, the sidebar's page list, Saved mail, Data, Settings, Connect or Disconnect, then
  the page (on Today: Back to latest, Retry, the brief list and the detail's buttons in
  display order), then Undo and back to Sync and review; hidden and disabled widgets are
  skipped. The chain is linked from Undo, so other pages' controls, in their own order,
  fall between Today's last button and Undo. Never walk the focus chain from Python
  (`nextInFocusChain()`, `previousInFocusChain()`): PySide re-parents the returned
  wrapper under the widget it was called on, and deleting that widget later invalidates
  the wrapper and all its children.

## Run page (`ui/run_view.py`, `ui/main_window.py`)

- Step 1, `review_panel`: "Step 1 of 2" (11px muted) over "Choose what MailBrief reads"
  (15px Medium), the hint (a secondary `WrapLabel`), the shortlist and Continue, a
  `variant="primary"` button left-aligned. Step 2, `consent_panel`: "Step 2 of 2" over
  "Approve sending to Groq", the disclosure (a `WrapLabel`) in a `HairlineFrame` card,
  then outline Approve and Decline side by side, Decline focused.
- The shortlist stays a `QListWidget`: each item keeps its text (what is read aloud), its
  ID under `UserRole`, flags, check state and app-text tooltip. `review()` adds a
  `ShortlistRow(subject, name, address, chips, blocked)` under `ROW_ROLE`, and
  `ShortlistDelegate` paints it with the brief list's row helpers: selection fill and 2px `accent_border`
  bar, focus ring only after keyboard focus, subject 13px Medium, sender 13px secondary
  (`sender_line`: the whole "Name <address>" when it fits; else the name cut at its end
  before " <address>"; else the address alone, cut by `cut_address`; a long display name
  never hides the address, and joined parts are re-measured, shrinking the cut part 1 px
  at a time, so the line never overflows). `cut_address` keeps the end of the domain,
  which says who sent it: the whole address; else the part before the "@" cut at its end
  and the whole domain ("bill…@paypal.com.secure-login.evil.example"); else "…@" and the
  domain cut at its start ("…@…login.evil.example"). Chips ("Tracked reply" and
  "Left out earlier" accent, "Excluded in Settings"
  warning). A cosmetic hairline separates rows; the last row has none, since the list's
  frame closes it.
- Height: once `review()` fills the list, its maximum height is its rows' `sizeHintForRow`
  plus twice the frame width. The shortlist has stretch 1 in `review_panel`, which has
  stretch 1 in the run column, and a stretch after Continue takes the height the list
  can't use: a short list shows no empty box, a long one fills the page and scrolls, and
  the page itself never scrolls at 1100 × 720.
- No horizontal scroll bar (`ScrollBarAlwaysOff`), and rows have no width of their own,
  so list mode gives them the viewport's width and no hidden horizontal range exists.
  Scrolling is per pixel.
- Check box: MailBrief paints its own inside the style's `SE_ItemViewItemCheckIndicator`
  rect, so the inherited `editorEvent` still handles clicks and Space. Unchecked: a
  radius-3 rounded square with a cosmetic 1px `border_strong` outline. Checked:
  `accent_bg` fill, `accent_border` outline and an antialiased `accent_fg` check mark.
  `indicator_colors(checked, tokens)` returns (border, fill, mark), with `NONE`
  ("transparent") for an unchecked box's fill and mark.
- A blocked row is muted, has no check box and can never be checked. Its check rect is
  computed as if it had one (`HasCheckIndicator` on a copied option), so its text starts
  at the same `text_left(option, index)` as a checkable row's.
- Automatic runs never show the run page.

## Buttons, lists and tabs

- `QPushButton[variant="primary"]`: `accent_bg` background, `accent_fg` text, 1px
  `accent_border`, the outline button's radius and padding; `:focus` border `accent_fg`;
  `:disabled` transparent with `text_muted` text and a `hairline` border, like outline.
- `QListWidget::item`: padding 4px 8px and a transparent 2px left border, so selected and
  unselected rows line up; `:selected` (also `:!active`): `selection` background, `text`,
  2px `accent_border` left border.
- `QListView#actionList` and `QScrollArea#actionDetail` share the brief list's and detail's
  panel background, no border and no outline.
- `QTabWidget::pane`: no border. `QTabBar::tab`: transparent, `text_secondary`, padding
  6px 12px, no border; `:selected`: `text` with a 2px `accent_border` bottom border.
- Briefs: the missed-days list hides when there are none; its note says why. An automatic
  refresh waits only while a replacement is being confirmed there, and reloads the page
  when it ends while Briefs shows.

## Stylesheet additions from the Phase 4 visual pass

- Outline buttons use `padding: 5px 14px` (the brief's 4px 10px looked small next to the
  mockup), with 14px icons.
- Scrollbars are thin token-coloured handles with no arrow buttons: 8px wide, handle
  `border_strong`, radius 3, transparent track.
- Focus rings in the painted lists show only after the keyboard moves focus
  (`State_KeyboardFocusChange`), not on first show.
- Suggestion cards: 4px between title and meta, 8px before each button row.
- Long mail-derived button text ("Add to …", a proposal's effect) goes through
  `short_button_label` (40 characters, cut at a grapheme boundary, then "…"), with the full text as the button's
  accessible name; each "Add to" button has its own row.
- Settings is a `_NavButton` painted like a page row (icon at `_ICON_X`, label at
  `_LABEL_X`), so it lines up with the pages.
- `register_fonts()` returns False instead of raising; `apply_theme` then keeps Qt's font
  and still applies the colours. `apply_theme` is all or nothing. On failure it puts back
  the stylesheet, style, palette and font it found, sets tokens from the restored palette
  (`palette_tokens`) and raises; `main()` logs the failure and runs unthemed.

## Tests

- Never assert a top-level window's requested size. CI's macOS screen is small (about
  1024 × 649 available) and Cocoa keeps windows inside it, so a window asked for
  1100 × 720 or 1600 × 900 gets less there; offscreen gets whatever it asks for. Derive
  expectations from the actual geometry or from the screen's available area
  (`QGuiApplication.primaryScreen().availableGeometry()`), and `pytest.skip` a behaviour
  that needs more room than the screen has. Never call `widget.screen()` in a test: PySide
  re-parents the shared QScreen wrapper under that widget, and deleting the widget
  invalidates it.
