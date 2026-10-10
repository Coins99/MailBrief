"""The selected action: its title, state, dates, plan, notes, thread activity, proposals and
sources, with the buttons that act on it.

The buttons are made once and stay, so the window's Tab order and the Actions panel's names
for them never go stale; only the text between them is rebuilt for each action. Text the
mail, the AI or the owner wrote appears only in plain-text wrapping labels, never in a
tooltip, and the pane never opens a URL itself.
"""

from collections.abc import Sequence
from datetime import date, datetime
from typing import Final
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from mailbrief.domain.actions import Action, ActionFilter, ActionSource, ActionStatus
from mailbrief.domain.analysis import ActionOwnership
from mailbrief.ui.action_text import (
    ActionDetails,
    action_details,
    completed_text,
    gmail_source,
    target_text,
)
from mailbrief.ui.brief_detail import DETAIL_MAX_WIDTH
from mailbrief.ui.brief_list import NO_SUBJECT
from mailbrief.ui.labels import outline_button, plain_label, wrap_label
from mailbrief.ui.theme import SMALL_PX, TEXT_PX, TITLE_PX

EMPTY_TEXT: Final = {
    ActionFilter.OPEN: "No open actions. Accept a suggestion in a brief to start one.",
    ActionFilter.WAITING: "Nothing you're waiting for.",
    ActionFilter.COMPLETED: "No completed actions yet.",
}
DONE_MARK: Final = "✓ "
OPEN_MARK: Final = "○ "


def state_text(action: Action, details: ActionDetails) -> str:
    """ "Yours · open", "Waiting for someone · open" or "Completed Oct 6"."""
    completed = completed_text(details)
    if action.status is ActionStatus.COMPLETED and completed is not None:
        return completed
    who = "Yours" if action.ownership is ActionOwnership.MINE else "Waiting for someone"
    return f"{who} · {action.status.value}"


def proposals_text(count: int) -> str:
    if count == 1:
        return "A follow-up reply proposes an update to this action."
    return f"{count} follow-up replies propose updates to this action."


def source_text(source: ActionSource, *, today: date, zone: ZoneInfo) -> str:
    """The sender's address and the received date in ``zone``, and whether the email has
    left local mail."""
    local = source.received_at_utc.astimezone(zone)
    year = "" if local.year == today.year else f", {local.year}"
    text = f"{source.sender_address} · Received {local:%b} {local.day}{year}"
    return text if source.available else f"{text} · No longer in local mail"


class _Section(QWidget):
    """A column of labels that is replaced as a whole."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.setObjectName(name)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)

    def set_widgets(self, widgets: Sequence[QWidget | int]) -> None:
        """Show ``widgets`` in order (an int is extra space above the next); hidden when
        there are none."""
        while (item := self._layout.takeAt(0)) is not None:
            old = item.widget()
            if old is not None:
                old.setParent(None)  # Gone now, not at the next turn of the event loop.
        for widget in widgets:
            if isinstance(widget, int):
                self._layout.addSpacing(widget)
            else:
                self._layout.addWidget(widget)
        self.setVisible(any(not isinstance(widget, int) for widget in widgets))


def _button_row(name: str, *buttons: QPushButton) -> QWidget:
    row = QWidget()
    row.setObjectName(name)
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    for button in buttons:
        layout.addWidget(button)
    layout.addStretch(1)  # Buttons keep their own width.
    return row


def _heading(text: str) -> QWidget:
    """A section's caption: app text, muted."""
    label = plain_label(text, tone="muted", px=SMALL_PX)
    label.setObjectName("actionSectionHeading")
    return label


_SECTION_GAP: Final = 6


class ActionDetailPane(QScrollArea):
    """Shows one action, or the view's empty state; it emits nothing itself; the Actions
    panel connects its buttons."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("actionDetail")
        self.setAccessibleName("Selected action")
        # Tab goes from the list straight to the buttons; a click on the pane still lets
        # the arrow keys scroll it.
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("actionDetailContent")
        # A readable measure on wide windows; the scroll area keeps it at the left.
        content.setMaximumWidth(DETAIL_MAX_WIDTH)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        # K: the window's Sync and review already takes S.
        self.seen_button = outline_button("Mar&k seen", "seenButton", mnemonic=True)
        self.seen_button.setToolTip("Clear the new-in-thread and you-replied notes.")
        # R: the other letters of "Proposals" are taken by Complete, Delete, Open source and
        # the window's Sync and review.
        self.proposals_button = outline_button("P&roposals…", "proposalsButton", mnemonic=True)
        self.proposals_button.setToolTip("Review follow-up replies that propose an update.")
        self.source_button = outline_button(
            "Open s&ource", "sourceButton", mnemonic=True, icon_name="external-link"
        )
        self.edit_button = outline_button("&Edit…", "editButton", mnemonic=True)
        self.complete_button = outline_button("Com&plete", "completeButton", mnemonic=True)
        self.delete_button = outline_button("De&lete", "deleteButton", mnemonic=True)
        self.draft_button = outline_button(
            "Dra&ft…", "draftButton", mnemonic=True, icon_name="pencil"
        )
        self.empty = wrap_label("", tone="muted", px=TEXT_PX)
        self.empty.setObjectName("actionEmpty")
        self._top = _Section("actionSummary")
        self._seen_row = _button_row("seenRow", self.seen_button)
        self._proposals = _Section("actionProposals")
        self._proposals_row = _button_row("proposalsRow", self.proposals_button)
        self._sources = _Section("actionSources")
        self._source_row = _button_row("sourceRow", self.source_button)
        self._later_sources = _Section("actionLaterSources")
        self._footer = _button_row(
            "actionFooter",
            self.edit_button,
            self.complete_button,
            self.delete_button,
            self.draft_button,
        )
        # Created in display order, which is also the Tab order of the buttons.
        for widget in (
            self.empty,
            self._top,
            self._seen_row,
            self._proposals,
            self._proposals_row,
            self._sources,
            self._source_row,
            self._later_sources,
        ):
            layout.addWidget(widget)
        layout.addSpacing(_SECTION_GAP)
        layout.addWidget(self._footer)
        layout.addStretch(1)
        self.setWidget(content)
        self.title: QWidget | None = None
        self._shown: str | None = None  # The public ID of the action shown.
        self.show_empty(ActionFilter.OPEN)

    def all_buttons(self) -> list[QPushButton]:
        """Every button, shown or not, in display order."""
        return [
            self.seen_button,
            self.proposals_button,
            self.source_button,
            self.edit_button,
            self.complete_button,
            self.delete_button,
            self.draft_button,
        ]

    def buttons(self) -> list[QPushButton]:
        """The buttons shown, in display order."""
        content = self.widget()
        if content is None:
            return []
        return [button for button in self.all_buttons() if button.isVisibleTo(content)]

    def show_empty(self, view: ActionFilter) -> None:
        self._shown = None
        self.title = None
        self.empty.setText(EMPTY_TEXT[view])
        self.empty.show()
        for section in (self._top, self._proposals, self._sources, self._later_sources):
            section.set_widgets(())
        for row in (self._seen_row, self._proposals_row, self._source_row, self._footer):
            row.hide()

    def show_action(self, action: Action, *, today: date, zone: ZoneInfo, now: datetime) -> None:
        """Show ``action``; another action than the one shown starts at the top."""
        details = action_details(action, today=today, zone=zone, now=now)
        self.empty.hide()
        self.title = wrap_label(action.title, px=TITLE_PX, medium=True)
        self.title.setObjectName("actionTitle")
        state = wrap_label(state_text(action, details), tone="secondary", px=TEXT_PX)
        state.setObjectName("actionState")
        top: list[QWidget | int] = [self.title, state, *self._dates(details)]
        top += self._plan(action, details)
        if action.notes.strip():
            top += [_SECTION_GAP, _heading("Notes"), wrap_label(action.notes, px=TEXT_PX)]
        activity = [text for text in (details.activity, details.replied) if text is not None]
        if activity:
            top += [_SECTION_GAP, _heading("Thread")]
            top += [wrap_label(_sentence(text), px=TEXT_PX) for text in activity]
        self._top.set_widgets(top)
        self._seen_row.setVisible(bool(activity))
        count = len(details.proposals)
        self._proposals.set_widgets(
            [_SECTION_GAP, wrap_label(proposals_text(count), px=TEXT_PX)] if count else ()
        )
        self._proposals_row.setVisible(bool(count))
        self._show_sources(action, today=today, zone=zone)
        self._footer.show()
        if action.public_id != self._shown:
            self.verticalScrollBar().setValue(0)
        self._shown = action.public_id

    def _dates(self, details: ActionDetails) -> list[QWidget | int]:
        widgets: list[QWidget | int] = []
        if details.due is not None:
            text = f"Due {details.due}" + (" · Overdue" if details.overdue else "")
            due = wrap_label(text, tone="warning" if details.overdue else "secondary", px=TEXT_PX)
            due.setObjectName("actionDeadline")
            widgets.append(due)
        target = target_text(details)
        if target is not None:
            label = wrap_label(target, tone="secondary", px=TEXT_PX)
            label.setObjectName("actionTarget")
            widgets.append(label)
        return widgets

    def _plan(self, action: Action, details: ActionDetails) -> list[QWidget | int]:
        if not details.steps:
            return []
        widgets: list[QWidget | int] = [
            _SECTION_GAP,
            _heading(f"Plan · {details.steps_done} of {details.steps} done"),
        ]
        for step in sorted(action.steps, key=lambda step: step.position):
            if step.done:
                widgets.append(wrap_label(DONE_MARK + step.text, tone="muted", px=TEXT_PX))
            else:
                widgets.append(wrap_label(OPEN_MARK + step.text, px=TEXT_PX))
        return widgets

    def _show_sources(self, action: Action, *, today: date, zone: ZoneInfo) -> None:
        """Each source; Open source follows the first that opens Gmail."""
        sources = action.sources
        link = gmail_source(action)
        split = next(
            (number + 1 for number, source in enumerate(sources) if str(source.web_link) == link),
            len(sources),
        )
        first: list[QWidget | int] = []
        if sources:
            first = [_SECTION_GAP, _heading("Source" if len(sources) == 1 else "Sources")]
        first += [
            widget for source in sources[:split] for widget in self._source(source, today, zone)
        ]
        self._sources.set_widgets(first)
        self._source_row.setVisible(link is not None)
        self._later_sources.set_widgets(
            [widget for source in sources[split:] for widget in self._source(source, today, zone)]
        )

    @staticmethod
    def _source(source: ActionSource, today: date, zone: ZoneInfo) -> list[QWidget | int]:
        subject = wrap_label(source.subject or NO_SUBJECT, px=TEXT_PX)
        subject.setObjectName("sourceSubject")
        meta = wrap_label(
            source_text(source, today=today, zone=zone), tone="secondary", px=SMALL_PX
        )
        meta.setObjectName("sourceMeta")
        return [subject, meta]


def _sentence(text: str) -> str:
    """``text`` with a capital first letter, as a line of its own."""
    return text[:1].upper() + text[1:]
