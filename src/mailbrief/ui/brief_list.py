"""The brief as a painted list: section headers, then one row per email with its chips.

Mail and AI text is drawn with ``drawText`` only, so it can never become rich text or a
link, and it never reaches a tooltip.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFontMetrics, QKeyEvent, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QListView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from mailbrief.domain.actions import ActionProposal, ProposalState
from mailbrief.domain.analysis import DeadlinePrecision, FollowUpKind
from mailbrief.domain.digests import SECTION_TITLES, DailyDigest, DigestItem
from mailbrief.domain.messages import EmailContact
from mailbrief.ui.hairline import hairline_pen
from mailbrief.ui.theme import CAPTION_PX, RADIUS, SMALL_PX, TEXT_PX, current_tokens, ui_font

NO_SUBJECT: Final = "(no subject)"
_UNRESOLVED_CHARS: Final = 24

# Item rows: padding, the chip row's gap, chip padding and spacing (logical px).
_PAD_V: Final = 8
_PAD_H: Final = 12
_CHIP_GAP: Final = 4
_CHIP_PAD_V: Final = 1
_CHIP_PAD_H: Final = 6
_CHIP_SPACING: Final = 6
# Header rows: left inset, space above and below the title.
_HEADER_ABOVE: Final = 12
_HEADER_BELOW: Final = 4
_ELIDE_MARGIN: Final = 24


class ChipTone(StrEnum):
    WARNING = "warning"
    ACCENT = "accent"


@dataclass(frozen=True)
class Chip:
    text: str
    tone: ChipTone


@dataclass(frozen=True)
class BriefRow:
    kind: Literal["header", "item"]
    title: str
    item: DigestItem | None
    sender: str
    chips: tuple[Chip, ...]


def sender_text(contact: EmailContact) -> str:
    """The sender's name, else the address."""
    return contact.name or contact.address


def deadline_chip(item: DigestItem, zone: ZoneInfo) -> Chip | None:
    """A short deadline in the digest's zone, or None when the email states none."""
    text: str | None = None
    if item.deadline_precision is DeadlinePrecision.DATETIME and item.deadline_at_utc:
        local = item.deadline_at_utc.astimezone(zone)
        text = f"Due {local:%a %H:%M}"
    elif item.deadline_precision is DeadlinePrecision.DATE and item.deadline_date:
        day = item.deadline_date
        text = f"Due {day:%b} {day.day}"
    elif item.deadline_precision is DeadlinePrecision.UNRESOLVED and item.deadline_text:
        phrase = item.deadline_text
        if len(phrase) > _UNRESOLVED_CHARS:
            phrase = phrase[: _UNRESOLVED_CHARS - 1] + "…"
        text = f"Due {phrase}"
    return None if text is None else Chip(text, ChipTone.WARNING)


def proposal_chip(proposals: Sequence[ActionProposal]) -> Chip | None:
    """What the first pending proposal would do, or None."""
    for proposal in proposals:
        if proposal.state is ProposalState.PENDING:
            text = (
                "Proposes a new deadline"
                if proposal.kind is FollowUpKind.NEW_DEADLINE
                else "Proposes completing an action"
            )
            return Chip(text, ChipTone.ACCENT)
    return None


def build_rows(
    digest: DailyDigest, proposals: Mapping[str, Sequence[ActionProposal]]
) -> list[BriefRow]:
    """A header row whenever the section changes, then each item in digest order."""
    zone = ZoneInfo(digest.timezone_name)
    rows: list[BriefRow] = []
    section = None
    for item in digest.items:
        if item.section is not section:
            section = item.section
            rows.append(BriefRow("header", SECTION_TITLES[section], None, "", ()))
        chips = tuple(
            chip
            for chip in (
                deadline_chip(item, zone),
                proposal_chip(proposals.get(item.message_key, ())),
            )
            if chip is not None
        )
        rows.append(
            BriefRow("item", item.subject or NO_SUBJECT, item, sender_text(item.sender), chips)
        )
    return rows


KIND_ROLE: Final = Qt.ItemDataRole.UserRole + 1
ROW_ROLE: Final = Qt.ItemDataRole.UserRole + 2

_Index = QModelIndex | QPersistentModelIndex
_ROOT: Final = QModelIndex()


class BriefListModel(QAbstractListModel):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._rows: list[BriefRow] = []

    def set_rows(self, rows: Sequence[BriefRow]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def rows(self) -> tuple[BriefRow, ...]:
        return tuple(self._rows)

    def rowCount(self, parent: _Index = _ROOT) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index: _Index, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return row.title
        if role == Qt.ItemDataRole.AccessibleTextRole:
            if row.kind == "header":
                return row.title
            return f"{row.title}, from {row.sender}" + "".join(
                f", {chip.text}" for chip in row.chips
            )
        if role == KIND_ROLE:
            return row.kind
        if role == ROW_ROLE:
            return row
        return None

    def flags(self, index: _Index) -> Qt.ItemFlag:
        if not index.isValid() or self._rows[index.row()].kind == "header":
            # Not even enabled: an enabled, unselectable row would still take focus.
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable


def _metrics(px: int, *, medium: bool = False) -> QFontMetrics:
    return QFontMetrics(ui_font(px, medium=medium))


def _chip_height() -> int:
    return _metrics(CAPTION_PX).height() + 2 * _CHIP_PAD_V


class BriefItemDelegate(QStyledItemDelegate):
    """Paints header and item rows with ``drawText`` only."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: _Index) -> None:
        row = index.data(ROW_ROLE)
        if not isinstance(row, BriefRow):
            return
        tokens = current_tokens()
        rect: QRect = option.rect
        state: QStyle.StateFlag = option.state
        painter.save()
        if row.kind == "header":
            font = ui_font(SMALL_PX)
            painter.setFont(font)
            painter.setPen(QColor(tokens.text_muted))
            height = QFontMetrics(font).height()
            target = QRect(rect.left() + _PAD_H, rect.top() + _HEADER_ABOVE, 0, height)
            target.setRight(rect.right() - _PAD_H)
            painter.drawText(target, _single_line(), row.title)
            painter.restore()
            return
        if state & QStyle.StateFlag.State_Selected:
            painter.fillRect(rect, QColor(tokens.selection))
            painter.fillRect(
                QRect(rect.left(), rect.top(), 2, rect.height()), QColor(tokens.accent_border)
            )
        width = max(0, rect.width() - _ELIDE_MARGIN)
        left = rect.left() + _PAD_H
        top = rect.top() + _PAD_V
        title_font = ui_font(TEXT_PX, medium=True)
        title_metrics = QFontMetrics(title_font)
        painter.setFont(title_font)
        painter.setPen(QColor(tokens.text))
        painter.drawText(
            QRect(left, top, width, title_metrics.height()),
            _single_line(),
            title_metrics.elidedText(_flat(row.title), Qt.TextElideMode.ElideRight, width),
        )
        top += title_metrics.height()
        sender_font = ui_font(TEXT_PX)
        sender_metrics = QFontMetrics(sender_font)
        painter.setFont(sender_font)
        painter.setPen(QColor(tokens.text_secondary))
        painter.drawText(
            QRect(left, top, width, sender_metrics.height()),
            _single_line(),
            sender_metrics.elidedText(_flat(row.sender), Qt.TextElideMode.ElideRight, width),
        )
        top += sender_metrics.height()
        if row.chips:
            top += _CHIP_GAP
            self._paint_chips(painter, row.chips, left, top)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setPen(hairline_pen(tokens.hairline))
        bottom = rect.bottom()
        painter.drawLine(QPointF(rect.left(), bottom), QPointF(rect.right(), bottom))
        if state & QStyle.StateFlag.State_HasFocus:
            painter.setPen(hairline_pen(tokens.accent_fg))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(rect).adjusted(1, 1, -1, -1))
        painter.restore()

    def _paint_chips(self, painter: QPainter, chips: Sequence[Chip], left: int, top: int) -> None:
        tokens = current_tokens()
        font = ui_font(CAPTION_PX)
        metrics = QFontMetrics(font)
        height = _chip_height()
        radius = min(RADIUS, height / 2)
        painter.setFont(font)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        x = left
        for chip in chips:
            width = metrics.horizontalAdvance(chip.text) + 2 * _CHIP_PAD_H
            warning = chip.tone is ChipTone.WARNING
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(tokens.warning_bg if warning else tokens.accent_bg))
            painter.drawRoundedRect(QRectF(x, top, width, height), radius, radius)
            painter.setPen(QColor(tokens.warning_fg if warning else tokens.accent_fg))
            painter.drawText(
                QRect(x, top, width, height),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextSingleLine,
                chip.text,
            )
            x += width + _CHIP_SPACING

    def sizeHint(self, option: QStyleOptionViewItem, index: _Index) -> QSize:
        row = index.data(ROW_ROLE)
        width: int = option.rect.width()
        if not isinstance(row, BriefRow):
            return QSize(width, 0)
        if row.kind == "header":
            return QSize(width, _HEADER_ABOVE + _metrics(SMALL_PX).height() + _HEADER_BELOW)
        height = (
            _PAD_V
            + _metrics(TEXT_PX, medium=True).height()
            + _metrics(TEXT_PX).height()
            + _PAD_V
            + 1  # The hairline.
        )
        if row.chips:
            height += _CHIP_GAP + _chip_height()
        return QSize(width, height)


def _single_line() -> int:
    return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter) | int(
        Qt.TextFlag.TextSingleLine
    )


def _flat(text: str) -> str:
    """One line: elision measures the string as drawn, so line breaks become spaces."""
    return " ".join(text.split())


class BriefListView(QListView):
    """``item_selected(DigestItem)`` reports the selected email."""

    item_selected = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("briefList")
        self.setAccessibleName("Brief items")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setUniformItemSizes(False)
        self.brief_model = BriefListModel(self)
        self.setModel(self.brief_model)
        self.setItemDelegate(BriefItemDelegate(self))
        self.selectionModel().currentChanged.connect(self._current_changed)

    def show_rows(self, rows: Sequence[BriefRow]) -> None:
        self.brief_model.set_rows(rows)
        for number, row in enumerate(rows):
            if row.kind == "item":
                self.setCurrentIndex(self.brief_model.index(number))
                break

    def _current_changed(self, current: QModelIndex, previous: QModelIndex) -> None:
        row = current.data(ROW_ROLE)
        if isinstance(row, BriefRow) and row.item is not None:
            self.item_selected.emit(row.item)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        index = self.currentIndex()
        if index.isValid() and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # Activate once and consume the key, as ActivatingList does (ui/lists.py).
            self.activated.emit(index)
            event.accept()
            return
        super().keyPressEvent(event)
