"""Safe text widgets: mail and AI text is always plain text, never rich text or a link."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from mailbrief.ui.theme.assets import ui_font


def plain_label(
    text: str = "", *, tone: str | None = None, px: int | None = None, medium: bool = False
) -> QLabel:
    """Mail-derived text must never become rich text or an automatic hyperlink."""
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    if tone is not None:
        label.setProperty("tone", tone)
    if px is not None:
        label.setFont(ui_font(px, medium=medium))
    return label


def button_label(text: str) -> str:
    """Escape ``&`` so mail text can't create a mnemonic or lose its ampersands."""
    return text.replace("&", "&&")
