"""The Briefs page: saved briefs by day, and missed days to brief (M8 Part 3).

The panel only emits requests; the window loads and briefs through the backend. Rows are
plain text. Briefing a past day is always the owner's explicit choice, one day at a time,
and replacing a saved brief is confirmed first.
"""

from datetime import date, timedelta

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mailbrief.domain.digests import SavedBriefSummary
from mailbrief.services.history import CATCH_UP_DAYS
from mailbrief.ui.deadline_text import day_text
from mailbrief.ui.labels import outline_button
from mailbrief.ui.lists import ActivatingList
from mailbrief.ui.theme import TITLE_PX, ui_font

NOT_CONNECTED = "Connect Gmail to brief missed days."
NONE_MISSED = "No missed days in the last 7 days."
NEEDS_CONNECTION = "Connect Gmail to brief a past day."
OTHER_ACCOUNT = "That brief is for another account; connect it to brief this day again."


def _plain(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def summary_text(summary: SavedBriefSummary, today: date | None = None) -> str:
    noun = "item" if summary.item_count == 1 else "items"
    return (
        f"{day_text(summary.local_date, today)} · {summary.status.value} · "
        f"{summary.item_count} {noun} · {summary.account_email}"
    )


class BriefHistoryPanel(QWidget):
    """``open_requested(account_email, local_date)`` asks to show a saved brief;
    ``generate_requested(local_date)`` asks to brief a past day for the connected account."""

    open_requested = Signal(str, object)
    generate_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("briefHistory")
        self.setAccessibleName("Saved briefs and missed days")
        self._summaries: tuple[SavedBriefSummary, ...] = ()
        self._missed: tuple[date, ...] = ()
        self._account: str | None = None
        self._today = date.min
        self._busy = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        heading = QLabel("Briefs")
        heading.setTextFormat(Qt.TextFormat.PlainText)
        heading.setFont(ui_font(TITLE_PX, medium=True))
        layout.addWidget(heading)
        layout.addWidget(_plain("Saved briefs"))
        self.saved = ActivatingList()
        self.saved.setAccessibleName("Saved briefs")
        layout.addWidget(self.saved, 2)
        layout.addWidget(_plain("Missed days"))
        self.missed = QListWidget()
        self.missed.setAccessibleName("Missed days in the last 7 days")
        layout.addWidget(self.missed, 1)
        self.missed_note = _plain()
        layout.addWidget(self.missed_note)
        buttons = QHBoxLayout()
        self.open_button = outline_button("&Open", "openBriefButton", mnemonic=True)
        self.brief_button = outline_button("&Brief this day…", "briefDayButton", mnemonic=True)
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.brief_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.confirm_panel = QWidget()
        self.confirm_panel.setObjectName("replaceConfirmation")
        self.confirm_panel.setAccessibleName("Replace the saved brief")
        confirm = QVBoxLayout(self.confirm_panel)
        confirm.setContentsMargins(0, 0, 0, 0)
        self.confirm_label = _plain()
        confirm.addWidget(self.confirm_label)
        choices = QHBoxLayout()
        self.replace_button = outline_button("&Replace", "replaceBriefButton", mnemonic=True)
        self.keep_button = outline_button("&Keep it", "keepBriefButton", mnemonic=True)
        choices.addWidget(self.replace_button)
        choices.addWidget(self.keep_button)
        choices.addStretch(1)
        confirm.addLayout(choices)
        layout.addWidget(self.confirm_panel)
        self.confirm_panel.hide()
        layout.addWidget(
            _plain(
                "A past day's brief covers only the messages still in your Inbox when it is "
                "made. Briefing sends messages to Groq only after you approve, as for today."
            )
        )
        self.status = _plain()
        layout.addWidget(self.status)
        self.saved.currentRowChanged.connect(lambda row: self._chose(self.saved, row))
        self.missed.currentRowChanged.connect(lambda row: self._chose(self.missed, row))
        self.saved.itemActivated.connect(lambda _item: self._open())
        self.open_button.clicked.connect(self._open)
        self.brief_button.clicked.connect(self._brief)
        self.replace_button.clicked.connect(self._replace)
        self.keep_button.clicked.connect(self.confirm_panel.hide)
        self._update_buttons()

    def configure(
        self,
        summaries: tuple[SavedBriefSummary, ...],
        missed: tuple[date, ...],
        account_email: str | None,
        today: date,
    ) -> None:
        """Show saved briefs and, for the connected account, its missed days."""
        self._summaries = summaries
        self._account, self._today = account_email, today
        self.confirm_panel.hide()
        self.status.clear()
        self.saved.blockSignals(True)
        self.saved.clear()
        for summary in summaries:
            self.saved.addItem(QListWidgetItem(summary_text(summary, self._shown_today())))
        self.saved.blockSignals(False)
        self._show_missed(missed)
        if not summaries:
            self.status.setText("No saved briefs yet.")
        if summaries:
            self.saved.setCurrentRow(0)
        self._update_buttons()

    def set_account(self, account_email: str | None) -> None:
        """Follow the connected account without reading storage. Missed days belong to an
        account, so another account's are dropped (the window reloads them); without one,
        the page says to connect. A replacement being confirmed is withdrawn."""
        if account_email == self._account:
            return
        self._account = account_email
        self._show_missed(None)
        self.confirm_panel.hide()
        if self.status.text() in (NEEDS_CONNECTION, OTHER_ACCOUNT):
            self.status.clear()  # It was about the account that changed.
        self._update_buttons()

    def _shown_today(self) -> date | None:
        """The owner's day for naming years, None until the page is configured."""
        return None if self._today == date.min else self._today

    def _show_missed(self, missed: tuple[date, ...] | None) -> None:
        """The missed-days list and its note, for ``self._account``. ``missed`` is None
        until that account's days are loaded; the note then claims nothing about them."""
        self._missed = missed or ()
        self.missed.blockSignals(True)
        self.missed.clear()
        for day in self._missed:
            self.missed.addItem(QListWidgetItem(f"{day_text(day, self._shown_today())} · no brief"))
        self.missed.blockSignals(False)
        if self._account is None:
            note = NOT_CONNECTED
        elif missed is None or missed:
            note = ""
        else:
            note = NONE_MISSED
        self.missed_note.setText(note)
        self.missed_note.setVisible(bool(note))
        self.missed.setVisible(bool(self._missed))  # The note says why there are none.

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_buttons()

    def _chose(self, widget: QListWidget, row: int) -> None:
        """Choosing in one list clears the other, so one day is chosen at a time."""
        if row >= 0:
            other = self.missed if widget is self.saved else self.saved
            other.blockSignals(True)
            other.setCurrentRow(-1)
            other.clearSelection()
            other.blockSignals(False)
        self.confirm_panel.hide()
        self._update_buttons()

    def _chosen_summary(self) -> SavedBriefSummary | None:
        row = self.saved.currentRow()
        return self._summaries[row] if 0 <= row < len(self._summaries) else None

    def _chosen_missed(self) -> date | None:
        row = self.missed.currentRow()
        return self._missed[row] if 0 <= row < len(self._missed) else None

    def _chosen_day(self) -> date | None:
        summary = self._chosen_summary()
        return summary.local_date if summary is not None else self._chosen_missed()

    def _can_brief(self, day: date | None) -> bool:
        """A past day within the catch-up window; today is briefed with Sync and review."""
        return day is not None and (
            self._today - timedelta(days=CATCH_UP_DAYS) <= day < self._today
        )

    def _update_buttons(self) -> None:
        self.saved.setEnabled(not self._busy)
        self.missed.setEnabled(not self._busy)
        self.open_button.setEnabled(not self._busy and self._chosen_summary() is not None)
        self.brief_button.setEnabled(not self._busy and self._can_brief(self._chosen_day()))
        self.replace_button.setEnabled(not self._busy)

    def _open(self) -> None:
        summary = self._chosen_summary()
        if summary is not None and not self._busy:
            self.open_requested.emit(summary.account_email, summary.local_date)

    def _brief(self) -> None:
        day = self._chosen_day()
        if self._busy or not self._can_brief(day):
            return
        assert day is not None
        if self._account is None:
            self.status.setText(NEEDS_CONNECTION)
            return
        summary = self._chosen_summary()
        if summary is None:
            self.generate_requested.emit(day)
            return
        if summary.account_email != self._account:
            self.status.setText(OTHER_ACCOUNT)
            return
        self.confirm_label.setText(
            f"This replaces the saved brief for {day_text(day, self._shown_today())}."
        )
        self.confirm_panel.show()
        self.keep_button.setFocus()

    def _replace(self) -> None:
        day = self._chosen_day()
        self.confirm_panel.hide()
        if day is not None and not self._busy:
            self.generate_requested.emit(day)
