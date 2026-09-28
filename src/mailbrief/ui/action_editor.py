"""Edit one action: its own fields and its plan, saved together as one revision."""

from datetime import date
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from mailbrief.domain.actions import (
    ACTION_NOTES_MAX_CHARS,
    ACTION_STEP_MAX_CHARS,
    ACTION_TITLE_MAX_CHARS,
    MAX_ACTION_STEPS,
    TARGET_REASON_TEXT,
    Action,
    ActionEdit,
    StepEdit,
)
from mailbrief.domain.analysis import ActionEffort, ActionOwnership, DeadlinePrecision

_OWNERS = ((ActionOwnership.MINE, "Mine"), (ActionOwnership.WAITING_FOR, "Waiting for someone"))
_EFFORTS: tuple[tuple[ActionEffort | None, str], ...] = (
    (None, "Not set"),
    (ActionEffort.MINUTES, "Minutes"),
    (ActionEffort.HOURS, "Hours"),
    (ActionEffort.DAYS, "Days"),
)
_STEP_ID = Qt.ItemDataRole.UserRole


def _plain(text: str) -> QLabel:
    """Action and source text must never become rich text or a link."""
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _deadline(action: Action, owner_zone: ZoneInfo | None) -> str:
    """An exact deadline in the owner's zone, like the brief, naming the email's own time
    when the email stated it in another zone."""
    at, zone = action.deadline_at_utc, action.deadline_timezone
    if action.deadline_precision is DeadlinePrecision.DATETIME and at and zone:
        stated = at.astimezone(ZoneInfo(zone))  # The zone the time was stated in, not UTC.
        shown = at.astimezone(owner_zone) if owner_zone is not None else stated
        if owner_zone is None or owner_zone.key == zone:
            return f"{shown.isoformat(timespec='minutes')} (as the email states)"
        return f"{shown.isoformat(timespec='minutes')} ({stated:%H:%M} {zone} as the email states)"
    if action.deadline_precision is DeadlinePrecision.DATE and action.deadline_date:
        return f"{action.deadline_date.isoformat()} (date only)"
    if action.deadline_text:
        return f"“{action.deadline_text}” (no specific day)"
    return "none stated"


class ActionEditor(QDialog):
    """``save_requested(action, edit, steps)``: steps is None when the plan is unchanged.

    The dialog validates what it can; the service still checks every bound and the revision.
    """

    save_requested = Signal(object, object, object)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit action")
        self._action: Action | None = None
        self._original: list[tuple[int | None, str, bool]] = []
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.title = QLineEdit()
        self.title.setMaxLength(ACTION_TITLE_MAX_CHARS)
        form.addRow("&Title", self.title)
        self.owner = QComboBox()
        for _value, name in _OWNERS:
            self.owner.addItem(name)
        form.addRow("&Whose", self.owner)
        self.effort = QComboBox()
        for _effort, name in _EFFORTS:
            self.effort.addItem(name)
        form.addRow("Eff&ort", self.effort)
        self.has_target = QCheckBox("Target &date")
        self.target = QDateEdit()
        self.target.setCalendarPopup(True)
        self.target.setDisplayFormat("yyyy-MM-dd")
        self.has_target.toggled.connect(self.target.setEnabled)
        target_row = QHBoxLayout()
        target_row.addWidget(self.has_target)
        target_row.addWidget(self.target)
        form.addRow("Target", target_row)
        self.deadline = _plain("")
        form.addRow("Deadline", self.deadline)
        self.suggested = _plain("")
        form.addRow("Suggested", self.suggested)
        self.sources = _plain("")
        form.addRow("From", self.sources)
        self.notes = QPlainTextEdit()
        self.notes.setTabChangesFocus(True)
        form.addRow("&Notes", self.notes)
        layout.addLayout(form)
        layout.addWidget(
            _plain("Plan (check a step when it's done; double-click a step to rename it):")
        )
        self.steps = QListWidget()
        self.steps.setAccessibleName("Plan steps")
        layout.addWidget(self.steps)
        step_buttons = QHBoxLayout()
        self.add_step = QPushButton("&Add step")
        self.remove_step = QPushButton("&Remove step")
        self.up = QPushButton("Move &up")
        self.down = QPushButton("Move do&wn")
        for button in (self.add_step, self.remove_step, self.up, self.down):
            step_buttons.addWidget(button)
        layout.addLayout(step_buttons)
        self.status = _plain("")
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        layout.addWidget(self.buttons)
        self.add_step.clicked.connect(self._add_step)
        self.remove_step.clicked.connect(self._remove_step)
        self.up.clicked.connect(lambda: self._move(-1))
        self.down.clicked.connect(lambda: self._move(1))
        self.steps.currentRowChanged.connect(lambda _row: self._update_step_buttons())
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        self._busy = False

    def edit(self, action: Action, zone: ZoneInfo | None = None) -> None:
        """Show one action's current values; nothing is kept from an earlier edit.

        ``zone`` is the owner's, so an exact deadline reads like it does in the brief.
        """
        self._action = action
        self.title.setText(action.title)
        self.owner.setCurrentIndex([value for value, _ in _OWNERS].index(action.ownership))
        self.effort.setCurrentIndex([value for value, _ in _EFFORTS].index(action.effort))
        self.has_target.setChecked(action.target_date is not None)
        self.target.setEnabled(action.target_date is not None)
        shown = action.target_date or action.suggested_target_date or date.today()
        self.target.setDate(QDate(shown.year, shown.month, shown.day))
        self.deadline.setText(_deadline(action, zone))
        if action.suggested_target_date is not None and action.target_reason is not None:
            self.suggested.setText(
                f"{action.suggested_target_date.isoformat()}, "
                f"{TARGET_REASON_TEXT[action.target_reason]}"
            )
        else:
            self.suggested.setText("no target was suggested")
        self.sources.setText(
            "; ".join(
                f"{source.subject or '(no subject)'} — {source.sender_address}"
                + ("" if source.available else " (no longer in local mail)")
                for source in action.sources
            )
            or "no source"
        )
        self.notes.setPlainText(action.notes)
        self.steps.clear()
        for step in action.steps:
            self._append_step(step.text, step.done, step.step_id)
        self._original = self._current_steps()
        self.status.clear()
        self._update_step_buttons()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        save = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        if save is not None:
            save.setEnabled(not busy)

    def _append_step(self, text: str, done: bool, step_id: int | None) -> QListWidgetItem:
        item = QListWidgetItem(text)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEditable)
        item.setCheckState(Qt.CheckState.Checked if done else Qt.CheckState.Unchecked)
        item.setData(_STEP_ID, step_id)
        self.steps.addItem(item)
        return item

    def _current_steps(self) -> list[tuple[int | None, str, bool]]:
        found: list[tuple[int | None, str, bool]] = []
        for index in range(self.steps.count()):
            item = self.steps.item(index)
            if item is not None:
                step_id = item.data(_STEP_ID)
                found.append(
                    (
                        step_id if isinstance(step_id, int) else None,
                        item.text().strip(),
                        item.checkState() == Qt.CheckState.Checked,
                    )
                )
        return found

    def _update_step_buttons(self) -> None:
        row = self.steps.currentRow()
        self.add_step.setEnabled(self.steps.count() < MAX_ACTION_STEPS)
        self.remove_step.setEnabled(row >= 0)
        self.up.setEnabled(row > 0)
        self.down.setEnabled(0 <= row < self.steps.count() - 1)

    def _add_step(self) -> None:
        if self.steps.count() >= MAX_ACTION_STEPS:
            return
        item = self._append_step("New step", False, None)
        self.steps.setCurrentItem(item)
        self.steps.editItem(item)
        self._update_step_buttons()

    def _remove_step(self) -> None:
        row = self.steps.currentRow()
        if row >= 0:
            self.steps.takeItem(row)
        self._update_step_buttons()

    def _move(self, offset: int) -> None:
        row = self.steps.currentRow()
        target = row + offset
        if row < 0 or not 0 <= target < self.steps.count():
            return
        item = self.steps.takeItem(row)
        self.steps.insertItem(target, item)
        self.steps.setCurrentRow(target)
        self._update_step_buttons()

    def _save(self) -> None:
        action = self._action
        if action is None or self._busy:
            return
        title = self.title.text().strip()
        notes = self.notes.toPlainText()
        steps = [step for step in self._current_steps() if step[1]]
        if not title:
            self.status.setText("Enter a title.")
            return
        if len(notes) > ACTION_NOTES_MAX_CHARS:
            self.status.setText(f"Notes can be at most {ACTION_NOTES_MAX_CHARS:,} characters.")
            return
        if any(len(text) > ACTION_STEP_MAX_CHARS for _, text, _ in steps):
            self.status.setText(f"A step can be at most {ACTION_STEP_MAX_CHARS} characters.")
            return
        picked = self.target.date()
        edit = ActionEdit(
            title=title,
            ownership=_OWNERS[self.owner.currentIndex()][0],
            effort=_EFFORTS[self.effort.currentIndex()][0],
            target_date=(
                date(picked.year(), picked.month(), picked.day())
                if self.has_target.isChecked()
                else None
            ),
            notes=notes,
        )
        plan = (
            None
            if steps == self._original
            else [StepEdit(step_id=step_id, text=text, done=done) for step_id, text, done in steps]
        )
        self.save_requested.emit(action, edit, plan)
        self.accept()
