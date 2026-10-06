"""Hairlines painted with cosmetic pens: one device pixel at every scale, unlike QSS borders,
which round to whole logical pixels and blur at fractional scales."""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QFrame, QSplitter, QSplitterHandle, QWidget

from mailbrief.ui.theme import RADIUS, current_tokens


def hairline_pen(color: str) -> QPen:
    """A cosmetic pen: width 0 is always one device pixel."""
    pen = QPen(QColor(color), 0)
    pen.setCosmetic(True)
    return pen


class HairlineFrame(QFrame):
    """A card: a rounded hairline border around its contents."""

    def __init__(
        self, parent: QWidget | None = None, *, radius: int = RADIUS, strong: bool = False
    ) -> None:
        super().__init__(parent)
        self._radius = radius
        self._strong = strong
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setContentsMargins(12, 10, 12, 10)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        tokens = current_tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(hairline_pen(tokens.border_strong if self._strong else tokens.hairline))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # Half a device pixel puts the line on pixel centres, so it stays crisp.
        inset = 0.5 / self.devicePixelRatioF()
        rect = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
        painter.drawRoundedRect(rect, self._radius, self._radius)
        painter.end()


class HairlineDivider(QWidget):
    """A one-pixel line between panes."""

    def __init__(
        self,
        orientation: Qt.Orientation = Qt.Orientation.Horizontal,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._orientation = orientation
        if orientation is Qt.Orientation.Horizontal:
            self.setFixedHeight(1)
        else:
            self.setFixedWidth(1)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setPen(hairline_pen(current_tokens().hairline))
        if self._orientation is Qt.Orientation.Horizontal:
            painter.drawLine(QPointF(0, 0), QPointF(self.width(), 0))
        else:
            painter.drawLine(QPointF(0, 0), QPointF(0, self.height()))
        painter.end()


class _HairlineHandle(QSplitterHandle):
    def paintEvent(self, event: QPaintEvent) -> None:
        tokens = current_tokens()
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(tokens.panel))
        painter.setPen(hairline_pen(tokens.hairline))
        if self.orientation() is Qt.Orientation.Horizontal:
            painter.drawLine(QPointF(0, 0), QPointF(0, self.height()))
        else:
            painter.drawLine(QPointF(0, 0), QPointF(self.width(), 0))
        painter.end()


class HairlineSplitter(QSplitter):
    """A splitter whose handle is a single hairline."""

    def __init__(
        self,
        orientation: Qt.Orientation = Qt.Orientation.Horizontal,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(orientation, parent)
        self.setHandleWidth(1)

    def createHandle(self) -> QSplitterHandle:
        return _HairlineHandle(self.orientation(), self)
