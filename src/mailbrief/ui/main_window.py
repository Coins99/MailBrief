"""Responsive desktop workflow with explicit metadata review and cloud consent."""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
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

from mailbrief.domain.actions import Action, ActionEdit, ActionFilter, StepEdit
from mailbrief.domain.briefs import BriefRunResult, BriefStatus, TransmissionPreview
from mailbrief.domain.cached_mail import CachedAccount, CachedMailPage
from mailbrief.domain.digests import DailyDigest, DigestStatus, SyncProgress, SyncStatus
from mailbrief.domain.drafts import (
    KIND_NAMES,
    Draft,
    DraftEdit,
    DraftKind,
    DraftSummary,
    DraftVersion,
    DraftVersionInfo,
    placeholders,
)
from mailbrief.domain.messages import RankedMessage
from mailbrief.errors import ConfigurationError
from mailbrief.infra.files import write_text_atomically
from mailbrief.ports.errors import AuthenticationRequiredError, ProviderError
from mailbrief.services.actions import (
    ActionConflictError,
    ActionNotFoundError,
    SuggestionNotFoundError,
)
from mailbrief.services.brief import ConsentGate, ShortlistGate, disclosure_lines
from mailbrief.services.calendar import resolve_timezone
from mailbrief.services.drafts import DraftConflictError, DraftNotFoundError, SourceNotFoundError
from mailbrief.services.ranking import MAX_SHORTLIST_SIZE
from mailbrief.ui.action_editor import ActionEditor
from mailbrief.ui.actions_view import COMPLETE, DELETE, EDIT, REOPEN, ActionsPanel
from mailbrief.ui.cached_view import CachedMailDialog
from mailbrief.ui.diagnostics import configuration_guidance, error_guidance, log_failure
from mailbrief.ui.digest_view import ACCEPT, DISMISS, DigestView
from mailbrief.ui.draft_editor import DraftEditor, still_to_fill
from mailbrief.ui.drafts_view import DELETE as DELETE_DRAFT
from mailbrief.ui.drafts_view import NEW as NEW_DRAFT
from mailbrief.ui.drafts_view import OPEN as OPEN_DRAFT
from mailbrief.ui.drafts_view import DraftsPanel
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
    async def list_actions(self, view: ActionFilter) -> tuple[Action, ...]: ...
    async def accept_suggestion(self, suggestion_id: int) -> Action: ...
    async def dismiss_suggestion(self, suggestion_id: int) -> None: ...
    async def restore_suggestion(self, suggestion_id: int) -> None: ...
    async def unaccept_action(self, public_id: str, revision: int) -> None: ...
    async def save_action(
        self,
        public_id: str,
        revision: int,
        edit: ActionEdit,
        steps: Sequence[StepEdit] | None = None,
    ) -> Action: ...
    async def complete_action(self, public_id: str, revision: int) -> Action: ...
    async def reopen_action(self, public_id: str, revision: int) -> Action: ...
    async def delete_action(self, public_id: str, revision: int) -> None: ...
    async def restore_action(self, public_id: str) -> Action: ...
    async def list_drafts(self) -> tuple[DraftSummary, ...]: ...
    async def get_draft(self, public_id: str) -> Draft: ...
    async def create_draft(self, kind: DraftKind) -> Draft: ...
    async def create_reply_draft(self, account_email: str, provider_message_id: str) -> Draft: ...
    async def create_draft_for_action(self, public_id: str, kind: DraftKind) -> Draft: ...
    async def autosave_draft(self, public_id: str, revision: int, edit: DraftEdit) -> Draft: ...
    async def checkpoint_draft(self, public_id: str, revision: int) -> DraftVersionInfo | None: ...
    async def draft_versions(self, public_id: str) -> tuple[DraftVersionInfo, ...]: ...
    async def draft_version(self, public_id: str, number: int) -> DraftVersion: ...
    async def restore_draft_version(self, public_id: str, revision: int, number: int) -> Draft: ...
    async def save_draft_as_new(self, public_id: str, edit: DraftEdit) -> Draft: ...
    async def delete_draft(self, public_id: str, revision: int) -> None: ...
    async def restore_draft(self, public_id: str) -> Draft: ...


# Raised when the brief, an action or a draft changed since it was shown; the window reloads.
_STALE = (
    ActionConflictError,
    ActionNotFoundError,
    SuggestionNotFoundError,
    DraftConflictError,
    DraftNotFoundError,
)
# Raised when the open draft changed elsewhere; the editor keeps the text.
_DRAFT_STALE = (DraftConflictError, DraftNotFoundError)
_PICK_ONE = "Select at least one message to analyze, or Cancel."
_NOT_REFRESHED = "The view could not be refreshed; restart MailBrief to see the latest."
_EDITOR_WAIT = "MailBrief is busy; try again in a moment."
_EDITOR_STALE = (
    "This action changed since you opened it. Your edits are still here — copy what you "
    "need, then Cancel and reopen the action."
)
_EDITOR_FAILED = "Couldn't save. Your edits are still here; try again."
_NO_SOURCE = "That email is no longer in local mail."
_DRAFT_CLOSE_FAILED = "Couldn't save. Your text is still here; try again."
_DRAFT_EXPORT_FAILED = "Couldn't export. Check the folder and try again."


def plain_label(text: str) -> QLabel:
    """Mail-derived text must never become rich text or an automatic hyperlink."""
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class DraftWrites:
    """One serialized, coalescing queue for the open draft's writes.

    The owner's typing never makes the window busy, so draft writes do not go through
    MainWindow.start(). Autosaves coalesce: only the newest waiting snapshot is saved, and
    it is read when its turn comes. Every other operation waits for waiting snapshots
    first. drain() waits for everything queued, so shutdown can finish it before the
    database is closed. Operations handle their own failures; anything left is logged.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._snapshot: Callable[[], Awaitable[object]] | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def idle(self) -> bool:
        return all(task.done() for task in self._tasks)

    def autosave(self, save: Callable[[], Awaitable[object]]) -> None:
        """Queue a save; it replaces any save still waiting."""
        self._snapshot = save
        self._spawn(self._flush())

    def run(self, operation: Callable[[], Awaitable[object]]) -> None:
        """Queue an operation to run once every earlier write, and any waiting save, is done."""
        self._spawn(self._then(operation))

    async def drain(self) -> None:
        # Finished tasks leave the set only once the loop runs their callbacks, and gather()
        # returns without yielding when every task is done, so wait only for unfinished ones.
        while pending := [task for task in self._tasks if not task.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    def _spawn(self, work: Awaitable[None]) -> None:
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _save_waiting(self) -> None:
        save, self._snapshot = self._snapshot, None
        if save is not None:
            await save()

    async def _flush(self) -> None:
        async with self._lock:
            await self._guard(self._save_waiting)

    async def _then(self, operation: Callable[[], Awaitable[object]]) -> None:
        async with self._lock:
            await self._guard(self._save_waiting)
            await self._guard(operation)

    @staticmethod
    async def _guard(operation: Callable[[], Awaitable[object]]) -> None:
        try:
            await operation()
        except Exception as exc:
            log_failure(exc)


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
        self._shutdown_complete = False
        self._cancellable = True
        # The one change that Undo reverses: its button label and the operation that undoes it.
        self._undo: tuple[str, Callable[[], Awaitable[None]]] | None = None
        # Carryover and overdue labels use the owner's local day.
        self.now: Callable[[], datetime] = lambda: datetime.now(UTC)
        self.zone = resolve_timezone(None)
        self.action_editor = ActionEditor(self)
        self.action_editor.save_requested.connect(self._request_save_action)
        self.draft_writes = DraftWrites()
        self.draft_editor = DraftEditor(self)
        self._connect_draft_editor(self.draft_editor)
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
        self.retry_button = QPushButton("&Retry loading saved data")
        self.retry_button.clicked.connect(lambda: self.start(self.initialize))
        actions.addWidget(self.retry_button, 2, 0, 1, 3)
        self.cached_button = QPushButton("Browse saved &mail (offline)")
        actions.addWidget(self.cached_button, 1, 2)
        self.status = plain_label("Loading saved brief…")
        layout.addWidget(self.status)
        self.undo_button = QPushButton("&Undo")
        self.undo_button.hide()
        self.undo_button.clicked.connect(lambda: self.start(self._undo_last, cancellable=False))
        layout.addWidget(self.undo_button)
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
        self.review_button = QPushButton("Co&ntinue with selected messages")
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
        self.digest.suggestion_requested.connect(self._request_suggestion)
        self.digest.setMinimumHeight(180)
        layout.addWidget(self.digest, 1)
        self.actions_panel = ActionsPanel()
        self.actions_panel.setMinimumHeight(220)
        self.actions_panel.action_requested.connect(self._request_action)
        self.actions_panel.draft_requested.connect(self._request_action_draft)
        layout.addWidget(self.actions_panel, 1)
        self.drafts_panel = DraftsPanel()
        self.drafts_panel.setMinimumHeight(200)
        self.drafts_panel.draft_requested.connect(self._request_draft)
        layout.addWidget(self.drafts_panel, 1)
        self.digest.reply_requested.connect(self._request_reply)
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
        self.retry_button.setVisible(not self._ready)
        self.retry_button.setEnabled(not busy)
        self.connect_button.setEnabled(not busy)
        self.disconnect_button.setEnabled(not busy)
        self.generate_button.setEnabled(not busy and self._ready)
        self.cancel_button.setEnabled(busy and self._cancellable)
        self.settings_button.setEnabled(not busy)
        self.settings_dialog.set_busy(busy)
        self.cached_button.setEnabled(not busy and self._ready)
        self.cached_dialog.set_busy(busy)
        self.undo_button.setEnabled(not busy)
        self.actions_panel.set_busy(busy)
        self.action_editor.set_busy(busy)
        self.drafts_panel.set_busy(busy)

    def _offer_undo(
        self, label: str | None = None, operation: Callable[[], Awaitable[None]] | None = None
    ) -> None:
        """Offer to undo the latest change, or clear the offer when called without one."""
        self._undo = None if label is None or operation is None else (label, operation)
        self.undo_button.setText("&Undo" if self._undo is None else f"&{self._undo[0]}")
        self.undo_button.setVisible(self._undo is not None)

    def _note_not_refreshed(self, exc: Exception) -> None:
        """The change itself was saved; only the view is out of date."""
        log_failure(exc)
        if _NOT_REFRESHED not in self.status.text():
            self.status.setText(f"{self.status.text()} {_NOT_REFRESHED}".strip())

    async def _reload_brief(self) -> None:
        try:
            saved = await self.backend.load_saved()
        except Exception as exc:
            self._note_not_refreshed(exc)
            return
        if saved is not None:
            self.digest.show_digest(saved)

    async def _refresh_actions(self) -> None:
        now = self.now()
        today = now.astimezone(self.zone).date()
        try:
            for view in ActionFilter:
                self.actions_panel.show_actions(
                    view,
                    await self.backend.list_actions(view),
                    today=today,
                    zone=self.zone,
                    now=now,
                )
        except Exception as exc:
            self._note_not_refreshed(exc)

    async def _refresh_drafts(self) -> None:
        try:
            self.drafts_panel.show_drafts(await self.backend.list_drafts(), self.zone)
        except Exception as exc:
            self._note_not_refreshed(exc)

    async def _refresh_views(self) -> None:
        await self._reload_brief()
        await self._refresh_actions()
        await self._refresh_drafts()

    async def _stale(self, exc: Exception) -> None:
        log_failure(exc)
        self.status.setText("That changed or is no longer available; the view was reloaded.")
        await self._refresh_views()

    def _request_suggestion(self, kind: str, suggestion_id: int) -> None:
        if kind == ACCEPT:
            self.start(lambda: self._accept_suggestion(suggestion_id), cancellable=False)
        elif kind == DISMISS:
            self.start(lambda: self._dismiss_suggestion(suggestion_id), cancellable=False)

    async def _accept_suggestion(self, suggestion_id: int) -> None:
        try:
            action = await self.backend.accept_suggestion(suggestion_id)
        except _STALE as exc:
            await self._stale(exc)
            return
        public_id, revision = action.public_id, action.revision
        self._offer_undo("Undo accept", lambda: self.backend.unaccept_action(public_id, revision))
        self.status.setText(f"Accepted: {action.title}. It stays open until you complete it.")
        await self._refresh_views()

    async def _dismiss_suggestion(self, suggestion_id: int) -> None:
        try:
            await self.backend.dismiss_suggestion(suggestion_id)
        except _STALE as exc:
            await self._stale(exc)
            return
        self._offer_undo("Undo dismiss", lambda: self.backend.restore_suggestion(suggestion_id))
        self.status.setText("Suggestion dismissed. It won't be suggested again for this email.")
        await self._refresh_views()

    async def _undo_last(self) -> None:
        if self._undo is None:
            return
        operation = self._undo[1]
        self._offer_undo()
        try:
            await operation()
        except _STALE as exc:
            log_failure(exc)
            self.status.setText("Can't undo: it has changed since then.")
            await self._refresh_views()
            return
        self.status.setText("Undone.")
        await self._refresh_views()

    def _request_action(self, kind: str, action: Action) -> None:
        if kind == EDIT:
            self.action_editor.edit(action, self.now().astimezone(self.zone).date(), self.zone)
            self.action_editor.open()
        elif kind == COMPLETE:
            self.start(lambda: self._complete_action(action), cancellable=False)
        elif kind == REOPEN:
            self.start(lambda: self._reopen_action(action), cancellable=False)
        elif kind == DELETE:
            self.start(lambda: self._delete_action(action), cancellable=False)

    def _request_save_action(
        self, action: Action, edit: ActionEdit, steps: Sequence[StepEdit] | None
    ) -> None:
        started = self.start(lambda: self._save_action(action, edit, steps), cancellable=False)
        if not started and not self._closing:
            self.action_editor.keep_open(_EDITOR_WAIT)

    async def _complete_action(self, action: Action) -> None:
        try:
            done = await self.backend.complete_action(action.public_id, action.revision)
        except _STALE as exc:
            await self._stale(exc)
            return

        async def undo() -> None:
            await self.backend.reopen_action(done.public_id, done.revision)

        self._offer_undo("Undo complete", undo)
        self.status.setText(f"Completed: {done.title}.")
        await self._refresh_views()

    async def _reopen_action(self, action: Action) -> None:
        try:
            opened = await self.backend.reopen_action(action.public_id, action.revision)
        except _STALE as exc:
            await self._stale(exc)
            return

        async def undo() -> None:
            await self.backend.complete_action(opened.public_id, opened.revision)

        self._offer_undo("Undo reopen", undo)
        self.status.setText(f"Reopened: {opened.title}.")
        await self._refresh_views()

    async def _delete_action(self, action: Action) -> None:
        try:
            await self.backend.delete_action(action.public_id, action.revision)
        except _STALE as exc:
            await self._stale(exc)
            return
        public_id = action.public_id

        async def undo() -> None:
            await self.backend.restore_action(public_id)

        self._offer_undo("Undo delete", undo)
        self.status.setText(f"Deleted: {action.title}.")
        await self._refresh_views()

    async def _save_action(
        self, action: Action, edit: ActionEdit, steps: Sequence[StepEdit] | None
    ) -> None:
        """The editor closes only once the save succeeds; otherwise it keeps the edits."""
        try:
            saved = await self.backend.save_action(action.public_id, action.revision, edit, steps)
        except _STALE as exc:
            if not self._closing:
                self.action_editor.keep_open(_EDITOR_STALE)
            await self._stale(exc)
            return
        except Exception:
            if not self._closing:
                self.action_editor.keep_open(_EDITOR_FAILED)
            raise  # _run logs it and reports the failure.
        self.action_editor.finish_saved()
        self._offer_undo()  # An edit has no undo; an older offer would refer to a past revision.
        self.status.setText(f"Saved: {saved.title}.")
        await self._refresh_views()

    # Drafts. Creating, opening and deleting use start(); the open draft's writes go
    # through draft_writes, so typing never makes the window busy.

    def _request_reply(self, account_email: str, message_id: str) -> None:
        self.start(
            lambda: self._open_new_draft(
                lambda: self.backend.create_reply_draft(account_email, message_id)
            ),
            cancellable=False,
        )

    def _request_action_draft(self, kind: DraftKind, action: Action) -> None:
        public_id = action.public_id
        self.start(
            lambda: self._open_new_draft(
                lambda: self.backend.create_draft_for_action(public_id, kind)
            ),
            cancellable=False,
        )

    def _request_draft(self, kind: str, value: object) -> None:
        if kind == NEW_DRAFT and isinstance(value, DraftKind):
            new_kind = value
            self.start(
                lambda: self._open_new_draft(lambda: self.backend.create_draft(new_kind)),
                cancellable=False,
            )
        elif isinstance(value, DraftSummary):
            summary = value
            if kind == OPEN_DRAFT:
                self.start(lambda: self._open_draft(summary), cancellable=False)
            elif kind == DELETE_DRAFT:
                self.start(lambda: self._delete_draft(summary), cancellable=False)

    async def _open_new_draft(self, create: Callable[[], Awaitable[Draft]]) -> None:
        try:
            draft = await create()
        except SourceNotFoundError as exc:
            log_failure(exc)
            self.status.setText(_NO_SOURCE)
            return
        except _STALE as exc:
            await self._stale(exc)
            return
        self._edit_draft(draft)
        self.status.setText(f"{KIND_NAMES[draft.kind]} draft started. It saves as you type.")
        await self._refresh_drafts()

    async def _open_draft(self, summary: DraftSummary) -> None:
        try:
            draft = await self.backend.get_draft(summary.public_id)
        except _STALE as exc:
            await self._stale(exc)
            return
        self._edit_draft(draft)

    def _edit_draft(self, draft: Draft) -> None:
        self.draft_editor.load(draft, self.zone)
        self.draft_editor.open()

    async def _delete_draft(self, summary: DraftSummary) -> None:
        try:
            await self.backend.delete_draft(summary.public_id, summary.revision)
        except _STALE as exc:
            await self._stale(exc)
            return
        public_id = summary.public_id

        async def undo() -> None:
            await self.backend.restore_draft(public_id)

        self._offer_undo("Undo delete", undo)
        self.status.setText(f"Deleted: {summary.display_title}.")
        await self._refresh_views()

    def _connect_draft_editor(self, editor: DraftEditor) -> None:
        writes = self.draft_writes
        editor.autosave_requested.connect(self._queue_autosave)
        editor.checkpoint_requested.connect(lambda: writes.run(self._checkpoint_draft))
        editor.versions_requested.connect(lambda: writes.run(self._list_draft_versions))
        editor.version_requested.connect(
            lambda number: writes.run(lambda: self._show_draft_version(number))
        )
        editor.restore_requested.connect(
            lambda number: writes.run(lambda: self._restore_draft_version(number))
        )
        editor.save_as_new_requested.connect(
            lambda edit: writes.run(lambda: self._save_draft_as_new(edit))
        )
        editor.export_requested.connect(
            lambda path, text: writes.run(lambda: self._export_draft(Path(path), text))
        )
        editor.close_requested.connect(lambda edit: writes.run(lambda: self._close_draft(edit)))

    def _queue_autosave(self, edit: DraftEdit) -> None:
        draft = self.draft_editor.draft
        if draft is not None:
            public_id = draft.public_id
            self.draft_writes.autosave(lambda: self._autosave_draft(public_id, edit))

    async def _autosave_draft(self, public_id: str, edit: DraftEdit) -> bool:
        """Save ``edit`` at the editor's latest revision; False when it was not saved."""
        editor = self.draft_editor
        draft = editor.draft
        if draft is None or draft.public_id != public_id or editor.in_conflict:
            return False
        try:
            saved = await self.backend.autosave_draft(public_id, draft.revision, edit)
        except _DRAFT_STALE as exc:
            log_failure(exc)
            editor.show_conflict()
            return False
        except Exception as exc:
            log_failure(exc)
            editor.save_failed()
            return False
        editor.saved(saved, edit)
        return True

    def _writable_draft(self) -> Draft | None:
        editor = self.draft_editor
        return None if editor.in_conflict else editor.draft

    async def _checkpoint_draft(self) -> None:
        editor = self.draft_editor
        draft = self._writable_draft()
        if draft is None:
            return
        try:
            info = await self.backend.checkpoint_draft(draft.public_id, draft.revision)
        except _DRAFT_STALE as exc:
            log_failure(exc)
            editor.show_conflict()
            return
        except Exception as exc:
            log_failure(exc)
            editor.set_status("Couldn't save a version; try again.")
            return
        editor.checkpointed(info)

    async def _list_draft_versions(self) -> None:
        editor = self.draft_editor
        draft = editor.draft
        if draft is None:
            return
        try:
            versions = await self.backend.draft_versions(draft.public_id)
        except Exception as exc:
            log_failure(exc)
            editor.set_status("Couldn't load the saved versions; try again.")
            return
        editor.show_versions(versions)

    async def _show_draft_version(self, number: int) -> None:
        editor = self.draft_editor
        draft = editor.draft
        if draft is None:
            return
        try:
            version = await self.backend.draft_version(draft.public_id, number)
        except Exception as exc:
            log_failure(exc)
            editor.set_status("Couldn't load that version; try again.")
            return
        editor.show_version(version)

    async def _restore_draft_version(self, number: int) -> None:
        editor = self.draft_editor
        draft = self._writable_draft()
        if draft is None:
            editor.restore_failed("Save your text as a new draft first.")
            return
        try:
            restored = await self.backend.restore_draft_version(
                draft.public_id, draft.revision, number
            )
        except _DRAFT_STALE as exc:
            log_failure(exc)
            editor.show_conflict()
            editor.restore_failed("")
            return
        except Exception as exc:
            log_failure(exc)
            editor.restore_failed("Couldn't restore that version; try again.")
            return
        editor.restored(restored, number)

    async def _save_draft_as_new(self, edit: DraftEdit) -> None:
        editor = self.draft_editor
        draft = editor.draft
        if draft is None:
            return
        try:
            copy = await self.backend.save_draft_as_new(draft.public_id, edit)
        except Exception as exc:
            log_failure(exc)
            editor.set_status("Couldn't save a new draft; try again.")
            return
        editor.continue_as(copy)
        await self._refresh_drafts()

    async def _export_draft(self, path: Path, text: str) -> None:
        """Write the owner's chosen file in a worker thread, replacing it atomically."""
        editor = self.draft_editor
        try:
            await asyncio.to_thread(write_text_atomically, path, text, overwrite=True)
        except Exception as exc:
            log_failure(exc)
            editor.set_status(_DRAFT_EXPORT_FAILED)
            return
        count = len(placeholders(text))
        editor.set_status(f"Exported {path.name}." + (f" {still_to_fill(count)}" if count else ""))

    async def _close_draft(self, edit: DraftEdit | None) -> None:
        """Save any unsaved text and a version, then let the editor close."""
        editor = self.draft_editor
        draft = editor.draft
        if draft is None:
            editor.finish_closed()
            return
        if edit is not None and not await self._autosave_draft(draft.public_id, edit):
            editor.cancel_close(None if editor.in_conflict else _DRAFT_CLOSE_FAILED)
            return
        draft = editor.draft
        assert draft is not None
        try:
            await self.backend.checkpoint_draft(draft.public_id, draft.revision)
        except _DRAFT_STALE as exc:
            log_failure(exc)
            editor.show_conflict()
            editor.cancel_close()
            return
        except Exception as exc:
            log_failure(exc)
            editor.cancel_close(_DRAFT_CLOSE_FAILED)
            return
        editor.finish_closed()
        if not self._closing:
            self.status.setText(f"Saved: {draft.display_title}.")
            await self._refresh_drafts()

    def start(self, operation: Callable[[], Awaitable[None]], *, cancellable: bool = True) -> bool:
        """Only one operation may own the providers and workflow at a time.

        Returns whether this operation started; it does not while closing or busy.
        """
        if self._closing or (self.task is not None and not self.task.done()):
            return False
        self._cancel = asyncio.Event()
        self._cancellable = cancellable
        self._set_busy(True)
        self.task = asyncio.create_task(self._run(operation))
        return True

    async def _run(self, operation: Callable[[], Awaitable[None]]) -> None:
        try:
            await operation()
        except asyncio.CancelledError:
            self.status.setText("Cancelled. The displayed saved brief is unchanged.")
        except AuthenticationRequiredError as exc:
            log_failure(exc)
            self.connection.setText("Gmail: session expired or missing. Connect Gmail to continue.")
            self.status.setText("Sign in to Gmail, then retry. The saved brief is still available.")
        except ConfigurationError as exc:
            log_failure(exc)
            self.status.setText(configuration_guidance(exc))
        except ProviderError as exc:
            log_failure(exc)
            self.status.setText("Provider unavailable. Check your connection and retry.")
        except Exception as exc:
            log_failure(exc)
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
        except ConfigurationError as exc:
            log_failure(exc)
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
        except Exception as exc:
            log_failure(exc)
            self.ai.setText("AI: configuration or secure key store unavailable.")

    async def initialize(self) -> None:
        try:
            saved = await self.backend.load_saved()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_failure(exc)
            self.connection.setText("Gmail: waiting for local storage.")
            self.ai.setText("AI: waiting for local storage.")
            self.status.setText(
                "Saved data could not be opened. The database may be newer than this app or "
                "unreadable. Use the latest MailBrief, check disk access, then Retry loading "
                "saved data. Do not delete your database."
            )
            self.digest.setPlainText("Saved brief unavailable until local storage can be opened.")
            return
        self._ready = True
        if saved is not None:
            self.digest.show_digest(saved)
        else:
            self.digest.setPlainText(
                "No saved brief yet. Connect Gmail, then sync and review your shortlist."
            )
        self.status.setText("Ready. Sync to review today's messages.")
        await self._refresh_actions()
        await self._refresh_drafts()
        await self._refresh_ai_status()
        try:
            email = await self.backend.connect(silent_only=True)
        except AuthenticationRequiredError as exc:
            log_failure(exc)
            self.connection.setText("Gmail: session expired or missing. Connect Gmail to continue.")
        except ConfigurationError as exc:
            log_failure(exc)
            self.connection.setText("Gmail: " + configuration_guidance(exc))
        except Exception as exc:
            log_failure(exc)
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
        self._offer_undo()  # A new brief replaces the suggestions that Undo would refer to.
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
            self.status.setText("Refresh failed. The displayed saved brief is unchanged.")
        if result.error_code == "AUTH_REQUIRED" or result.sync.error_code == "AUTH_REQUIRED":
            self.connection.setText("Gmail: session expired. Connect Gmail, then retry.")
        for code in dict.fromkeys((result.error_code, result.sync.error_code)):
            if code:
                guidance = error_guidance(code)
                self.status.setText(self.status.text() + " " + guidance)
                if code.startswith("AI_"):
                    self.ai.setText("AI: " + guidance)

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
        if not selected:
            # Continuing with nothing would save an empty brief over today's saved one.
            self.status.setText(_PICK_ONE)
            return
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
        noun = "message" if count == 1 else "messages"
        self.review_button.setText(f"Co&ntinue with {count} selected {noun}")
        self.review_button.setEnabled(1 <= count <= MAX_SHORTLIST_SIZE)
        if count == 0:
            self.status.setText(_PICK_ONE)
        elif count > MAX_SHORTLIST_SIZE:
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
        if self._shutdown_complete:
            event.accept()
            return
        event.ignore()  # Also defers QApplication.quit(), including Cmd+Q and Dock Quit.
        if self._closing:
            return
        self._closing = True
        self.settings_dialog.reject()
        self.cached_dialog.reject()
        self.action_editor.force_close()  # Even mid-save; a Save during shutdown is refused.
        final = self.draft_editor.final_edit() if self.draft_editor.isVisible() else None
        if final is not None:
            self._queue_autosave(final)  # shutdown() drains it before closing the database.
        self.draft_editor.force_close()
        self.cancel()
        self.closing.emit()

    async def shutdown(self) -> None:
        if self._shutdown_complete:
            return
        self.cancel()
        try:
            if self.task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await self.task
            await self.draft_writes.drain()
        finally:
            try:
                await self.backend.close()
            finally:
                self._shutdown_complete = True
                self.close()
