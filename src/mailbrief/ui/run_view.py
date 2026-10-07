"""The run page's shortlist rows: subject, sender and chips, painted like the brief list.

Each shortlist item keeps its text, ID, flags, check state and tooltip; the window adds a
``ShortlistRow`` under ``ROW_ROLE`` for painting only. Mail text is drawn with
``drawText``, never as rich text or in a tooltip. MailBrief paints its own check box inside
the rect the style gives the indicator, so the inherited ``editorEvent`` still handles
clicks and Space; a blocked row has no check box and can never be checked, but its text
starts where a checkable row's does.
"""

from dataclasses import dataclass
from typing import Final

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from mailbrief.domain.messages import RankedMessage
from mailbrief.ui.brief_list import (
    NO_SUBJECT,
    Chip,
    ChipTone,
    chip_height,
    paint_chips,
    sender_text,
)
from mailbrief.ui.hairline import hairline_pen
from mailbrief.ui.theme import TEXT_PX, Tokens, current_tokens, ui_font

# The item's own ID stays under Qt.UserRole; the row to paint sits beside it.
ROW_ROLE: Final = Qt.ItemDataRole.UserRole + 1

TRACKED_REPLY: Final = "Tracked reply"
LEFT_OUT: Final = "Left out earlier"
EXCLUDED: Final = "Excluded in Settings"

_PAD_V: Final = 8
_PAD_H: Final = 12
_CHECK_GAP: Final = 10
_CHIP_GAP: Final = 4
# The check box: its corner radius and the check mark's stroke (logical px).
_BOX_RADIUS: Final = 3
_MARK_WIDTH: Final = 1.5
# No fill and no mark: an unchecked box is only its outline.
NONE: Final = "transparent"

_Index = QModelIndex | QPersistentModelIndex


@dataclass(frozen=True)
class ShortlistRow:
    subject: str
    sender: str
    chips: tuple[Chip, ...]
    blocked: bool


def shortlist_row(
    ranked: RankedMessage, *, blocked: bool, outside: bool, declined: bool
) -> ShortlistRow:
    message = ranked.message
    chips: list[Chip] = []
    if outside:
        chips.append(Chip(TRACKED_REPLY, ChipTone.ACCENT))
    if declined:
        chips.append(Chip(LEFT_OUT, ChipTone.ACCENT))
    if blocked:
        chips.append(Chip(EXCLUDED, ChipTone.WARNING))
    return ShortlistRow(
        subject=message.subject or NO_SUBJECT,
        sender=sender_text(message.sender),
        chips=tuple(chips),
        blocked=blocked,
    )


def _metrics(px: int, *, medium: bool = False) -> QFontMetrics:
    return QFontMetrics(ui_font(px, medium=medium))


def _single_line() -> int:
    return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter) | int(
        Qt.TextFlag.TextSingleLine
    )


def _flat(text: str) -> str:
    return " ".join(text.split())


def indicator_colors(checked: bool, tokens: Tokens) -> tuple[str, str, str]:
    """The check box's (border, fill, mark): an outlined square when unchecked, an accent
    square with a check mark when checked."""
    if checked:
        return tokens.accent_border, tokens.accent_bg, tokens.accent_fg
    return tokens.border_strong, NONE, NONE


def paint_indicator(painter: QPainter, rect: QRect, *, checked: bool) -> None:
    """A rounded check box in ``rect`` with a one-device-pixel outline, and a check mark
    when ``checked``."""
    border, fill, mark = indicator_colors(checked, current_tokens())
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    # Half a device pixel puts the outline on pixel centres, so it stays crisp.
    inset = 0.5 / painter.device().devicePixelRatioF()
    box = QRectF(rect).adjusted(inset, inset, -inset, -inset)
    painter.setPen(hairline_pen(border))
    painter.setBrush(QColor(fill))
    painter.drawRoundedRect(box, _BOX_RADIUS, _BOX_RADIUS)
    if checked:
        pen = QPen(QColor(mark), _MARK_WIDTH)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        left, top, width, height = box.left(), box.top(), box.width(), box.height()
        painter.drawPolyline(
            [
                QPointF(left + 0.26 * width, top + 0.52 * height),
                QPointF(left + 0.43 * width, top + 0.69 * height),
                QPointF(left + 0.75 * width, top + 0.33 * height),
            ]
        )
    painter.restore()


class ShortlistDelegate(QStyledItemDelegate):
    """Paints shortlist rows; rows without a ``ShortlistRow`` paint as Qt would."""

    def check_rect(self, option: QStyleOptionViewItem, index: _Index) -> QRect:
        """Where the style puts this row's check indicator, which is also where the
        inherited ``editorEvent`` looks for clicks. A blocked row has no indicator, so its
        rect is computed as if it had one."""
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        styled.features |= QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
        view = self._view()
        return self._style().subElementRect(
            QStyle.SubElement.SE_ItemViewItemCheckIndicator, styled, view
        )

    def text_left(self, option: QStyleOptionViewItem, index: _Index) -> int:
        """Where the row's text starts: after the check box, blocked or not."""
        check = self.check_rect(option, index)
        rect: QRect = option.rect
        return check.right() + _CHECK_GAP if check.isValid() else rect.left() + _PAD_H

    def _view(self) -> QWidget | None:
        parent = self.parent()
        return parent if isinstance(parent, QWidget) else None

    def _style(self) -> QStyle:
        view = self._view()
        return view.style() if view is not None else QApplication.style()

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: _Index) -> None:
        row = index.data(ROW_ROLE)
        if not isinstance(row, ShortlistRow):
            super().paint(painter, option, index)
            return
        tokens = current_tokens()
        rect: QRect = option.rect
        state: QStyle.StateFlag = option.state
        painter.save()
        if state & QStyle.StateFlag.State_Selected:
            painter.fillRect(rect, QColor(tokens.selection))
            painter.fillRect(
                QRect(rect.left(), rect.top(), 2, rect.height()), QColor(tokens.accent_border)
            )
        if not row.blocked:
            checked = index.data(Qt.ItemDataRole.CheckStateRole)
            paint_indicator(
                painter,
                self.check_rect(option, index),
                checked=checked == Qt.CheckState.Checked or checked == Qt.CheckState.Checked.value,
            )
        # Blocked rows line up with checkable ones, with nothing to check.
        left = self.text_left(option, index)
        width = max(0, rect.right() - _PAD_H - left)
        top = rect.top() + _PAD_V
        title_font = ui_font(TEXT_PX, medium=True)
        title_metrics = QFontMetrics(title_font)
        painter.setFont(title_font)
        painter.setPen(QColor(tokens.text_muted if row.blocked else tokens.text))
        painter.drawText(
            QRect(left, top, width, title_metrics.height()),
            _single_line(),
            title_metrics.elidedText(_flat(row.subject), Qt.TextElideMode.ElideRight, width),
        )
        top += title_metrics.height()
        sender_font = ui_font(TEXT_PX)
        sender_metrics = QFontMetrics(sender_font)
        painter.setFont(sender_font)
        painter.setPen(QColor(tokens.text_muted if row.blocked else tokens.text_secondary))
        painter.drawText(
            QRect(left, top, width, sender_metrics.height()),
            _single_line(),
            sender_metrics.elidedText(_flat(row.sender), Qt.TextElideMode.ElideRight, width),
        )
        top += sender_metrics.height()
        if row.chips:
            paint_chips(painter, row.chips, left, top + _CHIP_GAP)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        if index.row() < index.model().rowCount() - 1:  # The list's frame closes the last row.
            painter.setPen(hairline_pen(tokens.hairline))
            bottom = rect.bottom()
            painter.drawLine(QPointF(rect.left(), bottom), QPointF(rect.right(), bottom))
        if state & QStyle.StateFlag.State_HasFocus and (
            state & QStyle.StateFlag.State_KeyboardFocusChange
        ):
            painter.setPen(hairline_pen(tokens.accent_fg))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(rect).adjusted(1, 1, -1, -1))
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: _Index) -> QSize:
        row = index.data(ROW_ROLE)
        if not isinstance(row, ShortlistRow):
            return super().sizeHint(option, index)
        height = (
            _PAD_V
            + _metrics(TEXT_PX, medium=True).height()
            + _metrics(TEXT_PX).height()
            + _PAD_V
            + 1  # The hairline.
        )
        if row.chips:
            height += _CHIP_GAP + chip_height()
        # No width of its own: in list mode a row takes the viewport's width, so it never
        # outgrows the viewport when the vertical scroll bar appears.
        return QSize(0, height)
