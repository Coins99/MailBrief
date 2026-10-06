"""Safe text widgets: mail and AI text is always plain text, never rich text or a link."""

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPainter, QPaintEvent, QPalette
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


class ElidedLabel(QLabel):
    """One line of plain text, elided at the right to fit; the full text stays the
    accessible name, never a tooltip."""

    def __init__(self, text: str = "", *, tone: str | None = None, px: int | None = None) -> None:
        super().__init__()
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(False)
        if tone is not None:
            self.setProperty("tone", tone)
        if px is not None:
            self.setFont(ui_font(px))
        self.setText(text)

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setAccessibleName(text)

    def sizeHint(self) -> QSize:
        # Its width never drives a layout: the text elides to whatever width it gets.
        return QSize(0, super().sizeHint().height())

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def paintEvent(self, event: QPaintEvent) -> None:
        rect = self.contentsRect()
        painter = QPainter(self)
        text = " ".join(self.text().split())
        elided = self.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, rect.width())
        self.style().drawItemText(
            painter,
            rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            self.palette(),
            self.isEnabled(),
            elided,
            QPalette.ColorRole.WindowText,
        )
        painter.end()
