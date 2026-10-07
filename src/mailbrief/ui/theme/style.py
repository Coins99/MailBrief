"""Fusion style, palette and stylesheet built from the design tokens."""

import dataclasses
import string

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QProxyStyle,
    QStyle,
    QStyleFactory,
    QStyleHintReturn,
    QStyleOption,
    QWidget,
)

from mailbrief.ui.theme.assets import register_fonts, release_icon_cache, ui_font
from mailbrief.ui.theme.tokens import DARK, TEXT_PX, ThemeMode, Tokens, tokens_for

QSS = """\
QWidget#workspace, QWidget#sidebar { background: $panel; }
QWidget#headerBar { background: $canvas; }
QLabel[tone="secondary"] { color: $text_secondary; }
QLabel[tone="muted"] { color: $text_muted; }
QLabel[tone="warning"] { color: $warning_fg; }
QPushButton[variant="outline"] { background: transparent; color: $text; border: 1px solid $border_strong; border-radius: 8px; padding: 5px 14px; }
QPushButton[variant="outline"]:hover { background: $selection; }
QPushButton[variant="outline"]:focus { border-color: $accent_fg; }
QPushButton[variant="outline"]:disabled { color: $text_muted; border-color: $hairline; }
QPushButton[variant="primary"] { background: $accent_bg; color: $accent_fg; border: 1px solid $accent_border; border-radius: 8px; padding: 5px 14px; }
QPushButton[variant="primary"]:focus { border-color: $accent_fg; }
QPushButton[variant="primary"]:disabled { background: transparent; color: $text_muted; border-color: $hairline; }
QPushButton[variant="nav"] { text-align: left; background: transparent; color: $text_secondary; border: none; border-radius: 8px; padding: 6px 10px; }
QPushButton[variant="nav"]:hover, QPushButton[variant="nav"]:focus { background: $selection; color: $text; }
QListView#sidebarNav, QListView#briefList { background: $panel; border: none; outline: 0; }
QScrollArea#briefDetail { background: $panel; border: none; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px 1px; }
QScrollBar:horizontal { background: transparent; height: 8px; margin: 1px 2px; }
QScrollBar::handle { background: $border_strong; border-radius: 3px; min-height: 24px; min-width: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
"""  # noqa: E501

_current = DARK
_cache_hooked = False


def current_tokens() -> Tokens:
    """The tokens of the applied theme; widgets read them at paint time."""
    return _current


def set_current_tokens(tokens: Tokens) -> None:
    global _current
    _current = tokens


def build_palette(t: Tokens) -> QPalette:
    palette = QPalette()
    role = QPalette.ColorRole
    # Pairs, not a dict keyed by colour: several tokens share a value.
    pairs: tuple[tuple[str, tuple[QPalette.ColorRole, ...]], ...] = (
        (t.panel, (role.Window, role.Base, role.Button)),
        (t.text, (role.WindowText, role.Text, role.ButtonText, role.BrightText)),
        (t.selection, (role.AlternateBase, role.Midlight)),
        (t.canvas, (role.ToolTipBase,)),
        (t.text, (role.ToolTipText,)),
        (t.text_muted, (role.PlaceholderText,)),
        (t.accent_border, (role.Highlight,)),
        (t.text, (role.HighlightedText,)),
        (t.accent_fg, (role.Accent, role.Link, role.LinkVisited)),
        (t.border_strong, (role.Light,)),
        (t.hairline, (role.Mid,)),
        (t.canvas, (role.Dark, role.Shadow)),
    )
    for color, targets in pairs:
        for target in targets:
            palette.setColor(target, QColor(color))
    for target in (role.WindowText, role.Text, role.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, target, QColor(t.text_muted))
    return palette


def build_stylesheet(t: Tokens) -> str:
    return string.Template(QSS).substitute(dataclasses.asdict(t))


class ThemeStyle(QProxyStyle):
    """Fusion without mnemonic underlines: Alt and the letter still press a button, but no
    letter is underlined, as on macOS."""

    def styleHint(
        self,
        hint: QStyle.StyleHint,
        option: QStyleOption | None = None,
        widget: QWidget | None = None,
        returnData: QStyleHintReturn | None = None,
    ) -> int:
        if hint == QStyle.StyleHint.SH_UnderlineShortcut:
            return 0
        return super().styleHint(hint, option, widget, returnData)


# The application owns its style; this reference keeps the Python object, and so its
# override, alive for as long as it is the application's style.
_style: ThemeStyle | None = None


def apply_theme(app: QApplication, mode: ThemeMode = ThemeMode.DARK) -> Tokens:
    """Apply Fusion (as ThemeStyle), the bundled font, the palette and the stylesheet for
    ``mode``.

    Without the bundled font the app keeps Qt's font; the colours still apply.
    """
    tokens = tokens_for(mode)
    # A stylesheet wraps the style in a proxy that hides its name; clear it first.
    app.setStyleSheet("")
    global _style
    if not isinstance(app.style(), ThemeStyle):
        _style = ThemeStyle(QStyleFactory.create("Fusion"))
        app.setStyle(_style)
    if register_fonts():
        app.setFont(ui_font(TEXT_PX))
    app.setPalette(build_palette(tokens))
    app.setStyleSheet(build_stylesheet(tokens))
    set_current_tokens(tokens)
    global _cache_hooked
    if not _cache_hooked:
        # Release the cached icon pixmaps while Qt can still free them.
        app.aboutToQuit.connect(release_icon_cache)
        _cache_hooked = True
    return tokens
