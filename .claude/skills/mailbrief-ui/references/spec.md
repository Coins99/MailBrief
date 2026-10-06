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
- `HairlineSplitter(QSplitter)`: `handleWidth(1)`; its handle fills with panel and draws a
  cosmetic hairline.

## Brief list (`ui/brief_list.py`)

- Rows: a header row (from `SECTION_TITLES`) whenever the digest section changes, then
  item rows in digest order. `BriefRow(kind, title, item, sender, chips)`.
- Sender: the contact's name, else its address.
- Deadline chip (WARNING), in the digest's zone: DATETIME `Due {local:%a %H:%M}`; DATE
  `Due {%b} {day}`; UNRESOLVED `Due ` + the phrase cut to 24 characters with "…"; NONE
  no chip.
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
- View: objectName `briefList`, accessible name "Brief items", NoFrame,
  ScrollPerPixel, SingleSelection; `item_selected(DigestItem)`; `show_rows(rows)` selects
  the first item row; Return and Enter emit `activated` once and are consumed.

## Detail pane (`ui/brief_detail.py`)

`BriefDetailPane(QScrollArea)`, objectName `briefDetail`, accessible name "Selected
email". Signals mirror `DigestView`: `suggestion_requested(str, int)`,
`accept_into_requested(int, str, int)`, `proposal_requested(str, int, int)`,
`reply_requested(str, str)`, plus `source_requested(str)`; it never opens URLs itself.
Content margins 16, 14, 16, 14, spacing 8, in order:
1. Subject (or "(no subject)"), 15px Medium.
2. Sender, secondary. No received time is invented.
3. Summary.
4. Action text, secondary, if present and different from the summary.
5. Deadline: 14px clock icon (warning_fg) and a warning label. DATETIME
   `Due {%a %b} {day}, {%H:%M}`; DATE `Due {%a %b} {day}`; UNRESOLVED `Due “{text}”`.
6. `Continues: “{title}”`, secondary, for each link that isn't a source.
7. Suggestions. PENDING: a `HairlineFrame` card with the title (13px Medium), a 12px
   secondary meta line ("Yours." or "Waiting for someone.", then the target with its
   reason, else the deadline, else "No deadline stated."), Accept and Dismiss, and one
   `Add to “{title}”` button per link. ACCEPTED: `Accepted: {title}`, secondary.
   DISMISSED: nothing.
8. Each pending proposal: a card with `Proposes for “{action_title}”: “{evidence}”`, an
   `effect_text` button (APPLY) and Dismiss.
9. Footer: "Draft a reply" (pencil) and, only for https links on mail.google.com,
   "Open in Gmail" (external-link).
10. A stretch.
Buttons: `button_label()` text, `variant="outline"`, `setAutoDefault(False)`.

## Workspace (`ui/workspace.py`)

- `HeaderBar`: objectName `headerBar`, margins 14, 8, 14, 8; "MailBrief" 13px Medium, a
  stretch, a 14px refresh icon (text_secondary) and a 12px secondary status label;
  `set_status(text)`.
- `SidebarNav`: objectName `sidebar`, fixed width `SIDEBAR_WIDTH`, margins 6, 10, 6, 10,
  spacing 2. A fixed-height `QListView#sidebarNav` with pages today (Today, sun), actions
  (Actions, checkbox), waiting (Waiting, hourglass), drafts (Drafts, pencil) and briefs
  (Briefs, calendar). Rows 31px: selected rows get a rounded selection fill (radius 8);
  a 14px icon at x 10 (text when selected, else text_secondary); a 13px label; the count
  right-aligned in text_secondary; the brief list's focus ring. Below: a stretch, a
  "Settings" nav button with the settings icon, and an 11px muted note.
  `page_requested(str)` (on selection and Return/Enter), `settings_requested()`,
  `set_counts(actions, waiting, drafts)` (None hides a count), `set_current(key)`,
  `set_note(text)`.
- `ThreePaneWorkspace`: objectName `workspace`. HeaderBar, a horizontal divider, then
  SidebarNav, a vertical divider and a `HairlineSplitter` holding the list pane
  (`BriefListView` and an 11px muted coverage line, margins 12, 10, 12, 10, drawn as one
  elided line by `ElidedLabel` with the full text as its accessible name) and the
  `BriefDetailPane`; stretch 4:5, not collapsible, minimum widths 220 and 280.
  Attributes `header`, `sidebar`, `brief_list`, `coverage`, `detail`. `show_digest`
  builds the rows and `coverage_line`, shows the selected item's links and proposals, and
  shows "No analyzed messages in this brief." when empty. MainWindow doesn't use it yet.

## Stylesheet additions from the Phase 4 visual pass

- Outline buttons use `padding: 5px 14px` (the brief's 4px 10px looked small next to the
  mockup), with 14px icons.
- Scrollbars are thin token-coloured handles with no arrow buttons: 8px wide, handle
  `border_strong`, radius 3, transparent track.
- Focus rings in the painted lists show only after the keyboard moves focus
  (`State_KeyboardFocusChange`), not on first show.
- Suggestion cards: 4px between title and meta, 8px before each button row.
