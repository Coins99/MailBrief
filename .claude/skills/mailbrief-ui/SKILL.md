---
name: mailbrief-ui
description: Use for any change to MailBrief's desktop UI under src/mailbrief/ui, including layout, styling, widgets, icons, fonts, the theme and the three-pane workspace. Gives the design tokens, Qt styling rules, plain-text safety rules and the screenshot review loop.
---

# MailBrief desktop UI

Read AGENTS.md first; its invariants win. Component specs and the token table are in
`references/spec.md`. The approved mockup is `docs/ui/mockup-three-pane-dark.png`.

## Rules
- Colours come only from `ui/theme/tokens.py`. Widgets read `current_tokens()` at paint
  time; never hard-code a hex colour anywhere else under `src/mailbrief/ui`.
- Mail and AI text reaches the screen only through `plain_label()` (`ui/labels.py`, plain
  text, word wrap), `wrap_label()` (plain text that also wraps inside a long unbroken
  token) or a delegate's `drawText()`. Never put it in a tooltip, and never use rich text
  or automatic links for it. Use `wrap_label()` wherever an address, link or long word
  could otherwise widen the layout.
- Outline buttons come only from `outline_button()` in ui/labels.py. Their text is
  escaped with `button_label()` unless it is app text carrying its own `&` mnemonic
  (`mnemonic=True`); long mail-derived text uses `shorten=True`.
- Fonts are bundled Inter (`ui_font(px, medium=...)`); sizes are in pixels, never points.
  Metrics: `TEXT_PX` 13, `SMALL_PX` 12, `CAPTION_PX` 11, `TITLE_PX` 15, `RADIUS` 8,
  `SIDEBAR_WIDTH` 128.
- Measure `ui_font()` text with `ui_metrics(px, medium=...)`, built once per size. `ui_font()`
  returns a copy of a cached font; both caches are cleared when the fonts are registered
  and when the app quits.
- Icons are bundled Tabler outlines, coloured at render time: `icon(name, color, px)` or
  `icon_pixmap(...)`. Add a new icon only with its SVG and an `ICON_NAMES` entry.
- Hairlines on cards and dividers are painted with cosmetic pens (`ui/hairline.py`:
  `HairlineFrame`, `HairlineDivider`, `HairlineSplitter`), never with QSS borders, so they
  stay one device pixel at every scale.
- Keep MainWindow's attribute names; tests and other modules use them.
- Every new widget gets an `objectName` and an accessible name. Rows a keyboard must skip
  (section headers) have `NoItemFlags`; Return and Enter activate a row once.
- Keep the Tab order in `MainWindow._link_tab_order` (see the spec), and never call
  `nextInFocusChain()` or `previousInFocusChain()` from Python: PySide re-parents the
  returned wrapper under the caller, and deleting the caller later invalidates it.
- No blocking network or database work on the Qt main thread. Add no dependencies.
- Bundled files resolve through `Path(__file__)` and are listed in
  `scripts/build_desktop.py`; `ui/smoke.py` checks they load in the package.

## Tests
- The offscreen platform ignores `setColorScheme`, so tests call `apply_theme`
  explicitly, and every test that calls it (directly or through `app.main`) takes the
  `themed` fixture from `tests/ui/conftest.py`, which restores the style, palette,
  stylesheet, font, tokens and the default `QLocale` (`app.main` sets it to English).
- On macOS, UI tests need `MACOS_GUI_AVAILABLE=1` (or a real session) and run offscreen
  with `QT_QPA_PLATFORM=offscreen`.
- Never assert a top-level window's requested size: CI's macOS screen is small (about
  1024 × 649 available), and Cocoa keeps windows inside it. Derive expectations from the
  actual geometry (a list shorter than its rows scrolls) or from the screen's available
  area, and skip a check that needs a wider screen than the one present. Get the screen
  with `QGuiApplication.primaryScreen()` or `screenAt()`, never `widget.screen()`, which
  re-parents the shared QScreen wrapper under the widget.
- Check colours with:
  `git grep -nE "#[0-9a-fA-F]{6}" src/mailbrief/ui ':!src/mailbrief/ui/theme/tokens.py'`

## Screenshot review loop
1. Render the gallery at 1x and 2x (PowerShell: set `$env:MAILBRIEF_UI_SHOTS` instead):
   ```
   MAILBRIEF_UI_SHOTS=scratch/ui-shots uv run pytest tests/ui/test_workspace.py -q --no-cov
   QT_SCALE_FACTOR=2 MAILBRIEF_UI_SHOTS=scratch/ui-shots uv run pytest tests/ui/test_workspace.py -q --no-cov
   ```
   Files are named `{size}-{mode}-{dpr}x.png`, sizes `mockup` (680×520) and `desktop`
   (1100×720). The Actions page renders at the desktop size as `page-actions-*`,
   `page-waiting-*` and `page-completed-dark-*`; compare them with Today's renders.
2. Compare `scratch/ui-shots/mockup-dark-2x.png` with the mockup. List differences in
   spacing, sizes, weights, colours and alignment; fix them; render again. At most three
   rounds per change.
3. Look at the light renders for anything hard to read.
4. Never commit `scratch/`.
