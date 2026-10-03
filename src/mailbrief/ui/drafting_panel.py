"""The "Write with AI" panel inside the draft editor (ADR 0013).

The owner chooses what may be sent, sees exactly what will be sent, and approves it. The
panel only emits requests; the editor and window do the work. The preview is plain text.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from mailbrief.domain.drafting import (
    INSTRUCTIONS_MAX_CHARS,
    DraftContextPart,
    DraftingOptions,
    DraftingPreview,
)
from mailbrief.domain.drafts import DraftKind, DraftLength, DraftTone

TONE_CHOICES = (
    (DraftTone.NEUTRAL, "Neutral"),
    (DraftTone.WARM, "Warm"),
    (DraftTone.FORMAL, "Formal"),
    (DraftTone.DIRECT, "Direct"),
)
LENGTH_CHOICES = (
    (DraftLength.SHORT, "Short (up to about 80 words)"),
    (DraftLength.MEDIUM, "Medium (about 80–200 words)"),
    (DraftLength.LONG, "Long (about 200–400 words)"),
)
REPLY_EMAIL_LABEL = "The email you're replying to (downloaded from Gmail now)"
SOURCE_EMAIL_LABEL = "The email this draft is about (downloaded from Gmail now)"


def _plain(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class DraftingPanel(QWidget):
    """Choose, preview, approve.

    ``prepare_requested(options)`` asks for a preview; ``send_requested(agreed)`` approves
    it, with whether the owner ticked the first-use consent; ``cancel_requested()`` backs
    out of a preview or stops a generation.
    """

    prepare_requested = Signal(object)
    send_requested = Signal(bool)
    cancel_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._first_use = False
        self._defaults = (DraftTone.NEUTRAL, DraftLength.MEDIUM)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(
            _plain(
                "Groq writes a new version from the parts you tick. You see exactly what is "
                "sent before anything leaves this device, and your text is kept as a version."
            )
        )
        self.form = QWidget()
        form = QFormLayout(self.form)
        form.setContentsMargins(0, 0, 0, 0)
        self.email_box = QCheckBox(REPLY_EMAIL_LABEL)
        self.action_box = QCheckBox("The linked action")
        self.text_box = QCheckBox("My current text")
        parts = QVBoxLayout()
        for box in (self.email_box, self.action_box, self.text_box):
            parts.addWidget(box)
        self.no_parts = _plain("Nothing else to send: Groq writes from your instructions only.")
        parts.addWidget(self.no_parts)
        form.addRow("Send", parts)
        self.tone = QComboBox()
        for _value, name in TONE_CHOICES:
            self.tone.addItem(name)
        form.addRow("T&one", self.tone)
        self.length = QComboBox()
        for _length, name in LENGTH_CHOICES:
            self.length.addItem(name)
        form.addRow("Len&gth", self.length)
        self.instructions = QPlainTextEdit()
        self.instructions.setAccessibleName("Instructions for Groq")
        self.instructions.setPlaceholderText("For example: accept, and ask for the numbers.")
        self.instructions.setTabChangesFocus(True)
        self.instructions.setMaximumHeight(80)
        form.addRow("&Instructions", self.instructions)
        self.instructions_counter = _plain()
        form.addRow("", self.instructions_counter)
        layout.addWidget(self.form)
        self.continue_button = QPushButton("C&ontinue")
        layout.addWidget(self.continue_button)
        self.preview = QWidget()
        preview = QVBoxLayout(self.preview)
        preview.setContentsMargins(0, 0, 0, 0)
        self.preview_text = QPlainTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setAccessibleName("What will be sent")
        preview.addWidget(self.preview_text)
        self.agree_box = QCheckBox("I agree to send these parts to Groq for AI drafting")
        preview.addWidget(self.agree_box)
        self.send_button = QPushButton("Send to Groq")
        preview.addWidget(self.send_button)
        layout.addWidget(self.preview)
        self.working = _plain()
        layout.addWidget(self.working)
        self.cancel_button = QPushButton("Cancel")
        layout.addWidget(self.cancel_button)
        self.instructions.textChanged.connect(self._update_counter)
        self.continue_button.clicked.connect(self._continue)
        self.agree_box.toggled.connect(self._update_send)
        self.send_button.clicked.connect(
            lambda: self.send_requested.emit(self.agree_box.isChecked())
        )
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        self._apply_defaults()
        self.reset()

    def set_defaults(self, tone: DraftTone, length: DraftLength) -> None:
        """The owner's saved tone and length, chosen each time the panel is offered."""
        self._defaults = (tone, length)

    def _apply_defaults(self) -> None:
        tone, length = self._defaults
        self.tone.setCurrentIndex([value for value, _ in TONE_CHOICES].index(tone))
        self.length.setCurrentIndex([value for value, _ in LENGTH_CHOICES].index(length))

    def offer(self, parts: frozenset[DraftContextPart], kind: DraftKind) -> None:
        """Show the parts this draft can send, all ticked, with the saved tone and length,
        and go back to choosing.

        The defaults apply here rather than in reset(): reset() also runs after a cancel or
        a failure, when the owner's own choice of tone and length must stay.
        """
        self.email_box.setText(REPLY_EMAIL_LABEL if kind is DraftKind.REPLY else SOURCE_EMAIL_LABEL)
        for part, box in self._boxes():
            box.setVisible(part in parts)
            box.setChecked(part in parts)
        self.no_parts.setVisible(not parts)
        self._apply_defaults()
        self.reset()

    def _boxes(self) -> tuple[tuple[DraftContextPart, QCheckBox], ...]:
        return (
            (DraftContextPart.SOURCE_EMAIL, self.email_box),
            (DraftContextPart.ACTION, self.action_box),
            (DraftContextPart.CURRENT_TEXT, self.text_box),
        )

    def reset(self) -> None:
        """Back to choosing: the form is editable and there is no preview."""
        self.form.setEnabled(True)
        self.continue_button.show()
        self.continue_button.setEnabled(True)
        self.preview.hide()
        self.preview_text.clear()
        self.agree_box.setChecked(False)
        self.working.hide()
        self.cancel_button.hide()
        self._update_counter()

    def options(self) -> DraftingOptions | None:
        """The owner's choices, or None while the instructions are too long."""
        instructions = self.instructions.toPlainText()
        if len(instructions) > INSTRUCTIONS_MAX_CHARS:
            return None
        return DraftingOptions(
            parts=frozenset(
                part for part, box in self._boxes() if not box.isHidden() and box.isChecked()
            ),
            tone=TONE_CHOICES[self.tone.currentIndex()][0],
            length=LENGTH_CHOICES[self.length.currentIndex()][0],
            instructions=instructions.strip(),
        )

    def _update_counter(self) -> None:
        count = len(self.instructions.toPlainText())
        over = " — too long" if count > INSTRUCTIONS_MAX_CHARS else ""
        self.instructions_counter.setText(f"{count:,} / {INSTRUCTIONS_MAX_CHARS:,}{over}")
        self.continue_button.setEnabled(count <= INSTRUCTIONS_MAX_CHARS)

    def _continue(self) -> None:
        options = self.options()
        if options is None:
            return
        self.form.setEnabled(False)
        self.continue_button.setEnabled(False)
        self.working.setText("Preparing what will be sent…")
        self.working.show()
        self.cancel_button.show()
        self.prepare_requested.emit(options)

    def show_preview(self, preview: DraftingPreview, lines: tuple[str, ...]) -> None:
        """Show exactly what will be sent; first use needs the consent box ticked."""
        self._first_use = preview.first_use
        self.continue_button.hide()
        self.working.hide()
        self.preview_text.setPlainText("\n".join(lines))
        self.agree_box.setVisible(preview.first_use)
        self.agree_box.setChecked(False)
        self.preview.show()
        self.cancel_button.show()
        self._update_send()

    def _update_send(self) -> None:
        self.send_button.setEnabled(not self._first_use or self.agree_box.isChecked())

    def generating(self) -> None:
        """Locked while Groq writes; Cancel stops it."""
        self.preview.hide()
        self.working.setText("Writing with Groq…")
        self.working.show()
        self.cancel_button.show()
