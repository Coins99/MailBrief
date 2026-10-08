"""Safe text widgets: mail and AI text is always plain text, never rich text or a link."""

import math
from typing import Final

from PySide6.QtCore import QPointF, QSize, Qt, QTextBoundaryFinder
from PySide6.QtGui import (
    QFontMetricsF,
    QPainter,
    QPaintEvent,
    QPalette,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import QLabel, QSizePolicy

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


def wrap_label(
    text: str = "", *, tone: str | None = None, px: int | None = None, medium: bool = False
) -> "WrapLabel":
    """Like ``plain_label``, but a long unbroken token wraps instead of widening its
    container."""
    label = WrapLabel(text)
    if tone is not None:
        label.setProperty("tone", tone)
    if px is not None:
        label.setFont(ui_font(px, medium=medium))
    return label


def button_label(text: str) -> str:
    """Escape ``&`` so mail text can't create a mnemonic or lose its ampersands."""
    return text.replace("&", "&&")


_ELLIPSIS: Final = "…"


def cut_text(text: str, limit: int) -> str:
    """``text`` when it has at most ``limit`` characters; otherwise its start, cut at a
    grapheme boundary, then "…", at most ``limit`` characters in all. A cut never splits an
    emoji sequence, a flag or a letter from its accent."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if len(text) <= limit:
        return text
    # QTextBoundaryFinder counts UTF-16 code units; Python counts code points.
    units = [0]
    for character in text:
        units.append(units[-1] + (2 if ord(character) > 0xFFFF else 1))
    index_of = {unit: index for index, unit in enumerate(units)}
    finder = QTextBoundaryFinder(QTextBoundaryFinder.BoundaryType.Grapheme, text)
    cut = 0
    position = finder.toNextBoundary()
    while position != -1 and index_of[position] <= limit - 1:
        cut = index_of[position]
        position = finder.toNextBoundary()
    return text[:cut] + _ELLIPSIS


def short_button_label(text: str, limit: int = 40) -> str:
    """Button text of at most ``limit`` characters, on one line, escaped like
    ``button_label``. Give the button the full text as its accessible name."""
    return button_label(cut_text(" ".join(text.split()), limit))


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

    def setText(self, text: str, accessible_name: str | None = None) -> None:
        """Show ``text``; screen readers get ``accessible_name``, else the text itself."""
        super().setText(text)
        self.setAccessibleName(text if accessible_name is None else accessible_name)

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


# Average characters a wrapping label asks for, at most, before it wraps.
_WRAP_CHARS = 40


class WrapLabel(QLabel):
    """Plain text that wraps at word boundaries, or anywhere inside a token too long for
    its line, so no unbroken string (an address, a link, a long word) widens its
    container. Its height follows its width; its accessible name is the whole text; it
    has no tooltip and no links."""

    def __init__(self, text: str = "") -> None:
        super().__init__()
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        policy = self.sizePolicy()
        policy.setHeightForWidth(True)
        policy.setHorizontalPolicy(QSizePolicy.Policy.Preferred)
        self.setSizePolicy(policy)
        self.setText(text)

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setAccessibleName(text)
        self.updateGeometry()

    def _layout(self, width: float) -> tuple[QTextLayout, float, float]:
        """The text laid out at ``width``: the layout, its height and its widest line."""
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        # QTextLayout breaks lines at U+2028, not at a newline character.
        text = self.text().replace("\r\n", "\n").replace("\n", "\u2028")
        # Soft breaks after @ and /, so an address or a path wraps at its parts before
        # anywhere else; text() and the accessible name keep the text as it is.
        text = text.replace("@", "@\u200b").replace("/", "/\u200b")
        layout = QTextLayout(text, self.font())
        layout.setTextOption(option)
        layout.beginLayout()
        height = widest = 0.0
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(max(width, 1.0))
            line.setPosition(QPointF(0, height))
            height += line.height()
            widest = max(widest, line.naturalTextWidth())
        layout.endLayout()
        return layout, height, widest

    def _one_line(self) -> int:
        return math.ceil(QFontMetricsF(self.font()).height())

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        margins = self.contentsMargins()
        inner = width - margins.left() - margins.right()
        _layout, height, _widest = self._layout(inner)
        return max(math.ceil(height), self._one_line()) + margins.top() + margins.bottom()

    def sizeHint(self) -> QSize:
        margins = self.contentsMargins()
        limit = QFontMetricsF(self.font()).averageCharWidth() * _WRAP_CHARS
        _layout, _height, natural = self._layout(limit)
        width = math.ceil(min(natural, limit)) + margins.left() + margins.right()
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self) -> QSize:
        margins = self.contentsMargins()
        return QSize(0, self._one_line() + margins.top() + margins.bottom())

    def paintEvent(self, event: QPaintEvent) -> None:
        rect = self.contentsRect()
        layout, _height, _widest = self._layout(rect.width())
        painter = QPainter(self)
        # The palette's WindowText carries the stylesheet's tone colours.
        painter.setPen(self.palette().color(QPalette.ColorRole.WindowText))
        layout.draw(painter, QPointF(rect.topLeft()))
        painter.end()
