"""The automatic-analysis dialog: how many messages an automatic run may send without asking
(ADR 0017).

Without this permission an automatic refresh only checks Gmail and counts the messages ready
to review. The dialog shows the disclosure the permission rests on and emits the owner's
choice; the window saves it through the backend. Everything shown is plain text.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from mailbrief.domain.briefs import AUTO_SEND_LIMIT_MAX, AutoSendStatus
from mailbrief.services.brief import disclosure_lines, permission_sentence

INTRO = (
    "By default an automatic refresh only checks Gmail, follows the threads of your open "
    "actions and tells you how many messages are ready to review. It downloads no message "
    "text and sends nothing to Groq. Here you can allow it to analyze a few new messages "
    "without asking you first."
)
OFF_TEXT = (
    "Automatic runs only check Gmail and count the messages ready to review. Every analysis "
    "asks you first."
)


def _plain(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class AutoSendDialog(QDialog):
    """``save_requested(limit)`` carries the number of messages chosen, 0 for none."""

    save_requested = Signal(int)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Automatic analysis")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(580, 440)
        self._status: AutoSendStatus | None = None
        layout = QVBoxLayout(self)
        layout.addWidget(_plain(INTRO))
        form = QFormLayout()
        self.limit = QSpinBox()
        self.limit.setRange(0, AUTO_SEND_LIMIT_MAX)
        self.limit.setSpecialValueText("Off")
        self.limit.setAccessibleName("Messages an automatic run may send without asking")
        form.addRow("&Messages per run", self.limit)
        layout.addLayout(form)
        self.disclosure = _plain()
        self.disclosure.setAccessibleName("What automatic runs may send")
        layout.addWidget(self.disclosure, 1)
        buttons = QHBoxLayout()
        self.save_button = QPushButton("&Save")
        self.cancel_button = QPushButton("&Cancel")
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)
        self.limit.valueChanged.connect(lambda _value: self._show_disclosure())
        self.save_button.clicked.connect(self._save)
        self.cancel_button.clicked.connect(self.reject)

    def configure(self, status: AutoSendStatus) -> None:
        """Open on the current permission, with the disclosure it rests on."""
        self._status = status
        self.limit.setValue(status.limit)
        self._show_disclosure()

    def _show_disclosure(self) -> None:
        """Off says nothing is sent without asking; any number shows what may be sent."""
        status = self._status
        limit = self.limit.value()
        if status is None or limit == 0:
            self.disclosure.setText(OFF_TEXT)
            return
        preview = status.disclosure.model_copy(update={"message_count": limit})
        lines = (
            *disclosure_lines(preview),
            permission_sentence(limit, status.account_email, preview.provider_name),
        )
        self.disclosure.setText("\n\n".join(lines))

    def _save(self) -> None:
        self.save_requested.emit(self.limit.value())
        self.accept()
