"""The owner's actions: open, waiting and completed tabs, with their plan, dates and state.

Titles come from the owner or, originally, from AI suggestions about an email, so they are
shown only as plain list-item text and never in tooltips, which Qt may render as rich text.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QKeyEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from mailbrief.domain.actions import Action, ActionFilter, ActionStatus
from mailbrief.domain.analysis import ActionOwnership

EDIT = "edit"
COMPLETE = "complete"
REOPEN = "reopen"
DELETE = "delete"

_TAB_NAMES = {
    ActionFilter.OPEN: "Open",
    ActionFilter.WAITING: "Waiting",
    ActionFilter.COMPLETED: "Completed",
}


def describe(action: Action, *, today: date, zone: ZoneInfo, now: datetime) -> str:
    """One line of plain text naming the action and what matters about it now."""
    details: list[str] = []
    if action.target_date is not None:
        details.append(f"target {action.target_date.isoformat()}")
    if action.deadline_date is not None:
        details.append(f"due {action.deadline_date.isoformat()}")
    elif action.deadline_text:
        details.append(f"due “{action.deadline_text}”")
    if action.steps:
        done = sum(step.done for step in action.steps)
        details.append(f"{done}/{len(action.steps)} steps")
    if action.status is ActionStatus.OPEN:
        if action.ownership is ActionOwnership.WAITING_FOR:
            details.append("waiting for someone")
        if action.is_overdue(now):
            details.append("overdue")
        if action.carried_over(today, zone):
            details.append("carried over")
    if not any(source.available for source in action.sources) and action.sources:
        details.append("source no longer in local mail")
    return action.title + (" — " + " · ".join(details) if details else "")


def gmail_source(action: Action) -> str | None:
    """The first source link that opens Gmail, if any; nothing else is ever opened."""
    for source in action.sources:
        link = source.web_link
        if link.scheme == "https" and link.host == "mail.google.com":
            return str(link)
    return None


class _ActionList(QListWidget):
    """Return and Enter activate the current row on every platform.

    On macOS, Qt's item views only try to edit an item on Return, so they never activate it.
    """

    def keyPressEvent(self, event: QKeyEvent) -> None:
        item = self.currentItem()
        if item is not None and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.itemActivated.emit(item)
            return
        super().keyPressEvent(event)


class ActionsPanel(QWidget):
    """Lists actions per view and asks the window to act; it changes nothing itself.

    ``action_requested(kind, action)`` carries EDIT, COMPLETE, REOPEN or DELETE and the
    selected Action. Opening a source needs no backend, so the panel does it directly.
    """

    action_requested = Signal(str, object)

    def __init__(self) -> None:
        super().__init__()
        self._actions: dict[ActionFilter, tuple[Action, ...]] = dict.fromkeys(ActionFilter, ())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        heading = QLabel("Your actions")
        heading.setTextFormat(Qt.TextFormat.PlainText)
        font = heading.font()
        font.setPointSize(font.pointSize() + 3)
        heading.setFont(font)
        layout.addWidget(heading)
        self.tabs = QTabWidget()
        self.lists: dict[ActionFilter, QListWidget] = {}
        for view in ActionFilter:
            listing = _ActionList()
            listing.setAccessibleName(f"{_TAB_NAMES[view]} actions")
            listing.currentRowChanged.connect(lambda _row: self._update_buttons())
            listing.itemActivated.connect(lambda _item: self._request(EDIT))
            self.lists[view] = listing
            self.tabs.addTab(listing, _TAB_NAMES[view])
        self.tabs.currentChanged.connect(lambda _index: self._update_buttons())
        layout.addWidget(self.tabs)
        buttons = QHBoxLayout()
        self.edit_button = QPushButton("&Edit…")
        self.complete_button = QPushButton("Com&plete")
        self.delete_button = QPushButton("De&lete")
        self.source_button = QPushButton("Open s&ource")
        for button in (
            self.edit_button,
            self.complete_button,
            self.delete_button,
            self.source_button,
        ):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.edit_button.clicked.connect(lambda: self._request(EDIT))
        self.complete_button.clicked.connect(self._complete_or_reopen)
        self.delete_button.clicked.connect(lambda: self._request(DELETE))
        self.source_button.clicked.connect(self._open_source)
        self._busy = False
        self._update_buttons()

    def view(self) -> ActionFilter:
        return list(ActionFilter)[self.tabs.currentIndex()]

    def selected(self) -> Action | None:
        view = self.view()
        row = self.lists[view].currentRow()
        actions = self._actions[view]
        return actions[row] if 0 <= row < len(actions) else None

    def show_actions(
        self,
        view: ActionFilter,
        actions: tuple[Action, ...],
        *,
        today: date,
        zone: ZoneInfo,
        now: datetime,
    ) -> None:
        """Replace one view's list, keeping the selection on the same action when it remains."""
        listing = self.lists[view]
        previous = self._actions[view]
        row = listing.currentRow()
        kept = previous[row].public_id if 0 <= row < len(previous) else None
        self._actions[view] = actions
        listing.clear()
        for action in actions:
            listing.addItem(QListWidgetItem(describe(action, today=today, zone=zone, now=now)))
        ids = [action.public_id for action in actions]
        if kept in ids:
            listing.setCurrentRow(ids.index(kept))
        elif actions:
            listing.setCurrentRow(min(max(row, 0), len(actions) - 1))
        self.tabs.setTabText(list(ActionFilter).index(view), f"{_TAB_NAMES[view]} ({len(actions)})")
        self._update_buttons()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_buttons()

    def _update_buttons(self) -> None:
        action = self.selected()
        completed = self.view() is ActionFilter.COMPLETED
        self.complete_button.setText("Re&open" if completed else "Com&plete")
        enabled = action is not None and not self._busy
        for button in (self.edit_button, self.complete_button, self.delete_button):
            button.setEnabled(enabled)
        self.source_button.setEnabled(action is not None and gmail_source(action) is not None)

    def _request(self, kind: str) -> None:
        action = self.selected()
        if action is not None and not self._busy:
            self.action_requested.emit(kind, action)

    def _complete_or_reopen(self) -> None:
        self._request(REOPEN if self.view() is ActionFilter.COMPLETED else COMPLETE)

    def _open_source(self) -> None:
        action = self.selected()
        link = None if action is None else gmail_source(action)
        if link is not None:
            QDesktopServices.openUrl(QUrl(link))
