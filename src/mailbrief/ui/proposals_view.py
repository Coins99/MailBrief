"""The proposals of one action: what each would change, then apply or dismiss it (M8 Part 7).

The dialog only emits requests; the window applies and dismisses through the backend. A
proposal carries an email's quote, its sender and possibly a deadline phrase, all untrusted,
so everything here is plain text: rows and labels are never rich text, nothing is linked, and
a button label escapes the mnemonic marker.
"""

from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from mailbrief.domain.actions import Action, ActionProposal, ActionStatus
from mailbrief.domain.analysis import FollowUpKind
from mailbrief.ui.deadline_text import deadline_text
from mailbrief.ui.lists import ActivatingList

_COMPLETES = {
    FollowUpKind.CANCELLED: "Complete it (cancelled)",
    FollowUpKind.DELIVERED: "Complete it (delivered)",
}
NONE_PENDING = "This action has no pending proposals."
HINT = (
    "Select a row, then use a button: Return and a double-click never apply or dismiss. "
    "Applying changes only what the row says, never the action's title, notes, steps or "
    "owner, and you can undo it right after."
)


def pending_proposals(action: Action) -> tuple[ActionProposal, ...]:
    """The proposals that can still be applied: only an open action can be updated."""
    return action.proposals if action.status is ActionStatus.OPEN else ()


def effect_text(proposal: ActionProposal, zone: ZoneInfo) -> str:
    """What applying a proposal does, named by its effect.

    A new deadline reads "Set the deadline to 2026-10-05 17:00" (an exact time in ``zone``),
    "... to 2026-10-05" (a date) or "... to “next Monday”" (words only); a cancellation or
    delivery reads "Complete it (cancelled)" or "Complete it (delivered)".
    """
    if proposal.kind is FollowUpKind.NEW_DEADLINE:
        due = deadline_text(proposal, zone)
        return "Set the deadline" if due is None else f"Set the deadline to {due}"
    return _COMPLETES[proposal.kind]


def proposal_text(proposal: ActionProposal, zone: ZoneInfo) -> str:
    """One row: the effect, the email's quote, its sender and when it arrived in ``zone``."""
    received = proposal.received_at_utc.astimezone(zone)
    return (
        f"{effect_text(proposal, zone)} · “{proposal.evidence}” · "
        f"{proposal.sender_address} · {received:%Y-%m-%d %H:%M}"
    )


def _plain(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _label(text: str) -> str:
    """Button text with no accidental mnemonic: Qt reads ``&`` in a label as one."""
    return text.replace("&", "&&")


class ProposalsDialog(QDialog):
    """``apply_requested(proposal_id)`` and ``dismiss_requested(proposal_id)`` ask the window
    to act on the selected proposal; it looks up the action's revision from ``proposal()``.

    Only the buttons (and their mnemonics) apply or dismiss: Return and a double-click on a
    row do nothing, and neither button is the dialog's default, so Return elsewhere in the
    dialog only reaches Close.
    """

    apply_requested = Signal(int)
    dismiss_requested = Signal(int)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Proposals")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(680, 380)
        self._action_id: str | None = None
        self._proposals: tuple[ActionProposal, ...] = ()
        self._zone = ZoneInfo("UTC")
        self._busy = False
        layout = QVBoxLayout(self)
        self.heading = _plain()
        layout.addWidget(self.heading)
        self.listing = ActivatingList()
        self.listing.setAccessibleName("Pending proposals for this action")
        layout.addWidget(self.listing, 1)
        layout.addWidget(_plain(HINT))
        buttons = QHBoxLayout()
        self.apply_button = QPushButton("&Apply")
        self.dismiss_button = QPushButton("&Dismiss")
        self.close_button = QPushButton("&Close")
        for button in (self.apply_button, self.dismiss_button):
            button.setAutoDefault(False)
        for button in (self.apply_button, self.dismiss_button, self.close_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.listing.currentRowChanged.connect(lambda _row: self._update_buttons())
        self.apply_button.clicked.connect(self._apply)
        self.dismiss_button.clicked.connect(self._dismiss)
        self.close_button.clicked.connect(self.reject)
        self._update_buttons()

    @property
    def action_public_id(self) -> str | None:
        """The action being shown, if any."""
        return self._action_id

    def proposal(self, proposal_id: int) -> ActionProposal | None:
        """A proposal being shown, with the action revision it was loaded at."""
        return next((item for item in self._proposals if item.id == proposal_id), None)

    def show_proposals(self, action: Action | None, zone: ZoneInfo) -> None:
        """Replace what is listed with the action's pending proposals, keeping the selection on
        the same proposal when it remains. ``action`` is None when the action is gone."""
        previous = self._selected()
        kept = None if previous is None else previous.id
        self._action_id = None if action is None else action.public_id
        self._proposals = () if action is None else pending_proposals(action)
        self._zone = zone
        self.heading.setText("Proposals" if action is None else f"Proposals for “{action.title}”")
        self.listing.blockSignals(True)
        self.listing.clear()
        for proposal in self._proposals:
            self.listing.addItem(QListWidgetItem(proposal_text(proposal, zone)))
        self.listing.blockSignals(False)
        ids = [proposal.id for proposal in self._proposals]
        if self._proposals:
            self.listing.setCurrentRow(ids.index(kept) if kept in ids else 0)
        else:
            self.heading.setText(f"{self.heading.text()}\n{NONE_PENDING}")
        self._update_buttons()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_buttons()

    def _selected(self) -> ActionProposal | None:
        row = self.listing.currentRow()
        return self._proposals[row] if 0 <= row < len(self._proposals) else None

    def _update_buttons(self) -> None:
        chosen = self._selected()
        enabled = chosen is not None and not self._busy
        self.listing.setEnabled(not self._busy)
        self.apply_button.setEnabled(enabled)
        self.dismiss_button.setEnabled(enabled)
        if chosen is None:
            self.apply_button.setText("&Apply")
            self.apply_button.setAccessibleName("Apply the selected proposal")
            return
        effect = effect_text(chosen, self._zone)
        # A fixed mnemonic (A): the effect's own first letter varies with the row.
        self.apply_button.setText(f"&Apply: {_label(effect)}")
        self.apply_button.setAccessibleName(f"Apply: {effect}")

    def _apply(self) -> None:
        chosen = self._selected()
        if chosen is not None and not self._busy:
            self.apply_requested.emit(chosen.id)

    def _dismiss(self) -> None:
        chosen = self._selected()
        if chosen is not None and not self._busy:
            self.dismiss_requested.emit(chosen.id)
