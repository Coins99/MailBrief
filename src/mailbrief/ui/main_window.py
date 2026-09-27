"""Responsive desktop workflow with explicit metadata review and cloud consent."""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Protocol

from pydantic import SecretStr
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QGridLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from mailbrief.domain.briefs import BriefRunResult, BriefStatus, TransmissionPreview
from mailbrief.domain.cached_mail import CachedAccount, CachedMailPage
from mailbrief.domain.digests import DailyDigest, DigestStatus, SyncProgress, SyncStatus
from mailbrief.domain.messages import RankedMessage
from mailbrief.errors import ConfigurationError
from mailbrief.ports.errors import AuthenticationRequiredError, ProviderError
from mailbrief.services.brief import ConsentGate, ShortlistGate, disclosure_lines
from mailbrief.services.ranking import MAX_SHORTLIST_SIZE
from mailbrief.ui.cached_view import CachedMailDialog
from mailbrief.ui.digest_view import DigestView
from mailbrief.ui.preferences import DesktopPreferences
from mailbrief.ui.settings_view import SettingsDialog


class DesktopBackend(Protocol):
    async def cached_accounts(self) -> tuple[CachedAccount, ...]: ...
    async def cached_messages(
        self, account_id: int, day: date, offset: int = 0
    ) -> CachedMailPage: ...
    async def get_preferences(self) -> DesktopPreferences: ...
    async def save_preferences(self, preferences: DesktopPreferences) -> None: ...
    async def save_key(self, key: SecretStr) -> None: ...
    async def remove_key(self) -> None: ...
    async def revoke_consent(self) -> int: ...
    async def load_saved(self) -> DailyDigest | None: ...
    async def connect(self, *, silent_only: bool) -> str: ...
    async def ai_status(self) -> str: ...
    async def disconnect(self) -> None: ...
    async def generate(
        self,
        gate: ConsentGate,
        review: ShortlistGate,
        cancel: asyncio.Event,
        progress: Callable[[SyncProgress], None],
    ) -> BriefRunResult: ...
    async def close(self) -> None: ...


def plain_label(text: str) -> QLabel:
    """Mail-derived text must never become rich text or an automatic hyperlink."""
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class MainWindow(QMainWindow):
    """Keep the last saved brief visible throughout refresh, failure and cancellation."""

    closing = Signal()

    def __init__(self, backend: DesktopBackend) -> None:
        super().__init__()
        self.backend = backend
        self.task: asyncio.Task[None] | None = None
        self._cancel = asyncio.Event()
        self._review: asyncio.Future[tuple[str, ...] | None] | None = None
        self._consent: asyncio.Future[bool] | None = None
        self._ready = False
        self._closing = False
        self._cancellable = True
        self.cached_dialog = CachedMailDialog(self)
        self.cached_dialog.page_requested.connect(self._request_cached_page)
        self.settings_dialog = SettingsDialog(self)
        self.settings_dialog.save_requested.connect(self._request_save_preferences)
        self.settings_dialog.key_requested.connect(self._request_save_key)
        self.settings_dialog.remove_key_requested.connect(
            lambda: self.start(self._remove_key, cancellable=False)
        )
        self.settings_dialog.revoke_requested.connect(
            lambda: self.start(self._revoke_consent, cancellable=False)
        )
        self.setWindowTitle("MailBrief")
        self.resize(980, 760)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        title = plain_label("Your daily mail brief")
        font = title.font()
        font.setPointSize(20)
        title.setFont(font)
        layout.addWidget(title)
        self.connection = plain_label("Gmail: checking saved session…")
        self.ai = plain_label("AI: checking configuration…")
        layout.addWidget(self.connection)
        layout.addWidget(self.ai)
        actions = QGridLayout()
        self.connect_button = QPushButton("&Connect Gmail")
        self.disconnect_button = QPushButton("&Disconnect")
        self.generate_button = QPushButton("&Sync and review")
        self.generate_button.setToolTip("Refresh today's Inbox or retry an incomplete run.")
        self.cancel_button = QPushButton("&Cancel")
        self.settings_button = QPushButton("Se&ttings")
        for index, button in enumerate(
            (
                self.connect_button,
                self.disconnect_button,
                self.settings_button,
                self.generate_button,
                self.cancel_button,
            )
        ):
            actions.addWidget(button, index // 3, index % 3)
        layout.addLayout(actions)
        self.cached_button = QPushButton("Browse saved &mail (offline)")
        actions.addWidget(self.cached_button, 1, 2)
        self.status = plain_label("Loading saved brief…")
        layout.addWidget(self.status)
        self.review_panel = QWidget()
        review_layout = QVBoxLayout(self.review_panel)
        review_layout.addWidget(
            plain_label(
                "Review today's Inbox. Suggested messages are checked; add or remove any "
                "message, up to ten. Only checked messages will have their bodies retrieved."
            )
        )
        self.shortlist = QListWidget()
        self.shortlist.setAccessibleName("Messages selected for analysis")
        self.shortlist.itemChanged.connect(self._selection_changed)
        review_layout.addWidget(self.shortlist)
        self.review_button = QPushButton("&Continue with selected messages")
        review_layout.addWidget(self.review_button)
        layout.addWidget(self.review_panel)
        self.review_panel.hide()
        self.consent_panel = QWidget()
        consent_layout = QVBoxLayout(self.consent_panel)
        self.disclosure = plain_label("")
        consent_layout.addWidget(self.disclosure)
        self.approve_button = QPushButton("&Approve transmission to Groq")
        self.decline_button = QPushButton("&Decline")
        consent_layout.addWidget(self.approve_button)
        consent_layout.addWidget(self.decline_button)
        layout.addWidget(self.consent_panel)
        self.consent_panel.hide()
        self.digest = DigestView()
        self.digest.setMinimumHeight(180)
        layout.addWidget(self.digest, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        self.setCentralWidget(scroll)
        self.connect_button.clicked.connect(lambda: self.start(self._connect))
        self.disconnect_button.clicked.connect(
            lambda: self.start(self._disconnect, cancellable=False)
        )
        self.settings_button.clicked.connect(lambda: self.start(self._open_settings))
        self.cached_button.clicked.connect(lambda: self.start(self._open_cached))
        self.generate_button.clicked.connect(lambda: self.start(self._generate))
        self.cancel_button.clicked.connect(self.cancel)
        self.review_button.clicked.connect(self._accept_review)
        self.approve_button.clicked.connect(lambda: self._answer_consent(True))
        self.decline_button.clicked.connect(lambda: self._answer_consent(False))
        self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        self.connect_button.setEnabled(not busy)
        self.disconnect_button.setEnabled(not busy)
        self.generate_button.setEnabled(not busy and self._ready)
        self.cancel_button.setEnabled(busy and self._cancellable)
        self.settings_button.setEnabled(not busy)
        self.settings_dialog.set_busy(busy)
        self.cached_button.setEnabled(not busy and self._ready)
        self.cached_dialog.set_busy(busy)

    def start(self, operation: Callable[[], Awaitable[None]], *, cancellable: bool = True) -> None:
        """Only one operation may own the providers and workflow at a time."""
        if self._closing or (self.task is not None and not self.task.done()):
            return
        self._cancel = asyncio.Event()
        self._cancellable = cancellable
        self._set_busy(True)
        self.task = asyncio.create_task(self._run(operation))

    async def _run(self, operation: Callable[[], Awaitable[None]]) -> None:
        try:
            await operation()
        except asyncio.CancelledError:
            self.status.setText("Cancelled. The displayed saved brief is unchanged.")
        except AuthenticationRequiredError:
            self.connection.setText("Gmail: session expired or missing. Connect Gmail to continue.")
            self.status.setText("Sign in to Gmail, then retry. The saved brief is still available.")
        except ConfigurationError:
            self.status.setText(
                "Setup unavailable. Check the Gmail OAuth file, Groq model and OS credential "
                "store in Settings."
            )
        except ProviderError:
            self.status.setText("Provider unavailable. Check your connection and retry.")
        except Exception:
            # Validation and HTTP exceptions can contain secret or mail-derived values.
            self.status.setText("Operation failed. The displayed saved brief is unchanged.")
        finally:
            if self.cached_dialog.isVisible() and not self.cached_dialog.has_page:
                self.cached_dialog.status.setText(self.status.text())
            if self.settings_dialog.isVisible():
                self.settings_dialog.status.setText(self.status.text())
            self.review_panel.hide()
            self.consent_panel.hide()
            self.shortlist.clear()
            self.disclosure.clear()
            self._review = None
            self._consent = None
            self._set_busy(False)

    async def _open_cached(self) -> None:
        self.cached_dialog.configure(await self.backend.cached_accounts())
        self.cached_dialog.open()
        selected = self.cached_dialog.selection()
        if selected is not None:
            await self._load_cached_page(*selected, 0)
        else:
            self.status.setText("No cached Gmail accounts. Connect and sync to save metadata.")

    def _request_cached_page(self, account_id: int, day: date, offset: int) -> None:
        self.start(lambda: self._load_cached_page(account_id, day, offset))

    async def _load_cached_page(self, account_id: int, day: date, offset: int) -> None:
        self.cached_dialog.begin_load()
        self.cached_dialog.show_page(await self.backend.cached_messages(account_id, day, offset))
        self.status.setText("Showing saved metadata. No mailbox connection or body retrieval used.")

    async def _open_settings(self) -> None:
        try:
            preferences = await self.backend.get_preferences()
        except ConfigurationError:
            # A corrupt settings file must remain repairable from the editor.
            preferences = DesktopPreferences()
        self.settings_dialog.set_preferences(preferences)
        self.status.setText("Edit connection and AI settings.")
        self.settings_dialog.open()

    def _request_save_preferences(self, preferences: DesktopPreferences) -> None:
        self.start(lambda: self._save_preferences(preferences), cancellable=False)

    async def _save_preferences(self, preferences: DesktopPreferences) -> None:
        await self.backend.save_preferences(preferences)
        self.status.setText("Settings saved. Connect Gmail to verify the selected OAuth client.")
        self.connection.setText("Gmail: settings updated; connect to verify.")
        await self._refresh_ai_status()

    def _request_save_key(self, key: SecretStr) -> None:
        self.start(lambda: self._save_key(key), cancellable=False)

    async def _save_key(self, key: SecretStr) -> None:
        await self.backend.save_key(key)
        self.status.setText("Groq key saved in the OS credential store.")
        await self._refresh_ai_status()

    async def _remove_key(self) -> None:
        await self.backend.remove_key()
        self.status.setText("Groq key removed from the OS credential store.")
        await self._refresh_ai_status()

    async def _revoke_consent(self) -> None:
        count = await self.backend.revoke_consent()
        self.status.setText(f"AI consent revoked for {count} local Gmail consent records.")

    async def _refresh_ai_status(self) -> None:
        try:
            self.ai.setText(await self.backend.ai_status())
        except Exception:
            self.ai.setText("AI: configuration or secure key store unavailable.")

    async def initialize(self) -> None:
        saved = await self.backend.load_saved()
        self._ready = True
        if saved is not None:
            self.digest.show_digest(saved)
        self.status.setText("Ready. Sync to review today's messages.")
        await self._refresh_ai_status()
        try:
            email = await self.backend.connect(silent_only=True)
        except AuthenticationRequiredError:
            self.connection.setText("Gmail: session expired or missing. Connect Gmail to continue.")
        except ConfigurationError:
            self.connection.setText(
                "Gmail: setup required. Open Settings to choose an OAuth client."
            )
        except Exception:
            self.connection.setText("Gmail: offline or unavailable. Saved brief available locally.")
        else:
            self.connection.setText(f"Gmail: connected as {email}")

    async def _connect(self) -> None:
        self.status.setText("Connecting to Gmail. Complete sign-in in your browser.")
        email = await self.backend.connect(silent_only=False)
        self.connection.setText(f"Gmail: connected as {email}")
        self.status.setText("Connected. Sync to review today's messages.")

    async def _disconnect(self) -> None:
        await self.backend.disconnect()
        self.connection.setText("Gmail: disconnected")
        self.status.setText("Local credentials removed. Saved briefs remain on this device.")

    async def _generate(self) -> None:
        self.status.setText("Syncing today's Inbox…")
        result = await self.backend.generate(self, self, self._cancel, self._progress)
        if result.digest is not None:
            self.digest.show_digest(result.digest)
            self.status.setText(f"Brief saved ({result.digest.status.value}).")
            if result.digest.status is DigestStatus.EMPTY:
                if result.sync.message_count == 0 and result.sync.status is SyncStatus.COMPLETE:
                    self.status.setText("No messages in today's Inbox. Empty brief saved.")
                else:
                    self.status.setText(
                        "No analyzed messages in this selection. Empty brief saved."
                    )
        elif result.status is BriefStatus.CANCELLED:
            self.status.setText("Cancelled. The displayed saved brief is unchanged.")
        elif result.status is BriefStatus.CONSENT_DECLINED:
            self.status.setText("Transmission declined. No messages sent to AI in this run.")
        else:
            self.status.setText("Refresh failed. The displayed saved brief is unchanged. Retry.")
        if result.error_code == "AUTH_REQUIRED" or result.sync.error_code == "AUTH_REQUIRED":
            self.connection.setText("Gmail: session expired. Connect Gmail, then retry.")
        if result.error_code == "AI_KEY_MISSING":
            self.ai.setText("AI: key missing. Add a Groq API key in Settings.")
        elif result.error_code:
            self.status.setText(
                self.status.text() + " Some messages could not be processed; retry."
            )

    def _progress(self, progress: SyncProgress) -> None:
        self.status.setText(
            f"{progress.stage.value.capitalize()} · {progress.messages_fetched} messages · "
            f"{progress.ai_batches_completed} AI batches"
        )

    async def review(
        self, candidates: tuple[RankedMessage, ...], selected_ids: tuple[str, ...]
    ) -> tuple[str, ...] | None:
        if not candidates:
            return ()
        self._review = asyncio.get_running_loop().create_future()
        self.shortlist.clear()
        for ranked in candidates:
            message = ranked.message
            item = QListWidgetItem(f"{message.sender.address} — {message.subject}")
            item.setData(Qt.ItemDataRole.UserRole, message.provider_message_id)
            item.setToolTip(
                f"Rank score: {ranked.score}\n"
                + ", ".join(reason.value.replace("_", " ") for reason in ranked.reasons)
            )
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if message.provider_message_id in selected_ids
                else Qt.CheckState.Unchecked
            )
            self.shortlist.addItem(item)
        self.review_panel.show()
        self.shortlist.setCurrentRow(0)
        self.shortlist.setFocus()
        self.status.setText("Review the shortlist before any message bodies are retrieved.")
        self._selection_changed()
        try:
            return await self._review
        finally:
            self.review_panel.hide()

    def _accept_review(self) -> None:
        if self._review is None or self._review.done():
            return
        selected: list[str] = []
        for index in range(self.shortlist.count()):
            item = self.shortlist.item(index)
            if item is not None and item.checkState() is Qt.CheckState.Checked:
                selected.append(str(item.data(Qt.ItemDataRole.UserRole)))
        if len(selected) > MAX_SHORTLIST_SIZE:
            self.status.setText("Choose at most ten messages before continuing.")
            return
        self._review.set_result(tuple(selected))

    def _selection_changed(self) -> None:
        count = 0
        for index in range(self.shortlist.count()):
            item = self.shortlist.item(index)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                count += 1
        self.review_button.setText(f"&Continue with {count} selected messages")
        self.review_button.setEnabled(count <= MAX_SHORTLIST_SIZE)
        if count > MAX_SHORTLIST_SIZE:
            self.status.setText("Choose at most ten messages before continuing.")

    async def confirm(self, preview: TransmissionPreview) -> bool:
        self._consent = asyncio.get_running_loop().create_future()
        self.disclosure.setText("\n\n".join(disclosure_lines(preview)))
        self.consent_panel.show()
        self.decline_button.setFocus()
        self.status.setText("Review what will be sent to Groq. Enable Zero Data Retention first.")
        try:
            return await self._consent
        finally:
            self.consent_panel.hide()

    def _answer_consent(self, answer: bool) -> None:
        if self._consent is not None and not self._consent.done():
            self._consent.set_result(answer)

    def cancel(self) -> None:
        if not self._cancellable:
            return
        self._cancel.set()
        if self.task is not None and not self.task.done() and not self.task.cancelling():
            self.task.cancel()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._closing = True
        self.settings_dialog.reject()
        self.cached_dialog.reject()
        self.cancel()
        self.closing.emit()
        event.accept()

    async def shutdown(self) -> None:
        self.cancel()
        try:
            if self.task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await self.task
        finally:
            await self.backend.close()
