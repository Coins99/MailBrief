"""The three-pane workspace: header, sidebar, brief list and detail pane.

A preview: MainWindow doesn't use it yet.
"""

from collections.abc import Mapping, Sequence
from typing import Final

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QKeyEvent, QPainter, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from mailbrief.domain.actions import ActionProposal, ThreadLink
from mailbrief.domain.digests import DailyDigest, DigestItem
from mailbrief.services.history import coverage_line
from mailbrief.ui.brief_detail import BriefDetailPane
from mailbrief.ui.brief_list import BriefListView, build_rows
from mailbrief.ui.hairline import HairlineDivider, HairlineSplitter, hairline_pen
from mailbrief.ui.labels import ElidedLabel, button_label, plain_label
from mailbrief.ui.theme import (
    CAPTION_PX,
    RADIUS,
    SIDEBAR_WIDTH,
    SMALL_PX,
    TEXT_PX,
    current_tokens,
    icon,
    icon_pixmap,
    ui_font,
)

PAGES: Final = (
    ("today", "Today", "sun"),
    ("actions", "Actions", "checkbox"),
    ("waiting", "Waiting", "hourglass"),
    ("drafts", "Drafts", "pencil"),
    ("briefs", "Briefs", "calendar"),
)
EMPTY_BRIEF: Final = "No analyzed messages in this brief."

KEY_ROLE: Final = Qt.ItemDataRole.UserRole + 1
ICON_ROLE: Final = Qt.ItemDataRole.UserRole + 2
COUNT_ROLE: Final = Qt.ItemDataRole.UserRole + 3

_NAV_ROW: Final = 31
_ICON_PX: Final = 14
_ICON_X: Final = 10
_LABEL_X: Final = _ICON_X + _ICON_PX + 10

_Index = QModelIndex | QPersistentModelIndex


def _styled(widget: QWidget, name: str) -> None:
    """Name a plain QWidget so the stylesheet's background applies to it."""
    widget.setObjectName(name)
    widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)


class HeaderBar(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        _styled(self, "headerBar")
        self.setAccessibleName("MailBrief status")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(6)
        title = plain_label("MailBrief", px=TEXT_PX, medium=True)
        title.setWordWrap(False)
        layout.addWidget(title)
        layout.addStretch(1)
        self.refresh_icon = QLabel()
        self.refresh_icon.setPixmap(
            icon_pixmap("refresh", current_tokens().text_secondary, _ICON_PX, 2.0)
        )
        self.refresh_icon.setAccessibleName("Refresh status")
        layout.addWidget(self.refresh_icon)
        self.status = plain_label("", tone="secondary", px=SMALL_PX)
        self.status.setWordWrap(False)
        self.status.setAccessibleName("Status")
        layout.addWidget(self.status)

    def set_status(self, text: str) -> None:
        self.status.setText(text)


class _NavDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: _Index) -> None:
        tokens = current_tokens()
        rect: QRect = option.rect
        state = option.state
        selected = bool(state & QStyle.StateFlag.State_Selected)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(tokens.selection))
            painter.drawRoundedRect(QRectF(rect), RADIUS, RADIUS)
        name = index.data(ICON_ROLE)
        color = tokens.text if selected else tokens.text_secondary
        if isinstance(name, str):
            pixmap = icon_pixmap(name, color, _ICON_PX, painter.device().devicePixelRatioF())
            top = rect.top() + (rect.height() - _ICON_PX) // 2
            painter.drawPixmap(rect.left() + _ICON_X, top, pixmap)
        font = ui_font(TEXT_PX)
        painter.setFont(font)
        painter.setPen(QColor(tokens.text if selected else tokens.text_secondary))
        flags = int(Qt.AlignmentFlag.AlignVCenter) | int(Qt.TextFlag.TextSingleLine)
        label = QRect(
            rect.left() + _LABEL_X, rect.top(), rect.width() - _LABEL_X - 10, rect.height()
        )
        painter.drawText(label, flags | int(Qt.AlignmentFlag.AlignLeft), str(index.data()))
        count = index.data(COUNT_ROLE)
        if isinstance(count, int):
            painter.setPen(QColor(tokens.text_secondary))
            painter.drawText(label, flags | int(Qt.AlignmentFlag.AlignRight), str(count))
        if _keyboard_focus(state):
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            painter.setPen(hairline_pen(tokens.accent_fg))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(rect).adjusted(1, 1, -1, -1))
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: _Index) -> QSize:
        return QSize(option.rect.width(), _NAV_ROW)


def _keyboard_focus(state: QStyle.StateFlag) -> bool:
    """The focus ring shows once the keyboard has moved focus, not on first show."""
    return bool(
        state & QStyle.StateFlag.State_HasFocus
        and state & QStyle.StateFlag.State_KeyboardFocusChange
    )


class _NavList(QListView):
    """Return and Enter request the current page once and are consumed."""

    page_activated = Signal(str)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        index = self.currentIndex()
        if index.isValid() and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.page_activated.emit(str(index.data(KEY_ROLE)))
            event.accept()
            return
        super().keyPressEvent(event)


class SidebarNav(QWidget):
    """``page_requested(key)`` on selection and on Return or Enter; ``settings_requested``."""

    page_requested = Signal(str)
    settings_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        _styled(self, "sidebar")
        self.setAccessibleName("Pages")
        self.setFixedWidth(SIDEBAR_WIDTH)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 10, 6, 10)
        layout.setSpacing(2)
        self.pages = QStandardItemModel(self)
        for key, label, icon_name in PAGES:
            item = QStandardItem(label)
            item.setData(key, KEY_ROLE)
            item.setData(icon_name, ICON_ROLE)
            item.setData(label, Qt.ItemDataRole.AccessibleTextRole)
            item.setEditable(False)
            self.pages.appendRow(item)
        self.nav = _NavList()
        self.nav.setObjectName("sidebarNav")
        self.nav.setAccessibleName("Pages")
        self.nav.setModel(self.pages)
        self.nav.setItemDelegate(_NavDelegate(self.nav))
        self.nav.setFrameShape(QFrame.Shape.NoFrame)
        self.nav.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.nav.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.nav.setFixedHeight(_NAV_ROW * len(PAGES))
        self.nav.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._quiet = False
        self.nav.selectionModel().currentChanged.connect(self._current_changed)
        self.nav.page_activated.connect(self.page_requested)
        layout.addWidget(self.nav)
        layout.addStretch(1)
        self.settings = QPushButton(button_label("Settings"))
        self.settings.setProperty("variant", "nav")
        self.settings.setAutoDefault(False)
        self.settings.setIcon(icon("settings", current_tokens().text_secondary, _ICON_PX))
        self.settings.setIconSize(QSize(_ICON_PX, _ICON_PX))
        self.settings.clicked.connect(self.settings_requested)
        layout.addWidget(self.settings)
        self.note = plain_label("", tone="muted", px=CAPTION_PX)
        self.note.setAccessibleName("Connection")
        self.note.setContentsMargins(10, 4, 4, 0)
        layout.addWidget(self.note)
        self.set_current("today")

    def _row(self, key: str) -> int:
        return next(number for number, page in enumerate(PAGES) if page[0] == key)

    def _current_changed(self, current: QModelIndex, previous: QModelIndex) -> None:
        if not self._quiet and current.isValid():
            self.page_requested.emit(str(current.data(KEY_ROLE)))

    def set_current(self, key: str) -> None:
        """Show ``key`` as the current page without requesting it."""
        self._quiet = True
        try:
            self.nav.setCurrentIndex(self.pages.index(self._row(key), 0))
        finally:
            self._quiet = False

    def set_counts(self, actions: int | None, waiting: int | None, drafts: int | None) -> None:
        for key, count in (("actions", actions), ("waiting", waiting), ("drafts", drafts)):
            self.pages.item(self._row(key)).setData(count, COUNT_ROLE)

    def set_note(self, text: str) -> None:
        self.note.setText(text)

    def count_text(self, key: str) -> str | None:
        count = self.pages.item(self._row(key)).data(COUNT_ROLE)
        return None if count is None else str(count)


class ThreePaneWorkspace(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        _styled(self, "workspace")
        self.setAccessibleName("MailBrief workspace")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.header = HeaderBar()
        outer.addWidget(self.header)
        outer.addWidget(HairlineDivider(Qt.Orientation.Horizontal))
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.sidebar = SidebarNav()
        body.addWidget(self.sidebar)
        body.addWidget(HairlineDivider(Qt.Orientation.Vertical))
        self.splitter = HairlineSplitter(Qt.Orientation.Horizontal)
        list_pane = QWidget()
        list_pane.setObjectName("briefListPane")
        list_layout = QVBoxLayout(list_pane)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(0)
        self.brief_list = BriefListView()
        list_layout.addWidget(self.brief_list, 1)
        self.coverage = ElidedLabel(tone="muted", px=CAPTION_PX)
        self.coverage.setContentsMargins(12, 10, 12, 10)
        list_layout.addWidget(self.coverage)
        list_pane.setMinimumWidth(220)
        self.detail = BriefDetailPane()
        self.detail.setMinimumWidth(280)
        self.splitter.addWidget(list_pane)
        self.splitter.addWidget(self.detail)
        self.splitter.setStretchFactor(0, 4)
        self.splitter.setStretchFactor(1, 5)
        self.splitter.setChildrenCollapsible(False)
        body.addWidget(self.splitter, 1)
        outer.addLayout(body, 1)
        self._digest: DailyDigest | None = None
        self._links: Mapping[str, Sequence[ThreadLink]] = {}
        self._proposals: Mapping[str, Sequence[ActionProposal]] = {}
        self.brief_list.item_selected.connect(self._show_item)

    def show_digest(
        self,
        digest: DailyDigest,
        *,
        links: Mapping[str, Sequence[ThreadLink]] | None = None,
        proposals: Mapping[str, Sequence[ActionProposal]] | None = None,
    ) -> None:
        self._digest = digest
        self._links = links or {}
        self._proposals = proposals or {}
        self.coverage.setText(coverage_line(digest))
        if not digest.items:
            self.brief_list.show_rows([])
            self.detail.show_empty(EMPTY_BRIEF)
            return
        self.brief_list.show_rows(build_rows(digest, self._proposals))

    def _show_item(self, item: DigestItem) -> None:
        digest = self._digest
        if digest is None:
            return
        self.detail.show_item(
            item,
            account_email=digest.account_id,
            timezone_name=digest.timezone_name,
            links=self._links.get(item.message_key, ()),
            proposals=self._proposals.get(item.message_key, ()),
        )
