"""The owner's actions: open, waiting and completed tabs, with their plan, dates and state.

Titles come from the owner or, originally, from AI suggestions about an email, so they are
shown only as plain list-item text and never in tooltips, which Qt may render as rich text.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from mailbrief.domain.actions import Action, ActionFilter, ActionStatus
from mailbrief.domain.analysis import ActionOwnership
from mailbrief.domain.drafts import DraftKind
from mailbrief.ui.deadline_text import deadline_text
from mailbrief.ui.labels import outline_button
from mailbrief.ui.lists import ActivatingList
from mailbrief.ui.proposals_view import pending_proposals
from mailbrief.ui.theme import TITLE_PX, ui_font

EDIT = "edit"
COMPLETE = "complete"
REOPEN = "reopen"
DELETE = "delete"
SEEN = "seen"
PROPOSALS = "proposals"

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_TAB_NAMES = {
    ActionFilter.OPEN: "Open",
    ActionFilter.WAITING: "Waiting",
    ActionFilter.COMPLETED: "Completed",
}


def describe(action: Action, *, today: date, zone: ZoneInfo, now: datetime) -> str:
    """One line of plain text naming the action and what matters about it now.

    An exact deadline is shown in ``zone``, the owner's.
    """
    details: list[str] = []
    if action.target_date is not None:
        details.append(f"target {action.target_date.isoformat()}")
    due = deadline_text(action, zone)
    if due is not None:
        details.append(f"due {due}")
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
    thread = action.thread
    if thread is not None:
        if thread.latest_at_utc is not None and thread.latest_sender is not None:
            latest = _when(thread.latest_at_utc, today=today, zone=zone, clock=True)
            details.append(
                f"{thread.new_messages} new in thread, latest {latest} from {thread.latest_sender}"
            )
        if thread.owner_replied_at_utc is not None:
            replied = _when(thread.owner_replied_at_utc, today=today, zone=zone, clock=False)
            details.append(f"you replied {replied}")
    if proposals := len(pending_proposals(action)):
        details.append(f"{proposals} {'proposal' if proposals == 1 else 'proposals'}")
    return action.title + (" — " + " · ".join(details) if details else "")


def _when(moment: datetime, *, today: date, zone: ZoneInfo, clock: bool) -> str:
    """A moment of the past week by weekday ("Tue 14:02"), an older one by date, in ``zone``.

    Weekday names are fixed, like the rest of the window's English text.
    """
    local = moment.astimezone(zone)
    time = f" {local:%H:%M}" if clock else ""
    if today - timedelta(days=6) <= local.date() <= today:
        return _WEEKDAYS[local.weekday()] + time
    return local.date().isoformat() + time


def has_activity(action: Action) -> bool:
    """Whether the action's threads have anything the owner hasn't marked seen."""
    return action.thread is not None and action.thread.unseen


def can_reply(action: Action) -> bool:
    """Whether the action's first email still in local mail can be answered from Gmail.

    The draft service replies to that same email.
    """
    source = next((source for source in action.sources if source.available), None)
    link = None if source is None else source.web_link
    return link is not None and link.scheme == "https" and link.host == "mail.google.com"


def gmail_source(action: Action) -> str | None:
    """The first source link that opens Gmail, if any; nothing else is ever opened."""
    for source in action.sources:
        link = source.web_link
        if link.scheme == "https" and link.host == "mail.google.com":
            return str(link)
    return None


class ActionsPanel(QWidget):
    """Lists actions per view and asks the window to act; it changes nothing itself.

    ``action_requested(kind, action)`` carries EDIT, COMPLETE, REOPEN, DELETE, SEEN or
    PROPOSALS and the selected Action, and ``draft_requested(kind, action)`` a DraftKind and
    the Action. Opening a source needs no backend, so the panel does it directly.
    """

    action_requested = Signal(str, object)
    draft_requested = Signal(object, object)

    def __init__(self) -> None:
        super().__init__()
        self._actions: dict[ActionFilter, tuple[Action, ...]] = dict.fromkeys(ActionFilter, ())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        heading = QLabel("Your actions")
        heading.setTextFormat(Qt.TextFormat.PlainText)
        heading.setFont(ui_font(TITLE_PX, medium=True))
        layout.addWidget(heading)
        self.tabs = QTabWidget()
        self.lists: dict[ActionFilter, QListWidget] = {}
        for view in ActionFilter:
            listing = ActivatingList()
            listing.setAccessibleName(f"{_TAB_NAMES[view]} actions")
            listing.currentRowChanged.connect(lambda _row: self._update_buttons())
            listing.itemActivated.connect(lambda _item: self._request(EDIT))
            self.lists[view] = listing
            self.tabs.addTab(listing, _TAB_NAMES[view])
        self.tabs.currentChanged.connect(lambda _index: self._update_buttons())
        layout.addWidget(self.tabs)
        buttons = QHBoxLayout()
        self.edit_button = outline_button("&Edit…", "editButton", mnemonic=True)
        self.complete_button = outline_button("Com&plete", "completeButton", mnemonic=True)
        self.delete_button = outline_button("De&lete", "deleteButton", mnemonic=True)
        self.source_button = outline_button("Open s&ource", "sourceButton", mnemonic=True)
        # K: the window's Sync and review already takes S.
        self.seen_button = outline_button("Mar&k seen", "seenButton", mnemonic=True)
        self.seen_button.setToolTip("Clear the new-in-thread and you-replied notes.")
        # R: the other letters of "Proposals" are taken by Complete, Delete, Open source and
        # the window's Sync and review.
        self.proposals_button = outline_button("P&roposals…", "proposalsButton", mnemonic=True)
        self.proposals_button.setToolTip("Review follow-up replies that propose an update.")
        self.draft_button = outline_button("Dra&ft…", "draftButton", mnemonic=True)
        self.draft_menu = QMenu(self.draft_button)
        self.reply_draft = self.draft_menu.addAction("Reply to its email")
        self.reply_draft.triggered.connect(lambda: self._request_draft(DraftKind.REPLY))
        for kind, name in (
            (DraftKind.EMAIL, "Email"),
            (DraftKind.NOTE, "Note"),
            (DraftKind.MESSAGE, "Message"),
        ):
            menu_action = self.draft_menu.addAction(name)
            menu_action.triggered.connect(
                lambda _checked=False, chosen=kind: self._request_draft(chosen)
            )
        self.draft_button.setMenu(self.draft_menu)
        for button in (
            self.edit_button,
            self.complete_button,
            self.delete_button,
            self.source_button,
            self.seen_button,
            self.proposals_button,
            self.draft_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)  # Buttons keep their own width.
        layout.addLayout(buttons)
        self.edit_button.clicked.connect(lambda: self._request(EDIT))
        self.complete_button.clicked.connect(self._complete_or_reopen)
        self.delete_button.clicked.connect(lambda: self._request(DELETE))
        self.seen_button.clicked.connect(self._mark_seen)
        self.proposals_button.clicked.connect(self._show_proposals)
        self.source_button.clicked.connect(self._open_source)
        self._busy = False
        self._update_buttons()

    def view(self) -> ActionFilter:
        return list(ActionFilter)[self.tabs.currentIndex()]

    def show_view(self, view: ActionFilter) -> None:
        self.tabs.setCurrentWidget(self.lists[view])

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
        total: int | None = None,
    ) -> None:
        """Replace one view's list, keeping the selection on the same action when it remains.

        ``total`` is how many the view holds when the list shows only some, so the tab can
        say "Completed (200+)".
        """
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
        more = "+" if total is not None and total > len(actions) else ""
        self.tabs.setTabText(
            list(ActionFilter).index(view), f"{_TAB_NAMES[view]} ({len(actions)}{more})"
        )
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
        self.seen_button.setEnabled(enabled and action is not None and has_activity(action))
        self.proposals_button.setEnabled(
            enabled and action is not None and bool(pending_proposals(action))
        )
        self.draft_button.setEnabled(enabled)
        self.reply_draft.setEnabled(action is not None and can_reply(action))

    def _request(self, kind: str) -> None:
        action = self.selected()
        if action is not None and not self._busy:
            self.action_requested.emit(kind, action)

    def _request_draft(self, kind: DraftKind) -> None:
        action = self.selected()
        if action is None or self._busy or (kind is DraftKind.REPLY and not can_reply(action)):
            return
        self.draft_requested.emit(kind, action)

    def _complete_or_reopen(self) -> None:
        self._request(REOPEN if self.view() is ActionFilter.COMPLETED else COMPLETE)

    def _mark_seen(self) -> None:
        action = self.selected()
        if action is not None and has_activity(action):
            self._request(SEEN)

    def _show_proposals(self) -> None:
        action = self.selected()
        if action is not None and pending_proposals(action):
            self._request(PROPOSALS)

    def action_with_id(self, public_id: str) -> Action | None:
        """The action with this ID in any view, as last shown."""
        for actions in self._actions.values():
            for action in actions:
                if action.public_id == public_id:
                    return action
        return None

    def _open_source(self) -> None:
        action = self.selected()
        link = None if action is None else gmail_source(action)
        if link is not None:
            QDesktopServices.openUrl(QUrl(link))
