"""The run page's shortlist rows: subject, sender and chips, painted like the brief list.

Each shortlist item keeps its text, ID, flags, check state and tooltip; the window adds a
``ShortlistRow`` under ``ROW_ROLE`` for painting only. Mail text is drawn with
``drawText``, never as rich text or in a tooltip. The check indicator is drawn where the
style places it, so the inherited ``editorEvent`` handles clicks and Space; a blocked row
has no indicator and can never be checked.
"""

from dataclasses import dataclass
from typing import Final

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter
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
from mailbrief.ui.theme import TEXT_PX, current_tokens, ui_font

# The item's own ID stays under Qt.UserRole; the row to paint sits beside it.
ROW_ROLE: Final = Qt.ItemDataRole.UserRole + 1

TRACKED_REPLY: Final = "Tracked reply"
LEFT_OUT: Final = "Left out earlier"
EXCLUDED: Final = "Excluded in Settings"

_PAD_V: Final = 8
_PAD_H: Final = 12
_CHECK_GAP: Final = 10
_CHIP_GAP: Final = 4

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


class ShortlistDelegate(QStyledItemDelegate):
    """Paints shortlist rows; rows without a ``ShortlistRow`` paint as Qt would."""

    def check_rect(self, option: QStyleOptionViewItem, index: _Index) -> QRect:
        """Where the style puts this row's check indicator, which is also where the
        inherited ``editorEvent`` looks for clicks."""
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        view = self._view()
        return self._style().subElementRect(
            QStyle.SubElement.SE_ItemViewItemCheckIndicator, styled, view
        )

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
        check = self.check_rect(option, index)
        left = rect.left() + _PAD_H
        if not row.blocked and check.isValid():
            indicator = QStyleOptionViewItem(option)
            self.initStyleOption(indicator, index)
            indicator.rect = check
            checked = index.data(Qt.ItemDataRole.CheckStateRole)
            indicator.state = indicator.state & ~(
                QStyle.StateFlag.State_On | QStyle.StateFlag.State_Off
            )
            indicator.state |= (
                QStyle.StateFlag.State_On
                if checked == Qt.CheckState.Checked or checked == Qt.CheckState.Checked.value
                else QStyle.StateFlag.State_Off
            )
            self._style().drawPrimitive(
                QStyle.PrimitiveElement.PE_IndicatorItemViewItemCheck,
                indicator,
                painter,
                self._view(),
            )
            left = check.right() + _CHECK_GAP
        else:
            # Blocked rows line up with checkable ones, with nothing to check.
            left = max(left, check.right() + _CHECK_GAP) if check.isValid() else left
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
        return QSize(option.rect.width(), height)
