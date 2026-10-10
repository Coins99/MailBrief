"""The owner's actions: open, waiting and completed views, as painted rows beside the
selected action's detail.

Titles come from the owner or, originally, from AI suggestions about an email, so rows are
painted with ``drawText`` only, the item text is plain, and none of it reaches a tooltip,
which Qt may render as rich text.
"""

import itertools
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final
from zoneinfo import ZoneInfo

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QRect, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QKeyEvent, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QScrollBar,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from mailbrief.domain.actions import Action, ActionFilter
from mailbrief.domain.drafts import DraftKind
from mailbrief.ui.action_detail import ActionDetailPane
from mailbrief.ui.action_text import (
    action_details,
    completed_text,
    gmail_source,
    target_text,
)
from mailbrief.ui.brief_list import (
    CHIP_GAP,
    ELIDE_MARGIN,
    ROW_PAD_H,
    ROW_PAD_V,
    ROW_ROLE,
    Chip,
    ChipTone,
    elided,
    page_detail,
    paint_chips,
    paint_lines,
    paint_selection,
    paint_separator,
    proposal_chip,
    row_height,
)
from mailbrief.ui.deadline_text import day_text
from mailbrief.ui.hairline import HairlineSplitter, paint_focus_ring
from mailbrief.ui.labels import plain_label
from mailbrief.ui.lists import ActivatingList
from mailbrief.ui.proposals_view import pending_proposals
from mailbrief.ui.theme import TEXT_PX, TITLE_PX, current_tokens, ui_metrics

__all__ = [
    "COMPLETE",
    "DELETE",
    "EDIT",
    "PROPOSALS",
    "REOPEN",
    "SEEN",
    "ActionList",
    "ActionRow",
    "ActionRowDelegate",
    "ActionsPanel",
    "action_row",
    "can_reply",
    "describe",
    "gmail_source",
    "has_activity",
]

EDIT = "edit"
COMPLETE = "complete"
REOPEN = "reopen"
DELETE = "delete"
SEEN = "seen"
PROPOSALS = "proposals"

NO_DATES: Final = "No target or deadline"
_TAB_NAMES = {
    ActionFilter.OPEN: "Open",
    ActionFilter.WAITING: "Waiting",
    ActionFilter.COMPLETED: "Completed",
}

_Index = QModelIndex | QPersistentModelIndex


def describe(action: Action, *, today: date, zone: ZoneInfo, now: datetime) -> str:
    """One line of plain text naming the action and what matters about it now.

    An exact deadline is shown in ``zone``, the owner's.
    """
    facts = action_details(action, today=today, zone=zone, now=now)
    details: list[str] = []
    if facts.target is not None:
        details.append(f"target {day_text(facts.target, facts.today)}")
    if facts.due is not None:
        details.append(f"due {facts.due}")
    if facts.steps:
        details.append(f"{facts.steps_done}/{facts.steps} steps")
    if facts.waiting:
        details.append("waiting for someone")
    if facts.overdue:
        details.append("overdue")
    if facts.carried_over:
        details.append("carried over")
    if facts.source_gone:
        details.append("source no longer in local mail")
    details += [text for text in (facts.activity, facts.replied) if text is not None]
    if proposals := len(facts.proposals):
        details.append(f"{proposals} {'proposal' if proposals == 1 else 'proposals'}")
    return action.title + (" — " + " · ".join(details) if details else "")


@dataclass(frozen=True)
class ActionRow:
    """What an action's row paints: its title, a meta line, chips, and whether it is muted
    (completed)."""

    title: str
    meta: str
    chips: tuple[Chip, ...]
    muted: bool


def action_row(action: Action, *, today: date, zone: ZoneInfo, now: datetime) -> ActionRow:
    """The painted row for ``action``, from the same details as ``describe``."""
    facts = action_details(action, today=today, zone=zone, now=now)
    meta = [
        text
        for text in (
            completed_text(facts),
            target_text(facts, reason=False),  # The detail pane gives the reason.
            None if facts.due is None else f"Due {facts.due}",
            f"{facts.steps_done}/{facts.steps} steps" if facts.steps else None,
        )
        if text is not None
    ]
    chips: list[Chip] = []
    if facts.overdue:
        chips.append(Chip("Overdue", ChipTone.WARNING))
    if facts.new_messages and has_activity(action):
        chips.append(Chip(f"{facts.new_messages} new in thread", ChipTone.ACCENT))
    proposal = proposal_chip(facts.proposals)
    if proposal is not None:
        chips.append(proposal)
    return ActionRow(
        title=action.title,
        meta=" · ".join(meta) if meta else NO_DATES,
        chips=tuple(chips),
        muted=facts.completed is not None,
    )


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


class ActionRowDelegate(QStyledItemDelegate):
    """Paints an ``ActionRow`` with the brief list's helpers, ``drawText`` only."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: _Index) -> None:
        row = index.data(ROW_ROLE)
        if not isinstance(row, ActionRow):
            super().paint(painter, option, index)
            return
        tokens = current_tokens()
        rect: QRect = option.rect
        state: QStyle.StateFlag = option.state
        painter.save()
        painter.setClipRect(rect)  # Chips that don't fit stop at the row's edge.
        paint_selection(painter, rect, state, tokens)
        width = max(0, rect.width() - ELIDE_MARGIN)
        left = rect.left() + ROW_PAD_H
        top = paint_lines(
            painter,
            left,
            rect.top() + ROW_PAD_V,
            width,
            row.title,
            elided(row.meta, ui_metrics(TEXT_PX), width),
            title_color=tokens.text_secondary if row.muted else tokens.text,
            subtitle_color=tokens.text_muted if row.muted else tokens.text_secondary,
        )
        if row.chips:
            paint_chips(painter, row.chips, left, top + CHIP_GAP)
        paint_separator(painter, rect, tokens)
        paint_focus_ring(painter, rect, state, tokens.accent_fg)
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: _Index) -> QSize:
        row = index.data(ROW_ROLE)
        if not isinstance(row, ActionRow):
            return super().sizeHint(option, index)
        # No width of its own: a row takes the viewport's width, so it never widens the page.
        return QSize(0, row_height(bool(row.chips)))


class ActionList(ActivatingList):
    """One view's actions as painted rows. With ``set_page_scroll``, Page Up and Page Down
    scroll the selected action's detail while the whole list fits."""

    def __init__(self, view: ActionFilter) -> None:
        super().__init__()
        self.setObjectName("actionList")
        self.setAccessibleName(f"{_TAB_NAMES[view]} actions")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setUniformItemSizes(False)
        self.setItemDelegate(ActionRowDelegate(self))
        self._page_scroll: QScrollBar | None = None

    def set_page_scroll(self, bar: QScrollBar | None) -> None:
        self._page_scroll = bar

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if page_detail(self, self._page_scroll, event):
            return
        super().keyPressEvent(event)


class ActionsPanel(QWidget):
    """Lists actions per view beside the selected one's detail, and asks the window to act;
    it changes nothing itself.

    ``action_requested(kind, action)`` carries EDIT, COMPLETE, REOPEN, DELETE, SEEN or
    PROPOSALS and the selected Action, and ``draft_requested(kind, action)`` a DraftKind and
    the Action. Opening a source needs no backend, so the panel does it directly.
    """

    action_requested = Signal(str, object)
    draft_requested = Signal(object, object)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("actionsPanel")
        self._actions: dict[ActionFilter, tuple[Action, ...]] = dict.fromkeys(ActionFilter, ())
        # The day, zone and moment the lists were last shown for; the detail uses them too.
        self._moment: tuple[date, ZoneInfo, datetime] | None = None
        self._filling = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.splitter = HairlineSplitter(Qt.Orientation.Horizontal)
        list_side = QWidget()
        list_side.setObjectName("actionsListPane")
        side = QVBoxLayout(list_side)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(0)
        heading = plain_label("Your actions", px=TITLE_PX, medium=True)
        heading.setObjectName("actionsHeading")
        heading.setContentsMargins(12, 10, 12, 4)
        side.addWidget(heading)
        self.tabs = QTabBar()
        self.tabs.setObjectName("actionsTabs")
        self.tabs.setAccessibleName("Action views")
        self.tabs.setDrawBase(False)
        self.tabs.setExpanding(False)
        self.tabs.setElideMode(Qt.TextElideMode.ElideRight)
        # Tab reaches it on every platform, not only with macOS's keyboard navigation on.
        self.tabs.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        tabs_row = QHBoxLayout()
        tabs_row.setContentsMargins(4, 0, 12, 0)  # The tabs' own padding lines them up.
        tabs_row.addWidget(self.tabs)
        tabs_row.addStretch(1)
        side.addLayout(tabs_row)
        self.stack = QStackedWidget()
        self.stack.setObjectName("actionsStack")
        self.lists: dict[ActionFilter, QListWidget] = {}
        self.detail = ActionDetailPane()
        self.detail.setMinimumWidth(280)
        for view in ActionFilter:
            listing = ActionList(view)
            listing.set_page_scroll(self.detail.verticalScrollBar())
            listing.currentRowChanged.connect(lambda _row, shown=view: self._row_changed(shown))
            listing.itemActivated.connect(lambda _item: self._request(EDIT))
            self.lists[view] = listing
            self.tabs.addTab(_TAB_NAMES[view])
            self.stack.addWidget(listing)
        self.tabs.currentChanged.connect(self._view_changed)
        side.addWidget(self.stack, 1)
        list_side.setMinimumWidth(220)
        self.splitter.addWidget(list_side)
        self.splitter.addWidget(self.detail)
        self.splitter.setStretchFactor(0, 4)
        self.splitter.setStretchFactor(1, 5)
        self.splitter.setChildrenCollapsible(False)
        layout.addWidget(self.splitter)
        detail = self.detail
        self.edit_button = detail.edit_button
        self.complete_button = detail.complete_button
        self.delete_button = detail.delete_button
        self.source_button = detail.source_button
        self.seen_button = detail.seen_button
        self.proposals_button = detail.proposals_button
        self.draft_button = detail.draft_button
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
        self.edit_button.clicked.connect(lambda: self._request(EDIT))
        self.complete_button.clicked.connect(self._complete_or_reopen)
        self.delete_button.clicked.connect(lambda: self._request(DELETE))
        self.seen_button.clicked.connect(self._mark_seen)
        self.proposals_button.clicked.connect(self._show_proposals)
        self.source_button.clicked.connect(self._open_source)
        # Tab: the views, the list shown, then the detail's buttons in display order.
        chain: list[QWidget] = [self.tabs, *self.lists.values(), *detail.all_buttons()]
        for first, second in itertools.pairwise(chain):
            QWidget.setTabOrder(first, second)
        self._busy = False
        self._refresh()

    def view(self) -> ActionFilter:
        return list(ActionFilter)[self.tabs.currentIndex()]

    def show_view(self, view: ActionFilter) -> None:
        index = list(ActionFilter).index(view)
        self.tabs.setCurrentIndex(index)
        self.stack.setCurrentIndex(index)

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
        self._moment = (today, zone, now)
        self._filling = True
        try:
            listing.clear()
            for action in actions:
                item = QListWidgetItem(describe(action, today=today, zone=zone, now=now))
                item.setData(ROW_ROLE, action_row(action, today=today, zone=zone, now=now))
                listing.addItem(item)
            ids = [action.public_id for action in actions]
            if kept in ids:
                listing.setCurrentRow(ids.index(kept))
            elif actions:
                listing.setCurrentRow(min(max(row, 0), len(actions) - 1))
        finally:
            self._filling = False
        more = "+" if total is not None and total > len(actions) else ""
        self.tabs.setTabText(
            list(ActionFilter).index(view), f"{_TAB_NAMES[view]} ({len(actions)}{more})"
        )
        if view is self.view():
            self._refresh()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_buttons()

    def _view_changed(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self._refresh()

    def _row_changed(self, view: ActionFilter) -> None:
        if not self._filling and view is self.view():
            self._refresh()

    def _refresh(self) -> None:
        """Show the selected action in the detail, or the view's empty state."""
        action = self.selected()
        if action is None or self._moment is None:
            self.detail.show_empty(self.view())
        else:
            today, zone, now = self._moment
            self.detail.show_action(action, today=today, zone=zone, now=now)
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
