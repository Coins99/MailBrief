"""The three-pane workspace: header, sidebar, pages, brief list and detail pane."""

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Final
from zoneinfo import ZoneInfo

from PySide6.QtCore import (
    QModelIndex,
    QPersistentModelIndex,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFontMetricsF,
    QKeyEvent,
    QPainter,
    QPaintEvent,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from mailbrief.domain.actions import ActionProposal, ThreadLink
from mailbrief.domain.digests import DailyDigest, DigestItem, DigestStatus
from mailbrief.services.history import coverage_line, coverage_short
from mailbrief.ui.brief_detail import BriefDetailPane
from mailbrief.ui.brief_list import BriefListView, build_rows
from mailbrief.ui.deadline_text import day_text, moment_text
from mailbrief.ui.hairline import (
    HairlineDivider,
    HairlineSplitter,
    paint_focus_ring,
)
from mailbrief.ui.labels import ElidedLabel, button_label, plain_label
from mailbrief.ui.theme import (
    CAPTION_PX,
    RADIUS,
    SIDEBAR_WIDTH,
    SMALL_PX,
    TEXT_PX,
    current_tokens,
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


def brief_title(digest: DailyDigest, today: date | None = None) -> str:
    """The brief's day, such as "Tue Oct 6" (with the year when it isn't ``today``'s), and
    whether it is partial or empty."""
    title = day_text(digest.local_date, today)
    if digest.status is DigestStatus.PARTIAL:
        return f"{title} · Partial"
    if digest.status is DigestStatus.EMPTY:
        return f"{title} · Empty"
    return title


def brief_meta(digest: DailyDigest, zone: ZoneInfo, today: date | None = None) -> tuple[str, str]:
    """The brief's account, save time and coverage: a short line to show, and the full
    sentence for its accessible name.

    The save time is in the brief's own zone, like its coverage footer. When the owner's
    zone, ``zone``, would show another time, the brief's zone is named. The full sentence
    gives the save time in ``zone``, as ``moment_text`` writes it.
    """
    saved = digest.generated_at_utc.astimezone(ZoneInfo(digest.timezone_name))
    if saved.date() > digest.local_date:
        when = f"{saved:%b} {saved.day} {saved:%H:%M}"
    else:
        when = f"{saved:%H:%M}"
    # Compare offsets, not names: two names for one zone (Asia/Calcutta, Asia/Kolkata),
    # or zones that agree at that moment, show the same time and need no name.
    owner_offset = digest.generated_at_utc.astimezone(zone).utcoffset()
    named = "" if saved.utcoffset() == owner_offset else f" ({digest.timezone_name})"
    short = [f"{digest.account_id} · saved {when}{named}"]
    full = f"{digest.account_id}. Saved {moment_text(digest.generated_at_utc, zone, today)}."
    coverage = digest.coverage
    if coverage is not None:
        if coverage.failed:
            short.append(f"{coverage.failed} failed")
        if coverage.deferred:
            short.append(f"{coverage.deferred} deferred")
        if not coverage.sync_complete:
            short.append("Inbox sync incomplete")
        counts = (
            f"{coverage.analyzed} analyzed, {coverage.reused} reused, "
            f"{coverage.failed} failed, {coverage.skipped} skipped"
        )
        if coverage.deferred:
            counts += f", {coverage.deferred} deferred"
        sync = "complete" if coverage.sync_complete else "incomplete"
        full = f"{full} {counts}. Inbox sync {sync}."
    return " · ".join(short), full


class _StatusLabel(ElidedLabel):
    """Asks for its whole text's width, but can shrink to nothing: a long status elides
    instead of widening the window."""

    def sizeHint(self) -> QSize:
        margins = self.contentsMargins()
        # Elision measures fractional widths, so round up: a whole-pixel width can fall a
        # fraction short and elide a status that fits.
        text = " ".join(self.text().split())
        width = math.ceil(QFontMetricsF(self.font()).horizontalAdvance(text))
        return QSize(width + margins.left() + margins.right(), super().sizeHint().height())


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
        self.refresh_icon.setObjectName("headerRefreshIcon")
        self.refresh_icon.setAccessibleName("Refresh status")
        # Hidden without a status, but keeping its place, so the status never moves the
        # header's minimum width.
        policy = self.refresh_icon.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.refresh_icon.setSizePolicy(policy)
        layout.addWidget(self.refresh_icon)
        self.status = _StatusLabel(tone="secondary", px=SMALL_PX)
        self.status.setObjectName("headerStatus")
        layout.addWidget(self.status)
        # Widgets the window adds, right of the status: 2 + the layout's 6 makes 8.
        self._slots = QHBoxLayout()
        self._slots.setContentsMargins(2, 0, 0, 0)
        self._slots.setSpacing(8)
        layout.addLayout(self._slots)
        self.set_status("")

    def set_status(self, text: str) -> None:
        """Show ``text``; the refresh icon shows only beside a status."""
        self.status.setText(text)
        self.refresh_icon.setVisible(bool(text))

    def add_widget(self, widget: QWidget) -> None:
        """Append ``widget`` right of the status."""
        self._slots.addWidget(widget)


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
        paint_focus_ring(painter, rect, state, tokens.accent_fg)
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: _Index) -> QSize:
        return QSize(option.rect.width(), _NAV_ROW)


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


class NavButton(QPushButton):
    """A button painted like a page row, so its icon and label line up with the pages.

    ``mnemonic`` is app-authored button text with an ``&`` (such as "Se&ttings") so Alt and
    that key press it; the row still paints ``text``.
    """

    def __init__(
        self,
        text: str,
        icon_name: str,
        *,
        mnemonic: str | None = None,
        accessible_name: str | None = None,
    ) -> None:
        super().__init__(button_label(text) if mnemonic is None else mnemonic)
        self._text = text
        self._icon_name = icon_name
        self.setProperty("variant", "nav")
        self.setAccessibleName(text if accessible_name is None else accessible_name)
        self.setAutoDefault(False)
        self.setFixedHeight(_NAV_ROW)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _active(self) -> bool:
        return self.isEnabled() and (self.underMouse() or self.hasFocus() or self.isDown())

    def _color(self) -> str:
        """The icon and label colour: muted while disabled, full while hovered, focused or
        pressed, else secondary."""
        tokens = current_tokens()
        if not self.isEnabled():
            return tokens.text_muted
        return tokens.text if self._active() else tokens.text_secondary

    def paintEvent(self, event: QPaintEvent) -> None:
        tokens = current_tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        if self._active():  # Never while disabled.
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(tokens.selection))
            painter.drawRoundedRect(QRectF(rect), RADIUS, RADIUS)
        color = self._color()
        pixmap = icon_pixmap(self._icon_name, color, _ICON_PX, self.devicePixelRatioF())
        painter.drawPixmap(_ICON_X, (rect.height() - _ICON_PX) // 2, pixmap)
        painter.setFont(ui_font(TEXT_PX))
        painter.setPen(QColor(color))
        label = QRect(_LABEL_X, 0, rect.width() - _LABEL_X, rect.height())
        flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        painter.drawText(label, flags | int(Qt.TextFlag.TextSingleLine), self._text)
        painter.end()


class SidebarNav(QWidget):
    """``page_requested(key)`` on selection and on Return or Enter; ``settings_requested``.

    Below the pages: Saved mail, Data, Settings, the note, then ``footer`` for the window's
    own widgets."""

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
        self.saved_mail = NavButton("Saved mail", "mail", mnemonic="Saved &mail")
        self.saved_mail.setObjectName("savedMailButton")
        layout.addWidget(self.saved_mail)
        self.data = NavButton(
            "Data", "database", mnemonic="&Data", accessible_name="Data and recovery"
        )
        self.data.setObjectName("dataButton")
        layout.addWidget(self.data)
        self.settings = NavButton("Settings", "settings", mnemonic="Se&ttings")
        self.settings.setObjectName("settingsButton")
        self.settings.clicked.connect(self.settings_requested)
        layout.addWidget(self.settings)
        self.note = plain_label("", tone="muted", px=CAPTION_PX)
        self.note.setObjectName("sidebarNote")
        self.note.setAccessibleName("Connection")
        self.note.setContentsMargins(10, 4, 4, 0)
        self.note.hide()
        layout.addWidget(self.note)
        self.footer = QVBoxLayout()
        self.footer.setContentsMargins(0, 0, 0, 0)
        self.footer.setSpacing(4)
        layout.addLayout(self.footer)
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

    def current_key(self) -> str:
        return str(self.nav.currentIndex().data(KEY_ROLE))

    def set_counts(self, actions: int | None, waiting: int | None, drafts: int | None) -> None:
        """Show each count (None hides it); a screen reader reads "Actions, 5"."""
        for key, count in (("actions", actions), ("waiting", waiting), ("drafts", drafts)):
            item = self.pages.item(self._row(key))
            item.setData(count, COUNT_ROLE)
            label = item.text()
            item.setData(
                label if count is None else f"{label}, {count}", Qt.ItemDataRole.AccessibleTextRole
            )

    def set_note(self, text: str) -> None:
        self.note.setText(text)
        self.note.setVisible(bool(text))

    def count_text(self, key: str) -> str | None:
        count = self.pages.item(self._row(key)).data(COUNT_ROLE)
        return None if count is None else str(count)


class BriefHeading(QWidget):
    """The shown brief's day above the list, and its account, save time and coverage."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("briefHeading")
        self.setAccessibleName("Brief")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 4)
        layout.setSpacing(2)
        self.title = plain_label("", px=TEXT_PX, medium=True)
        self.title.setObjectName("briefHeadingTitle")
        layout.addWidget(self.title)
        self.meta = ElidedLabel(tone="muted", px=CAPTION_PX)
        self.meta.setObjectName("briefHeadingMeta")
        layout.addWidget(self.meta)

    def show_brief(self, digest: DailyDigest, zone: ZoneInfo, today: date) -> None:
        self.title.setText(brief_title(digest, today))
        self.meta.setText(*brief_meta(digest, zone, today))
        self.show()


class ThreePaneWorkspace(QWidget):
    """Header, sidebar and ``pages``: ``today`` holds ``today_top`` above the brief's list
    and detail; the window adds its own pages with ``add_page``."""

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
        self.pages = QStackedWidget()
        self.pages.setObjectName("workspacePages")
        self._pages: dict[str, QWidget] = {}
        today = QWidget()
        today.setObjectName("todayPage")
        today.setAccessibleName("Today")
        today_layout = QVBoxLayout(today)
        today_layout.setContentsMargins(0, 0, 0, 0)
        today_layout.setSpacing(0)
        self.today_top = QVBoxLayout()
        self.today_top.setContentsMargins(0, 0, 0, 0)
        self.today_top.setSpacing(0)
        today_layout.addLayout(self.today_top)
        self.splitter = HairlineSplitter(Qt.Orientation.Horizontal)
        list_pane = QWidget()
        list_pane.setObjectName("briefListPane")
        list_layout = QVBoxLayout(list_pane)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(0)
        self.heading = BriefHeading()
        self.heading.hide()
        list_layout.addWidget(self.heading)
        self.brief_list = BriefListView()
        list_layout.addWidget(self.brief_list, 1)
        self.coverage = ElidedLabel(tone="muted", px=CAPTION_PX)
        self.coverage.setContentsMargins(12, 10, 12, 10)
        list_layout.addWidget(self.coverage)
        list_pane.setMinimumWidth(220)
        self.detail = BriefDetailPane()
        self.detail.setMinimumWidth(280)
        # Page Up and Page Down in the list page through the selected email.
        self.brief_list.set_page_scroll(self.detail.verticalScrollBar())
        self.splitter.addWidget(list_pane)
        self.splitter.addWidget(self.detail)
        self.splitter.setStretchFactor(0, 4)
        self.splitter.setStretchFactor(1, 5)
        self.splitter.setChildrenCollapsible(False)
        today_layout.addWidget(self.splitter, 1)
        self.add_page("today", today)
        body.addWidget(self.pages, 1)
        outer.addLayout(body, 1)
        self._digest: DailyDigest | None = None
        self._links: Mapping[str, Sequence[ThreadLink]] = {}
        self._proposals: Mapping[str, Sequence[ActionProposal]] = {}
        # The detail's scroll position to restore once its new content has a range.
        self._pending_scroll: int | None = None
        # The owner's zone and day the brief was last shown for, to show it again on a new day.
        self._owner_zone: ZoneInfo | None = None
        self._today: date | None = None
        self.brief_list.item_selected.connect(self._show_item)
        self.detail.verticalScrollBar().rangeChanged.connect(self._restore_scroll)

    def add_page(self, key: str, widget: QWidget) -> None:
        self._pages[key] = widget
        self.pages.addWidget(widget)

    def show_page(self, key: str) -> None:
        """Show page ``key``; an unknown key raises KeyError."""
        self.pages.setCurrentWidget(self._pages[key])

    def current_page(self) -> str:
        current = self.pages.currentWidget()
        return next(key for key, widget in self._pages.items() if widget is current)

    def show_digest(
        self,
        digest: DailyDigest,
        *,
        links: Mapping[str, Sequence[ThreadLink]] | None = None,
        proposals: Mapping[str, Sequence[ActionProposal]] | None = None,
        owner_zone: ZoneInfo,
        today: date,
    ) -> None:
        """Show ``digest``, its deadline chips counted from ``today`` (the owner's current
        day). Showing the same saved brief again keeps the selected email and the detail's
        scroll position; another brief starts at its first email."""
        previous = self._digest
        same = previous is not None and _identity(previous) == _identity(digest)
        select = self.brief_list.selected_key() if same else None
        scroll = self.detail.verticalScrollBar().value() if same else 0
        self._digest = digest
        self._links = links or {}
        self._proposals = proposals or {}
        self._owner_zone, self._today = owner_zone, today
        self.heading.show_brief(digest, owner_zone, today)
        self.coverage.setText(coverage_short(digest), coverage_line(digest))
        if not digest.items:
            self.brief_list.show_rows([])
            self.detail.show_empty(EMPTY_BRIEF)
            return
        rows = build_rows(digest, self._proposals, today, owner_zone)
        self.brief_list.show_rows(rows, select=select)
        if same and scroll:
            # The new content gets its scroll range once it is laid out: from the next turn
            # of the event loop, or when the range changes, whichever has one first.
            self._pending_scroll = scroll
            QTimer.singleShot(0, self._restore_scroll)

    def set_today(self, today: date, owner_zone: ZoneInfo) -> None:
        """On a new day (or in a new zone), recount the list's deadline chips from
        ``today`` in ``owner_zone``, in place: the selection, the detail pane and keyboard
        focus stay as they are, and nothing is read from storage. Nothing else in the brief
        depends on the day."""
        digest = self._digest
        if digest is None or (today, owner_zone) == (self._today, self._owner_zone):
            return
        self._today, self._owner_zone = today, owner_zone
        if digest.items:
            self.brief_list.brief_model.update_rows(
                build_rows(digest, self._proposals, today, owner_zone)
            )

    def _restore_scroll(self) -> None:
        bar = self.detail.verticalScrollBar()
        if self._pending_scroll is not None and bar.maximum() > 0:
            bar.setValue(min(self._pending_scroll, bar.maximum()))
            self._pending_scroll = None

    def clear(self, message: str) -> None:
        """Show no brief, only ``message``."""
        self._digest = None
        self._links = {}
        self._proposals = {}
        self._pending_scroll = None
        self._owner_zone = self._today = None
        self.brief_list.show_rows([])
        self.coverage.setText("")
        self.heading.hide()
        self.detail.show_empty(message)

    def _show_item(self, item: DigestItem) -> None:
        self._pending_scroll = None  # Another email starts at the top.
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


def _identity(digest: DailyDigest) -> tuple[str, date, datetime]:
    return (digest.account_id, digest.local_date, digest.generated_at_utc)
