"""The owner's drafts and notes, newest first, with New, Open and Delete.

Titles are the owner's text or, for replies, an email's subject, so rows are plain list-item
text and never tooltips, which Qt may render as rich text.
"""

from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from mailbrief.domain.drafts import KIND_NAMES, DraftKind, DraftSummary

NEW = "new"
OPEN = "open"
DELETE = "delete"
NEW_KINDS = (DraftKind.EMAIL, DraftKind.NOTE, DraftKind.MESSAGE)


def describe(summary: DraftSummary, zone: ZoneInfo) -> str:
    """One plain line: kind, title, when it changed, what is left to fill, and its action."""
    updated = summary.updated_at_utc.astimezone(zone).strftime("%Y-%m-%d %H:%M")
    parts = [f"{KIND_NAMES[summary.kind]} · {summary.display_title} — updated {updated}"]
    count = summary.placeholder_count
    if count:
        parts.append(f"{count} placeholder{'' if count == 1 else 's'}")
    if summary.action_title:
        parts.append(f"for “{summary.action_title}”")
    return " · ".join(parts)


class _DraftList(QListWidget):
    """Return and Enter open the current row on every platform, as in the actions panel."""

    def keyPressEvent(self, event: QKeyEvent) -> None:
        item = self.currentItem()
        if item is not None and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.itemActivated.emit(item)
            return
        super().keyPressEvent(event)


class DraftsPanel(QWidget):
    """Lists drafts and asks the window to act; it changes nothing itself.

    ``draft_requested(kind, value)`` carries NEW with a DraftKind, or OPEN or DELETE with
    the selected DraftSummary.
    """

    draft_requested = Signal(str, object)

    def __init__(self) -> None:
        super().__init__()
        self._drafts: tuple[DraftSummary, ...] = ()
        self._busy = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        heading = QLabel("Your drafts and notes")
        heading.setTextFormat(Qt.TextFormat.PlainText)
        font = heading.font()
        font.setPointSize(font.pointSize() + 3)
        heading.setFont(font)
        layout.addWidget(heading)
        self.list = _DraftList()
        self.list.setAccessibleName("Drafts and notes")
        self.list.currentRowChanged.connect(lambda _row: self._update_buttons())
        self.list.itemActivated.connect(lambda _item: self._request(OPEN))
        layout.addWidget(self.list)
        self.empty = QLabel("No drafts yet. Start one with New, or from a brief item or action.")
        self.empty.setTextFormat(Qt.TextFormat.PlainText)
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)
        buttons = QHBoxLayout()
        self.new_button = QPushButton("Ne&w")
        self.new_menu = QMenu(self.new_button)
        for kind in NEW_KINDS:
            menu_action = self.new_menu.addAction(KIND_NAMES[kind])
            menu_action.triggered.connect(
                lambda _checked=False, chosen=kind: self._request_new(chosen)
            )
        self.new_button.setMenu(self.new_menu)
        self.open_button = QPushButton("&Open")
        self.delete_button = QPushButton("Delete draf&t")
        for button in (self.new_button, self.open_button, self.delete_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.open_button.clicked.connect(lambda: self._request(OPEN))
        self.delete_button.clicked.connect(lambda: self._request(DELETE))
        self._update_buttons()

    def selected(self) -> DraftSummary | None:
        row = self.list.currentRow()
        return self._drafts[row] if 0 <= row < len(self._drafts) else None

    def show_drafts(self, drafts: tuple[DraftSummary, ...], zone: ZoneInfo) -> None:
        """Replace the list, keeping the selection on the same draft when it remains."""
        previous = self.selected()
        row = self.list.currentRow()
        self._drafts = drafts
        self.list.clear()
        for summary in drafts:
            self.list.addItem(QListWidgetItem(describe(summary, zone)))
        ids = [summary.public_id for summary in drafts]
        if previous is not None and previous.public_id in ids:
            self.list.setCurrentRow(ids.index(previous.public_id))
        elif drafts:
            self.list.setCurrentRow(min(max(row, 0), len(drafts) - 1))
        self.empty.setVisible(not drafts)
        self._update_buttons()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_buttons()

    def _update_buttons(self) -> None:
        chosen = self.selected() is not None and not self._busy
        self.new_button.setEnabled(not self._busy)
        self.open_button.setEnabled(chosen)
        self.delete_button.setEnabled(chosen)

    def _request_new(self, kind: DraftKind) -> None:
        if not self._busy:
            self.draft_requested.emit(NEW, kind)

    def _request(self, kind: str) -> None:
        summary = self.selected()
        if summary is not None and not self._busy:
            self.draft_requested.emit(kind, summary)
