"""Write one draft or note: autosave as you type, saved versions, copy and export.

The editor never writes anything itself. It asks the window, which runs every draft write
through one serialized queue, and answers through the methods below. Source subjects and
senders come from email, so they are shown only in plain-text labels.
"""

from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
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

from mailbrief.domain.drafts import (
    DRAFT_BODY_MAX_CHARS,
    DRAFT_RECIPIENTS_MAX_CHARS,
    DRAFT_TITLE_MAX_CHARS,
    EMAIL_KINDS,
    KIND_NAMES,
    Draft,
    DraftEdit,
    DraftSource,
    DraftVersion,
    DraftVersionInfo,
    DraftVersionOrigin,
    export_filename,
    export_markdown,
    export_text,
    placeholders,
    recipient_warnings,
    still_to_fill,
)

DEBOUNCE_MS = 1_500
MAX_WAIT_MS = 10_000
RETRY_MS = 5_000
CONFLICT = "This draft changed elsewhere. Your text is still here."
NOT_SAVED = "Not saved; retrying"
_SAVING = "Saving…"
_TOO_LONG = (
    f"The body is over {DRAFT_BODY_MAX_CHARS:,} characters. Autosave is paused until it is shorter."
)
_DISCARD_HINT = "Press Close again to close without saving your latest changes."
_DISCARD_HINT_LOWER = "press Close again to close without saving your latest changes."
_ORIGINS = {
    DraftVersionOrigin.CREATED: "created",
    DraftVersionOrigin.EDITED: "saved",
    DraftVersionOrigin.RESTORED: "restored",
    DraftVersionOrigin.GENERATED: "generated",
}


def _plain(text: str = "") -> QLabel:
    """Draft and source text must never become rich text or a link."""
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def gmail_link(source: DraftSource) -> str | None:
    """The source's link when it opens Gmail; nothing else is ever opened."""
    link = source.web_link
    return str(link) if link.scheme == "https" and link.host == "mail.google.com" else None


class DraftEditor(QDialog):
    """Signals, each answered by the window through the methods named:

    - ``autosave_requested(edit)``: saved() or save_failed() or show_conflict();
    - ``checkpoint_requested()``: checkpointed();
    - ``versions_requested()`` and ``version_requested(number)``: show_versions() and
      show_version();
    - ``restore_requested(number)``: restored() or restore_failed();
    - ``save_as_new_requested(edit)``: continue_as();
    - ``export_requested(path, text)``: set_status();
    - ``close_requested(edit or None)``: finish_closed() or cancel_close().
    """

    autosave_requested = Signal(object)
    checkpoint_requested = Signal()
    versions_requested = Signal()
    version_requested = Signal(int)
    restore_requested = Signal(int)
    save_as_new_requested = Signal(object)
    export_requested = Signal(str, str)
    close_requested = Signal(object)

    def __init__(
        self,
        parent: QWidget,
        *,
        debounce_ms: int = DEBOUNCE_MS,
        max_wait_ms: int = MAX_WAIT_MS,
        retry_ms: int = RETRY_MS,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Draft")
        self.resize(720, 640)
        self.draft: Draft | None = None  # As last saved; its revision is what writes send.
        self._zone = ZoneInfo("UTC")
        self._loading = False
        self._dirty = False  # Changed since the last text sent to be saved.
        self._conflict = False
        self._closing = False
        self._locked = False  # A restore or close is waiting for the window.
        self._discard_armed = False
        self._versions: tuple[DraftVersionInfo, ...] = ()
        self._picker: QFileDialog | None = None
        layout = QVBoxLayout(self)
        context = QHBoxLayout()
        self.context = _plain()
        context.addWidget(self.context, 1)
        self.gmail_button = QPushButton("Open in &Gmail")
        self.gmail_button.clicked.connect(self._open_gmail)
        context.addWidget(self.gmail_button)
        layout.addLayout(context)
        self._fields = QWidget()
        form = QFormLayout(self._fields)
        form.setContentsMargins(0, 0, 0, 0)
        self._form = form
        self.to_edit = QLineEdit()
        self.to_edit.setMaxLength(DRAFT_RECIPIENTS_MAX_CHARS)
        self.to_edit.setAccessibleName("To")
        form.addRow("&To", self.to_edit)
        self.cc_edit = QLineEdit()
        self.cc_edit.setMaxLength(DRAFT_RECIPIENTS_MAX_CHARS)
        self.cc_edit.setAccessibleName("Cc")
        form.addRow("C&c", self.cc_edit)
        self.recipient_warning = _plain()
        form.addRow("", self.recipient_warning)
        self.title_label = QLabel("&Subject")
        self.title_edit = QLineEdit()
        self.title_edit.setMaxLength(DRAFT_TITLE_MAX_CHARS)
        self.title_label.setBuddy(self.title_edit)
        form.addRow(self.title_label, self.title_edit)
        self.body = QPlainTextEdit()
        self.body.setAccessibleName("Body")
        self.body.setTabChangesFocus(True)
        form.addRow("&Body", self.body)
        self.counter = _plain()
        form.addRow("", self.counter)
        self.placeholder_line = _plain()
        form.addRow("", self.placeholder_line)
        layout.addWidget(self._fields, 1)
        buttons = QHBoxLayout()
        self.copy_text_button = QPushButton("Copy te&xt")
        self.copy_subject_button = QPushButton("Copy s&ubject")
        self.export_button = QPushButton("&Export…")
        self.versions_button = QPushButton("&Versions…")
        self.checkpoint_button = QPushButton("Save versio&n")
        self.close_button = QPushButton("C&lose")
        for button in (
            self.copy_text_button,
            self.copy_subject_button,
            self.export_button,
            self.versions_button,
            self.checkpoint_button,
            self.close_button,
        ):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.save_as_new_button = QPushButton("Save as ne&w draft")
        self.save_as_new_button.hide()
        layout.addWidget(self.save_as_new_button)
        self.versions_panel = QWidget()
        versions = QVBoxLayout(self.versions_panel)
        versions.setContentsMargins(0, 0, 0, 0)
        versions.addWidget(_plain("Saved versions, newest first:"))
        self.versions_list = QListWidget()
        self.versions_list.setAccessibleName("Saved versions")
        versions.addWidget(self.versions_list)
        self.version_preview = QPlainTextEdit()
        self.version_preview.setReadOnly(True)
        self.version_preview.setAccessibleName("Selected version")
        versions.addWidget(self.version_preview)
        self.restore_button = QPushButton("&Restore this version…")
        versions.addWidget(self.restore_button)
        self.confirm_panel = QWidget()
        confirm = QHBoxLayout(self.confirm_panel)
        confirm.setContentsMargins(0, 0, 0, 0)
        self.confirm_label = _plain()
        self.confirm_button = QPushButton("Restore")
        self.keep_button = QPushButton("Keep my text")
        confirm.addWidget(self.confirm_label, 1)
        confirm.addWidget(self.confirm_button)
        confirm.addWidget(self.keep_button)
        versions.addWidget(self.confirm_panel)
        self.confirm_panel.hide()
        self.versions_panel.hide()
        layout.addWidget(self.versions_panel, 1)
        self.status = _plain()
        layout.addWidget(self.status)
        self._debounce = self._timer(debounce_ms)
        self._max_wait = self._timer(max_wait_ms)
        self._retry = self._timer(retry_ms)
        for field in (self.to_edit, self.cc_edit, self.title_edit):
            field.textChanged.connect(self._changed)
        self.body.textChanged.connect(self._changed)
        self.copy_text_button.clicked.connect(self._copy_text)
        self.copy_subject_button.clicked.connect(self._copy_subject)
        self.export_button.clicked.connect(self._choose_export)
        self.versions_button.clicked.connect(self._toggle_versions)
        self.checkpoint_button.clicked.connect(self._checkpoint)
        self.close_button.clicked.connect(self.reject)
        self.save_as_new_button.clicked.connect(self._save_as_new)
        self.versions_list.currentRowChanged.connect(self._version_selected)
        self.restore_button.clicked.connect(self._ask_restore)
        self.confirm_button.clicked.connect(self._confirm_restore)
        self.keep_button.clicked.connect(self.confirm_panel.hide)

    def _timer(self, interval_ms: int) -> QTimer:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(interval_ms)
        timer.timeout.connect(self._autosave_now)
        return timer

    def set_timing(self, *, debounce_ms: int, max_wait_ms: int, retry_ms: int) -> None:
        """Autosave 'debounce_ms' after the last change, at least every 'max_wait_ms' while
        changes continue, and 'retry_ms' after a failed save."""
        self._debounce.setInterval(debounce_ms)
        self._max_wait.setInterval(max_wait_ms)
        self._retry.setInterval(retry_ms)

    # State the window reads.

    @property
    def in_conflict(self) -> bool:
        return self._conflict

    @property
    def dirty(self) -> bool:
        return self._dirty

    def current_edit(self) -> DraftEdit | None:
        """The text as it stands, or None while the body is too long to save."""
        if len(self.body.toPlainText()) > DRAFT_BODY_MAX_CHARS:
            return None
        email = self.draft is not None and self.draft.kind in EMAIL_KINDS
        return DraftEdit(
            title=self.title_edit.text(),
            to_text=self.to_edit.text() if email else "",
            cc_text=self.cc_edit.text() if email else "",
            body=self.body.toPlainText(),
        )

    def final_edit(self) -> DraftEdit | None:
        """Unsaved text to save before quitting, if any can be saved."""
        if self.draft is None or self._conflict or not self._dirty:
            return None
        return self.current_edit()

    # Loading.

    def load(self, draft: Draft, zone: ZoneInfo) -> None:
        """Show a draft; nothing is kept from an earlier one."""
        self._stop_timers()
        self._zone = zone
        self.draft = draft
        self._dirty = self._conflict = self._closing = self._discard_armed = False
        self._set_locked(False)
        email = draft.kind in EMAIL_KINDS
        name = KIND_NAMES[draft.kind]
        self.setWindowTitle(f"{name} draft")
        for field in (self.to_edit, self.cc_edit, self.recipient_warning):
            self._form.setRowVisible(field, email)
        self.title_label.setText("&Subject" if email else "&Title")
        self.copy_subject_button.setVisible(email)
        self._show_context(draft)
        self._set_text(draft.content())
        self.save_as_new_button.hide()
        self.versions_panel.hide()
        self.confirm_panel.hide()
        self._update_buttons()
        self.status.setText(f"Saved {self._time(draft)}")
        (self.body if draft.title else self.title_edit).setFocus()

    def _show_context(self, draft: Draft) -> None:
        parts: list[str] = []
        source = draft.sources[0] if draft.sources else None
        if source is not None:
            parts.append(
                f"From email: {source.subject or '(no subject)'} — {source.sender_address}"
            )
            if not source.available:
                parts.append("source no longer in local mail")
        if draft.action_title:
            parts.append(f"For action: “{draft.action_title}”")
        self.context.setText(" · ".join(parts) or "Not linked to an email or action.")
        self.gmail_button.setVisible(source is not None and gmail_link(source) is not None)

    def _set_text(self, content: DraftEdit) -> None:
        self._loading = True
        try:
            self.to_edit.setText(content.to_text)
            self.cc_edit.setText(content.cc_text)
            self.title_edit.setText(content.title)
            self.body.setPlainText(content.body)
        finally:
            self._loading = False
        self._update_live()

    def _time(self, draft: Draft) -> str:
        return f"{draft.updated_at_utc.astimezone(self._zone):%H:%M}"

    # Typing and autosave.

    def _changed(self) -> None:
        if self._loading:
            return
        self._update_live()
        if self.draft is None or self._conflict:
            return
        self._dirty = True
        self._discard_armed = False
        if self.current_edit() is None:
            self._stop_timers()
            self.status.setText(_TOO_LONG)
            return
        self.status.setText(_SAVING)
        self._debounce.start()
        if not self._max_wait.isActive():
            self._max_wait.start()

    def _update_live(self) -> None:
        body = self.body.toPlainText()
        self.counter.setText(f"{len(body):,} / {DRAFT_BODY_MAX_CHARS:,} characters")
        warnings = recipient_warnings(f"{self.to_edit.text()},{self.cc_edit.text()}")
        self.recipient_warning.setText(
            "Check these recipients: " + "; ".join(warnings) if warnings else ""
        )
        found = self._placeholders()
        self.placeholder_line.setText("Placeholders to fill: " + ", ".join(found) if found else "")

    def _placeholders(self) -> tuple[str, ...]:
        email = self.draft is not None and self.draft.kind in EMAIL_KINDS
        fields = [self.title_edit.text(), self.body.toPlainText()]
        if email:
            fields[:0] = [self.to_edit.text(), self.cc_edit.text()]
        return placeholders("\n".join(fields))

    def _stop_timers(self) -> None:
        for timer in (self._debounce, self._max_wait, self._retry):
            timer.stop()

    def _autosave_now(self) -> None:
        """Send the current text to be saved, when there is something that can be saved."""
        self._stop_timers()
        if self.draft is None or self._conflict or self._closing or not self._dirty:
            return
        edit = self.current_edit()
        if edit is None:
            self.status.setText(_TOO_LONG)
            return
        self._dirty = False
        self.status.setText(_SAVING)
        self.autosave_requested.emit(edit)

    def saved(self, draft: Draft, edit: DraftEdit) -> None:
        """An autosave of ``edit`` succeeded."""
        self.draft = draft
        self._discard_armed = False
        if not self._dirty and not self._closing and self.current_edit() == edit:
            self.status.setText(f"Saved {self._time(draft)}")

    def save_failed(self) -> None:
        """An autosave failed: keep the text and retry on the next change or soon."""
        self._dirty = True
        self.status.setText(NOT_SAVED)
        if not self._closing:
            self._retry.start()

    def show_conflict(self) -> None:
        """The draft changed elsewhere: stop autosaving and offer to save as a new draft."""
        self._conflict = True
        self._stop_timers()
        self.status.setText(CONFLICT)
        self.save_as_new_button.show()
        self._update_buttons()

    def continue_as(self, draft: Draft) -> None:
        """Keep editing ``draft``, the new copy saved after a conflict."""
        self.draft = draft
        self._conflict = False
        self._discard_armed = False
        self.save_as_new_button.hide()
        self._update_buttons()
        self._dirty = self.current_edit() != draft.content()
        self.status.setText(f"Saved as a new draft at {self._time(draft)}.")
        if self._dirty:
            self._debounce.start()

    def _save_as_new(self) -> None:
        edit = self.current_edit()
        if edit is None:
            self.status.setText(f"Shorten the body to {DRAFT_BODY_MAX_CHARS:,} characters first.")
            return
        self.status.setText(_SAVING)
        self.save_as_new_requested.emit(edit)

    # Versions.

    def _update_buttons(self) -> None:
        live = not self._conflict and not self._locked
        for button in (self.versions_button, self.checkpoint_button, self.restore_button):
            button.setEnabled(live)
        self.confirm_button.setEnabled(live)

    def _set_locked(self, locked: bool) -> None:
        self._locked = locked
        self._fields.setEnabled(not locked)
        self.close_button.setEnabled(not locked)
        self.save_as_new_button.setEnabled(not locked)
        self._update_buttons()

    def _checkpoint(self) -> None:
        if self.current_edit() is None:
            self.status.setText(_TOO_LONG)
            return
        self._autosave_now()
        self.checkpoint_requested.emit()

    def checkpointed(self, info: DraftVersionInfo | None) -> None:
        if info is None:
            self.status.setText("No changes since the last saved version.")
        else:
            self.status.setText(f"Saved version {info.number}.")
        if not self.versions_panel.isHidden():
            self.versions_requested.emit()

    def _toggle_versions(self) -> None:
        if not self.versions_panel.isHidden():
            self.versions_panel.hide()
            return
        self.versions_requested.emit()

    def show_versions(self, versions: tuple[DraftVersionInfo, ...]) -> None:
        self._versions = versions
        self.confirm_panel.hide()
        self.versions_list.blockSignals(True)
        self.versions_list.clear()
        for info in versions:
            when = info.created_at_utc.astimezone(self._zone).strftime("%Y-%m-%d %H:%M")
            preview = info.preview or "(empty)"
            self.versions_list.addItem(
                QListWidgetItem(
                    f"Version {info.number} · {_ORIGINS[info.origin]} {when} · "
                    f"{info.length:,} characters · {preview}"
                )
            )
        self.versions_list.blockSignals(False)
        self.version_preview.clear()
        self.versions_panel.show()
        if versions:
            self.versions_list.setCurrentRow(0)
        self.restore_button.setEnabled(bool(versions) and not self._conflict and not self._locked)

    def _selected_version(self) -> DraftVersionInfo | None:
        row = self.versions_list.currentRow()
        return self._versions[row] if 0 <= row < len(self._versions) else None

    def _version_selected(self, _row: int) -> None:
        self.confirm_panel.hide()
        info = self._selected_version()
        if info is not None:
            self.version_requested.emit(info.number)

    def show_version(self, version: DraftVersion) -> None:
        info = self._selected_version()
        if info is not None and info.number == version.number:
            self.version_preview.setPlainText(version.body)

    def _ask_restore(self) -> None:
        info = self._selected_version()
        if info is None:
            return
        self.confirm_label.setText(
            f"Replace your text with version {info.number}? "
            "Your current text is saved as a version first."
        )
        self.confirm_button.setText(f"Restore version {info.number}")
        self.confirm_panel.show()

    def _confirm_restore(self) -> None:
        info = self._selected_version()
        if info is None or self._conflict or self._locked:
            return
        if self.current_edit() is None:
            self.status.setText(_TOO_LONG)
            return
        self.confirm_panel.hide()
        self._autosave_now()
        self._set_locked(True)
        self.status.setText(f"Restoring version {info.number}…")
        self.restore_requested.emit(info.number)

    def restored(self, draft: Draft, number: int) -> None:
        """The restore succeeded: show the restored text."""
        self.draft = draft
        self._set_locked(False)
        self._set_text(draft.content())
        self._dirty = False
        self.status.setText(f"Restored version {number}. Your earlier text was saved as a version.")
        self.versions_requested.emit()

    def restore_failed(self, message: str) -> None:
        self._set_locked(False)
        if not self._conflict:
            self.status.setText(message)

    # Copy, export and Gmail.

    def _copied(self, what: str) -> None:
        count = len(self._placeholders())
        self.set_status(f"Copied the {what}." + (f" {still_to_fill(count)}" if count else ""))

    def _copy_text(self) -> None:
        QGuiApplication.clipboard().setText(self.body.toPlainText())
        self._copied("text")

    def _copy_subject(self) -> None:
        QGuiApplication.clipboard().setText(self.title_edit.text())
        self._copied("subject")

    def export_document(self, path: str) -> str:
        """The draft as it stands now, as Markdown for a .md path and plain text otherwise."""
        draft = self.draft
        if draft is None:
            return ""
        email = draft.kind in EMAIL_KINDS
        current = draft.model_copy(
            update={
                "title": self.title_edit.text(),
                "to_text": self.to_edit.text() if email else "",
                "cc_text": self.cc_edit.text() if email else "",
                "body": self.body.toPlainText(),
            }
        )
        return export_markdown(current) if path.lower().endswith(".md") else export_text(current)

    def _choose_export(self) -> None:
        draft = self.draft
        if draft is None:
            return
        self._picker = QFileDialog(self, "Export draft")
        self._picker.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        self._picker.setNameFilters(["Markdown (*.md)", "Plain text (*.txt)"])
        self._picker.setDefaultSuffix("md")
        self._picker.selectFile(export_filename(draft, ".md"))
        self._picker.fileSelected.connect(self.export_to)
        self._picker.open()

    def export_to(self, path: str) -> None:
        """Ask the window to write the draft to ``path``, which the owner chose."""
        if self.draft is None or not path:
            return
        self.status.setText("Exporting…")
        self.export_requested.emit(path, self.export_document(path))

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    def _open_gmail(self) -> None:
        draft = self.draft
        link = None if draft is None or not draft.sources else gmail_link(draft.sources[0])
        if link is not None:
            QDesktopServices.openUrl(QUrl(link))

    # Closing.

    def reject(self) -> None:
        """Close, Esc and the title-bar button ask the window to save and save a version;
        the editor closes once it confirms. After a failure, closing again discards."""
        if self._closing or self._locked:
            return
        if self._discard_armed or self.draft is None:
            self._close_now()
            return
        if self._conflict:
            self._discard_armed = True
            self.status.setText(f"{CONFLICT} Save it as a new draft, or {_DISCARD_HINT_LOWER}")
            return
        edit = self.current_edit()
        if edit is None:
            self._discard_armed = True
            self.status.setText(f"{_TOO_LONG} {_DISCARD_HINT}")
            return
        self._stop_timers()
        self._closing = True
        self._set_locked(True)
        self.status.setText(_SAVING)
        unsaved = edit if self._dirty else None
        self._dirty = False
        self.close_requested.emit(unsaved)

    def finish_closed(self) -> None:
        """The window saved the text and a version: close."""
        self._close_now()

    def cancel_close(self, message: str | None = None) -> None:
        """The save before closing failed: stay open with the text; Close again discards."""
        self._closing = False
        self._dirty = True
        self._discard_armed = True
        self._set_locked(False)
        if message is not None:
            self.status.setText(f"{message} {_DISCARD_HINT}")
        elif self._conflict:
            self.status.setText(f"{CONFLICT} {_DISCARD_HINT}")

    def force_close(self) -> None:
        """Close at once, as the window does when it quits after queueing a final save."""
        self._close_now()

    def _close_now(self) -> None:
        self._stop_timers()
        self._closing = False
        self._set_locked(False)
        super().reject()
