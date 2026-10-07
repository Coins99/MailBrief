"""Responsive desktop workflow with explicit metadata review and cloud consent."""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol, assert_never

from pydantic import SecretStr
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from mailbrief.domain.actions import (
    Action,
    ActionEdit,
    ActionFilter,
    ActionProposal,
    StepEdit,
    ThreadLink,
)
from mailbrief.domain.backup import BackupMetadata
from mailbrief.domain.briefs import (
    AutoSendStatus,
    BriefRunResult,
    BriefStatus,
    TransmissionPreview,
)
from mailbrief.domain.cached_mail import CachedAccount, CachedMailPage
from mailbrief.domain.digests import (
    DailyDigest,
    DigestStatus,
    SavedBriefSummary,
    SyncProgress,
    SyncResult,
    SyncStatus,
)
from mailbrief.domain.drafting import (
    DraftContextPart,
    DraftingOptions,
    DraftingOutcome,
    DraftingPreview,
    DraftingStatus,
)
from mailbrief.domain.drafts import (
    KIND_NAMES,
    Draft,
    DraftEdit,
    DraftKind,
    DraftSummary,
    DraftVersion,
    DraftVersionInfo,
    placeholders,
    still_to_fill,
)
from mailbrief.domain.messages import RankedMessage
from mailbrief.domain.preferences import OwnerPreferences, PreferencesEdit
from mailbrief.errors import ConfigurationError
from mailbrief.infra.files import write_text_atomically
from mailbrief.ports.errors import AuthenticationRequiredError, ProviderError
from mailbrief.services.actions import (
    COMPLETED_LIST_LIMIT,
    AcceptedInto,
    ActionConflictError,
    ActionNotFoundError,
    DecisionSnapshot,
    SuggestionNotFoundError,
)
from mailbrief.services.brief import (
    ConsentGate,
    ShortlistGate,
    disclosure_lines,
    needs_review_sentence,
)
from mailbrief.services.calendar import resolve_timezone
from mailbrief.services.consent import NO_CONSENT
from mailbrief.services.drafting import (
    DraftingContextError,
    DraftingGate,
    DraftingPlan,
)
from mailbrief.services.drafting import disclosure_lines as drafting_disclosure_lines
from mailbrief.services.drafts import DraftConflictError, DraftNotFoundError, SourceNotFoundError
from mailbrief.services.history import BriefDateError
from mailbrief.services.preferences import (
    PreferencesConflictError,
    PreferencesUnavailableError,
    owner_zone,
)
from mailbrief.services.proposals import ProposalNotFoundError
from mailbrief.services.ranking import (
    DECLINED_TEXT,
    MAX_SHORTLIST_SIZE,
    OUTSIDE_REPLY_TEXT,
    reason_text,
)
from mailbrief.ui.action_editor import ActionEditor
from mailbrief.ui.actions_view import (
    COMPLETE,
    DELETE,
    EDIT,
    PROPOSALS,
    REOPEN,
    SEEN,
    ActionsPanel,
)
from mailbrief.ui.auto_send_view import AutoSendDialog
from mailbrief.ui.brief_detail import ACCEPT, APPLY, DISMISS, is_gmail_link
from mailbrief.ui.cached_view import CachedMailDialog
from mailbrief.ui.data_view import DataDialog
from mailbrief.ui.diagnostics import (
    configuration_guidance,
    error_guidance,
    log_automatic_run,
    log_failure,
)
from mailbrief.ui.draft_editor import DraftEditor
from mailbrief.ui.drafts_view import DELETE as DELETE_DRAFT
from mailbrief.ui.drafts_view import NEW as NEW_DRAFT
from mailbrief.ui.drafts_view import OPEN as OPEN_DRAFT
from mailbrief.ui.drafts_view import DraftsPanel
from mailbrief.ui.hairline import HairlineDivider, HairlineFrame
from mailbrief.ui.history_view import NEEDS_CONNECTION, BriefHistoryPanel
from mailbrief.ui.labels import plain_label, wrap_label
from mailbrief.ui.preferences import DesktopPreferences
from mailbrief.ui.preferences_view import AUTO_DISCONNECTED, region_zones
from mailbrief.ui.proposals_view import ProposalsDialog
from mailbrief.ui.run_view import ROW_ROLE, ShortlistDelegate, shortlist_row
from mailbrief.ui.scheduler import RefreshScheduler
from mailbrief.ui.settings_view import SettingsDialog
from mailbrief.ui.theme import CAPTION_PX, SMALL_PX, TITLE_PX, ui_font
from mailbrief.ui.workspace import ThreePaneWorkspace


class DesktopBackend(Protocol):
    async def cached_accounts(self) -> tuple[CachedAccount, ...]: ...
    async def cached_messages(
        self, account_id: int, day: date, offset: int = 0
    ) -> CachedMailPage: ...
    async def get_preferences(self) -> DesktopPreferences: ...
    async def save_preferences(self, preferences: DesktopPreferences) -> None: ...
    async def get_owner_preferences(self) -> OwnerPreferences: ...
    async def save_owner_preferences(
        self, edit: PreferencesEdit, revision: int
    ) -> OwnerPreferences: ...
    async def reset_owner_preferences(self) -> OwnerPreferences: ...
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
        local_date: date | None = None,
    ) -> BriefRunResult: ...
    async def generate_automatic(
        self, cancel: asyncio.Event, progress: Callable[[SyncProgress], None]
    ) -> BriefRunResult: ...
    async def auto_send_status(self, account_email: str | None) -> AutoSendStatus | None: ...
    async def set_auto_send(
        self, limit: int, account_email: str | None
    ) -> AutoSendStatus | None: ...
    async def list_briefs(self) -> tuple[SavedBriefSummary, ...]: ...
    async def load_brief(self, account_email: str, local_date: date) -> DailyDigest | None: ...
    async def missed_days(self, account_email: str) -> tuple[date, ...]: ...
    async def close(self) -> None: ...
    async def list_actions(self, view: ActionFilter) -> tuple[Action, ...]: ...
    async def accept_suggestion(self, suggestion_id: int) -> Action: ...
    async def brief_links(self, digest: DailyDigest) -> dict[str, tuple[ThreadLink, ...]]: ...
    async def brief_proposals(
        self, digest: DailyDigest
    ) -> dict[str, tuple[ActionProposal, ...]]: ...
    async def apply_proposal(self, proposal_id: int, revision: int) -> Action: ...
    async def undo_apply_proposal(self, proposal_id: int, revision: int) -> Action: ...
    async def dismiss_proposal(self, proposal_id: int) -> None: ...
    async def restore_proposal(self, proposal_id: int) -> None: ...
    async def accept_into(
        self, suggestion_id: int, public_id: str, revision: int
    ) -> AcceptedInto: ...
    async def undo_accept_into(
        self,
        suggestion_id: int,
        public_id: str,
        revision: int,
        remove_source: bool,
        *,
        previous: DecisionSnapshot,
    ) -> Action: ...
    async def mark_thread_seen(self, public_id: str, revision: int) -> Action: ...
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
    async def count_actions(self, view: ActionFilter) -> int: ...
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
    async def drafting_ready(self) -> bool: ...
    async def available_drafting_parts(self, public_id: str) -> frozenset[DraftContextPart]: ...
    async def prepare_drafting(self, public_id: str, options: DraftingOptions) -> DraftingPlan: ...
    async def generate_draft(
        self, plan: DraftingPlan, gate: DraftingGate, cancel: asyncio.Event
    ) -> DraftingOutcome: ...


# Raised when the brief, an action or a draft changed since it was shown; the window reloads.
_STALE = (
    ActionConflictError,
    ActionNotFoundError,
    SuggestionNotFoundError,
    ProposalNotFoundError,
    DraftConflictError,
    DraftNotFoundError,
)
# Raised when the open draft changed elsewhere; the editor keeps the text.
_DRAFT_STALE = (DraftConflictError, DraftNotFoundError)
_PICK_ONE = "Select at least one message to analyze, or Cancel."
_NOT_REFRESHED = "The view could not be refreshed; restart MailBrief to see the latest."
_BUSY = "MailBrief is busy; try again in a moment."
_BRIEFS_UNAVAILABLE = "Saved briefs open once local storage loads. Choose Retry loading saved data."
_EDITOR_STALE = (
    "This action changed since you opened it. Your edits are still here — copy what you "
    "need, then Cancel and reopen the action."
)
_EDITOR_FAILED = "Couldn't save. Your edits are still here; try again."
_NO_SOURCE = "That email is no longer in local mail."
_DRAFT_CLOSE_FAILED = "Couldn't save. Your text is still here; try again."
_DRAFT_EXPORT_FAILED = "Couldn't export. Check the folder and try again."
_AI_CANCELLED = "Cancelled. Your text is unchanged."
_AI_DECLINED = "Nothing was sent: AI drafting needs your consent first."
_AI_FAILED = "Couldn't write with Groq. Your text is unchanged."
_REFRESH_DISCONNECTED = "Automatic refresh skipped: connect Gmail first."
_REFRESH_FAILED = "Refresh failed. The displayed saved brief is unchanged."
_AUTOMATIC = "Automatic refresh: "


def _review_hint(limit: int) -> str:
    noun = "message" if limit == 1 else "messages"
    return (
        "Review today's Inbox. Suggested messages are checked; add or remove any message, "
        f"up to {limit} {noun}. Only checked messages will have their bodies retrieved."
    )


def _too_many(limit: int) -> str:
    return f"Choose at most {limit} {'message' if limit == 1 else 'messages'} before continuing."


_EXCLUDED_TIP = (
    "This sender is excluded in Settings > Preferences, so this message is never analyzed. "
    "Change the rule there to include it."
)


def thread_check_text(sync: SyncResult) -> str:
    """The run's check of tracked threads in one sentence, never with its code; empty when
    no threads were tracked."""
    if sync.threads_stopped_code:
        return "Thread checks stopped early."
    tracked, checked = sync.threads_tracked, sync.threads_checked
    if not tracked:
        return ""
    noun = "thread" if tracked == 1 else "threads"
    if checked == tracked:
        return f"Checked {checked} tracked {noun}."
    return f"Checked {checked} of {tracked} tracked {noun}; {sync.threads_failed} failed."


def _count_messages(count: int, *, new: bool = False) -> str:
    noun = "new message" if new else "message"
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _ready_text(ready: int) -> str:
    """How many new messages an automatic run found ready to review (ADR 0017)."""
    if ready == 0:
        return "Nothing new to review."
    if ready == 1:
        return "1 new message is ready to review."
    return f"{ready} new messages are ready to review."


def _outline_button(
    text: str, name: str, *, px: int | None = None, accessible_name: str | None = None
) -> QPushButton:
    """An app-authored outline button; ``text`` may carry its ``&`` mnemonic. Without
    ``accessible_name``, Qt reads the button's text, so a label that changes stays read."""
    button = QPushButton(text)
    button.setObjectName(name)
    _outline(button)
    if accessible_name is not None:
        button.setAccessibleName(accessible_name)
    if px is not None:
        button.setFont(ui_font(px))
    return button


def _outline(button: QPushButton) -> None:
    button.setProperty("variant", "outline")
    button.setAutoDefault(False)


_RUN_WIDTH = 760


def _step_heading(layout: QVBoxLayout, step: str, title: str) -> None:
    """A run step's caption ("Step 1 of 2") above its title."""
    caption = plain_label(step, tone="muted", px=CAPTION_PX)
    caption.setObjectName("stepCaption")
    layout.addWidget(caption)
    heading = plain_label(title, px=TITLE_PX, medium=True)
    heading.setObjectName("stepTitle")
    layout.addWidget(heading)


def _page(widget: QWidget, name: str, accessible_name: str) -> QWidget:
    """A workspace page holding ``widget`` with 12 px margins."""
    page = QWidget()
    page.setObjectName(name)
    page.setAccessibleName(accessible_name)
    layout = QVBoxLayout(page)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.addWidget(widget)
    return page


class _ApprovedGate:
    """The editor's preview already asked; first use also needs the ticked consent box."""

    def __init__(self, agreed: bool) -> None:
        self._agreed = agreed

    async def request_drafting_consent(self, preview: DraftingPreview) -> bool:
        return self._agreed or not preview.first_use


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

    def _spawn(self, work: Coroutine[object, object, None]) -> None:
        try:
            task = asyncio.get_running_loop().create_task(work)
        except RuntimeError as exc:
            # Only possible once the event loop has stopped, when nothing can be saved.
            work.close()
            log_failure(exc)
            return
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

    def __init__(self, backend: DesktopBackend, *, database_path: Path | None = None) -> None:
        super().__init__()
        self.backend = backend
        self.pending_restore: tuple[Path, BackupMetadata] | None = None
        self._unsaved_drafts: set[str] = set()
        self.data_dialog = (
            None
            if database_path is None
            else DataDialog(
                self, database_path, self._run_data, self._request_restore, self._refresh_data
            )
        )
        self.task: asyncio.Task[None] | None = None
        self._cancel = asyncio.Event()
        self._review: asyncio.Future[tuple[str, ...] | None] | None = None
        # The review in progress: its message limit and the messages it can't select.
        self._review_limit = MAX_SHORTLIST_SIZE
        self._review_blocked: frozenset[str] = frozenset()
        self._consent: asyncio.Future[bool] | None = None
        self._ready = False
        self._closing = False
        self._shutdown_complete = False
        self._cancellable = True
        # The one change that Undo reverses: its button label and the operation that undoes it.
        self._undo: tuple[str, Callable[[], Awaitable[None]]] | None = None
        # The connected Gmail account, known once a connection succeeds.
        self._account_email: str | None = None
        # The brief shown: None for the latest, else a past brief's account and date.
        self._shown: tuple[str, date] | None = None
        # Whether the running operation is an automatic refresh (ADR 0017): its failures are
        # reported as such, and it never opens a panel or dialog.
        self._automatic_active = False
        # Carryover and overdue labels use the owner's local day.
        self.now: Callable[[], datetime] = lambda: datetime.now(UTC)
        self.zone = resolve_timezone(None)
        self.action_editor = ActionEditor(self)
        self.action_editor.save_requested.connect(self._request_save_action)
        self.draft_writes = DraftWrites()
        # The generation in progress: its plan, its cancel flag and its task.
        self._drafting_plan: DraftingPlan | None = None
        self._drafting_cancel: asyncio.Event | None = None
        self._drafting_task: asyncio.Task[DraftingOutcome] | None = None
        self.draft_editor = DraftEditor(self)
        self._connect_draft_editor(self.draft_editor)
        self.cached_dialog = CachedMailDialog(self)
        self.proposals_dialog = ProposalsDialog(self)
        self.proposals_dialog.apply_requested.connect(self._request_apply_from_dialog)
        self.proposals_dialog.dismiss_requested.connect(
            lambda proposal_id: self._start_from_link(lambda: self._dismiss_proposal(proposal_id))
        )
        self.history_panel = BriefHistoryPanel()
        self.history_panel.open_requested.connect(self._request_open_brief)
        self.history_panel.generate_requested.connect(self._request_brief_day)
        self.cached_dialog.page_requested.connect(self._request_cached_page)
        self.settings_dialog = SettingsDialog(self)
        self.auto_send_dialog = AutoSendDialog(self.settings_dialog)
        self.auto_send_dialog.save_requested.connect(self._request_set_auto_send)
        # When an automatic run is due, by the owner's schedule; it stops with the window.
        self.scheduler = RefreshScheduler(self, clock=lambda: self.now())
        self.scheduler.due.connect(self._refresh_due)
        self.settings_dialog.save_requested.connect(self._request_save_preferences)
        self.settings_dialog.key_requested.connect(self._request_save_key)
        self.settings_dialog.remove_key_requested.connect(
            lambda: self.start(self._remove_key, cancellable=False)
        )
        self.settings_dialog.revoke_requested.connect(
            lambda: self.start(self._revoke_consent, cancellable=False)
        )
        preferences_panel = self.settings_dialog.preferences_panel
        preferences_panel.save_requested.connect(self._request_save_owner_preferences)
        preferences_panel.reset_requested.connect(
            lambda: self.start(self._reset_owner_preferences, cancellable=False)
        )
        preferences_panel.auto_send_requested.connect(
            lambda: self.start(self._open_auto_send, cancellable=False)
        )
        self.setWindowTitle("MailBrief")
        self.resize(1100, 720)
        workspace = self.workspace = ThreePaneWorkspace()
        sidebar = workspace.sidebar
        # The header: when Gmail was last checked, then Sync and review and Cancel.
        self.generate_button = _outline_button("&Sync and review", "generateButton")
        self.generate_button.setToolTip("Refresh today's Inbox or retry an incomplete run.")
        self.cancel_button = _outline_button("&Cancel", "cancelButton")
        workspace.header.add_widget(self.generate_button)
        workspace.header.add_widget(self.cancel_button)
        # The sidebar's rows, under the names the rest of the window uses.
        self.settings_button = sidebar.settings
        self.cached_button = sidebar.saved_mail
        self.data_button = sidebar.data
        self.data_button.clicked.connect(lambda: self.start(self._open_data, cancellable=False))
        # Wrapping labels, read by their whole text.
        self.connection = wrap_label("Gmail: checking saved session…", tone="muted", px=CAPTION_PX)
        self.connection.setObjectName("connectionStatus")
        self.ai = wrap_label("AI: checking configuration…", tone="muted", px=CAPTION_PX)
        self.ai.setObjectName("aiStatus")
        self.connect_button = _outline_button(
            "&Connect Gmail", "connectButton", px=SMALL_PX, accessible_name="Connect Gmail"
        )
        self.disconnect_button = _outline_button(
            "&Disconnect", "disconnectButton", px=SMALL_PX, accessible_name="Disconnect Gmail"
        )
        for line in (self.connection, self.ai):
            line.setContentsMargins(10, 0, 0, 0)  # In line with the rows' icons.
        for widget in (self.connection, self.ai, self.connect_button, self.disconnect_button):
            sidebar.footer.addWidget(widget)
        # Today: the banner for a past brief and the retry row, above the brief.
        self.viewing = QWidget()
        self.viewing.setObjectName("viewingBanner")
        self.viewing.setAccessibleName("Brief shown")
        viewing = QHBoxLayout(self.viewing)
        viewing.setContentsMargins(12, 8, 12, 8)
        viewing.setSpacing(8)
        self.viewing_label = wrap_label("", tone="secondary", px=SMALL_PX)
        self.viewing_label.setObjectName("viewingLabel")
        self.latest_button = _outline_button("Back to &latest", "latestButton")
        viewing.addWidget(self.viewing_label, 1)
        viewing.addWidget(self.latest_button)
        workspace.today_top.addWidget(self.viewing)
        self.viewing.hide()
        self.retry_button = _outline_button("&Retry loading saved data", "retryButton")
        self.retry_button.clicked.connect(lambda: self.start(self.initialize))
        retry = QHBoxLayout()
        retry.setContentsMargins(12, 8, 12, 8)
        retry.addWidget(self.retry_button)
        retry.addStretch(1)
        workspace.today_top.addLayout(retry)
        # The run page: step 1 reviews the shortlist, step 2 approves sending.
        self.review_panel = QWidget()
        self.review_panel.setObjectName("reviewPanel")
        self.review_panel.setAccessibleName("Review the shortlist")
        review_layout = QVBoxLayout(self.review_panel)
        review_layout.setContentsMargins(0, 0, 0, 0)
        review_layout.setSpacing(8)
        _step_heading(review_layout, "Step 1 of 2", "Choose what MailBrief reads")
        self.review_hint = wrap_label(_review_hint(MAX_SHORTLIST_SIZE), tone="secondary")
        self.review_hint.setObjectName("reviewHint")
        review_layout.addWidget(self.review_hint)
        self.shortlist = QListWidget()
        self.shortlist.setObjectName("shortlist")
        self.shortlist.setAccessibleName("Messages selected for analysis")
        self.shortlist.setItemDelegate(ShortlistDelegate(self.shortlist))
        self.shortlist.itemChanged.connect(self._selection_changed)
        review_layout.addWidget(self.shortlist)
        self.review_button = QPushButton("Co&ntinue with selected messages")
        self.review_button.setObjectName("reviewButton")
        self.review_button.setProperty("variant", "primary")
        self.review_button.setAutoDefault(False)
        review_layout.addWidget(self.review_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.review_panel.hide()
        self.consent_panel = QWidget()
        self.consent_panel.setObjectName("consentPanel")
        self.consent_panel.setAccessibleName("Consent to send")
        consent_layout = QVBoxLayout(self.consent_panel)
        consent_layout.setContentsMargins(0, 0, 0, 0)
        consent_layout.setSpacing(8)
        _step_heading(consent_layout, "Step 2 of 2", "Approve sending to Groq")
        disclosure_card = HairlineFrame()
        disclosure_card.setObjectName("disclosureCard")
        disclosure_card.setAccessibleName("What will be sent")
        QVBoxLayout(disclosure_card).setContentsMargins(0, 0, 0, 0)
        self.disclosure = wrap_label("")
        self.disclosure.setObjectName("disclosure")
        card_layout = disclosure_card.layout()
        assert card_layout is not None
        card_layout.addWidget(self.disclosure)
        consent_layout.addWidget(disclosure_card)
        self.approve_button = QPushButton("&Approve transmission to Groq")
        self.approve_button.setObjectName("approveButton")
        self.decline_button = QPushButton("&Decline")
        self.decline_button.setObjectName("declineButton")
        answers = QHBoxLayout()
        answers.setSpacing(8)
        for answer in (self.approve_button, self.decline_button):
            _outline(answer)
            answers.addWidget(answer)
        answers.addStretch(1)
        consent_layout.addLayout(answers)
        self.consent_panel.hide()
        run_page = QScrollArea()
        run_page.setObjectName("runPage")
        run_page.setAccessibleName("Review and consent")
        run_page.setWidgetResizable(True)
        run_page.setFrameShape(QFrame.Shape.NoFrame)
        run_content = QWidget()
        run_content.setObjectName("runContent")
        # A centred column, at most _RUN_WIDTH wide.
        centred = QHBoxLayout(run_content)
        centred.setContentsMargins(16, 14, 16, 14)
        column = QWidget()
        column.setObjectName("runColumn")
        column.setMaximumWidth(_RUN_WIDTH)
        run_layout = QVBoxLayout(column)
        run_layout.setContentsMargins(0, 0, 0, 0)
        run_layout.addWidget(self.review_panel)
        run_layout.addWidget(self.consent_panel)
        run_layout.addStretch(1)
        centred.addStretch(1)
        centred.addWidget(column, 1000)
        centred.addStretch(1)
        run_page.setWidget(run_content)
        workspace.add_page("run", run_page)
        self.cached_dialog.zone = self.zone
        workspace.detail.suggestion_requested.connect(self._request_suggestion)
        workspace.detail.accept_into_requested.connect(self._request_accept_into)
        workspace.detail.proposal_requested.connect(self._request_proposal)
        workspace.detail.reply_requested.connect(self._request_reply)
        workspace.detail.source_requested.connect(self._open_source)
        self.actions_panel = ActionsPanel()
        self.actions_panel.action_requested.connect(self._request_action)
        self.actions_panel.draft_requested.connect(self._request_action_draft)
        self.actions_panel.tabs.currentChanged.connect(self._actions_tab_changed)
        workspace.add_page("actions", _page(self.actions_panel, "actionsPage", "Actions"))
        self.drafts_panel = DraftsPanel()
        self.drafts_panel.draft_requested.connect(self._request_draft)
        workspace.add_page("drafts", _page(self.drafts_panel, "draftsPage", "Drafts"))
        workspace.add_page("briefs", _page(self.history_panel, "briefsPage", "Briefs"))
        # Sidebar counts; each refresh replaces only its own, and a failure keeps them.
        self._counts: dict[str, int | None] = {"actions": None, "waiting": None, "drafts": None}
        # The status strip under the workspace.
        strip = QWidget()
        strip.setObjectName("statusStrip")
        strip.setAccessibleName("Status")
        strip.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        strip_layout = QHBoxLayout(strip)
        strip_layout.setContentsMargins(12, 6, 12, 6)
        strip_layout.setSpacing(8)
        self.status = wrap_label("Loading saved brief…", tone="secondary", px=SMALL_PX)
        self.status.setObjectName("statusText")
        strip_layout.addWidget(self.status, 1)
        self.undo_button = _outline_button("&Undo", "undoButton")
        self.undo_button.hide()
        self.undo_button.clicked.connect(lambda: self.start(self._undo_last, cancellable=False))
        strip_layout.addWidget(self.undo_button)
        central = QWidget()
        central.setObjectName("mainWindowContent")
        central.setAccessibleName("MailBrief")
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(workspace, 1)
        layout.addWidget(HairlineDivider(Qt.Orientation.Horizontal))
        layout.addWidget(strip)
        self.setCentralWidget(central)
        sidebar.page_requested.connect(self._request_page)
        self._set_account(None)
        sidebar.settings_requested.connect(lambda: self.start(self._open_settings))
        self.connect_button.clicked.connect(lambda: self.start(self._connect))
        self.disconnect_button.clicked.connect(
            lambda: self.start(self._disconnect, cancellable=False)
        )
        self.cached_button.clicked.connect(lambda: self.start(self._open_cached))
        self.latest_button.clicked.connect(
            lambda: self.start(self._back_to_latest, cancellable=False)
        )
        self.generate_button.clicked.connect(lambda: self.start(self._generate))
        self.cancel_button.clicked.connect(self.cancel)
        self.review_button.clicked.connect(self._accept_review)
        self.approve_button.clicked.connect(lambda: self._answer_consent(True))
        self.decline_button.clicked.connect(lambda: self._answer_consent(False))
        self._set_busy(False)

    def _set_account(self, email: str | None) -> None:
        """The connected Gmail account, or None; Connect shows only without one and
        Disconnect only with one."""
        self._account_email = email
        self.connect_button.setVisible(email is None)
        self.disconnect_button.setVisible(email is not None)

    def _set_busy(self, busy: bool) -> None:
        self.retry_button.setVisible(not self._ready)
        self.retry_button.setEnabled(not busy)
        self.connect_button.setEnabled(not busy)
        self.disconnect_button.setEnabled(not busy)
        self.generate_button.setEnabled(not busy and self._ready)
        self.cancel_button.setEnabled(busy and self._cancellable)
        self.cancel_button.setVisible(busy)
        self.settings_button.setEnabled(not busy)
        self.settings_dialog.set_busy(busy)
        self.cached_button.setEnabled(not busy and self._ready)
        self.data_button.setEnabled(not busy and self.data_dialog is not None)
        if self.data_dialog is not None:
            self.data_dialog.set_busy(busy)
        self.history_panel.set_busy(busy)
        self.latest_button.setEnabled(not busy)
        self.cached_dialog.set_busy(busy)
        self.proposals_dialog.set_busy(busy)
        self.undo_button.setEnabled(not busy)
        self.actions_panel.set_busy(busy)
        self.action_editor.set_busy(busy)
        self.drafts_panel.set_busy(busy)

    # Pages. The sidebar shows Today while the review or consent is up on the run page, and
    # Actions or Waiting by the actions page's tab.

    def _gate_pending(self) -> bool:
        """Whether the review or the consent question is waiting for the owner."""
        return any(
            future is not None and not future.done() for future in (self._review, self._consent)
        )

    def _show_page(self, key: str) -> None:
        if key == "today":
            self.workspace.show_page("run" if self._gate_pending() else "today")
        elif key in ("actions", "waiting"):
            self.workspace.show_page("actions")
            self.actions_panel.show_view(
                ActionFilter.WAITING if key == "waiting" else ActionFilter.OPEN
            )
        else:
            self.workspace.show_page(key)
        self.workspace.sidebar.set_current(self._sidebar_key())

    def _sidebar_key(self) -> str:
        """The sidebar row for the page shown."""
        page = self.workspace.current_page()
        if page == "run":
            return "today"
        if page == "actions":
            return "waiting" if self.actions_panel.view() is ActionFilter.WAITING else "actions"
        return page

    def _actions_tab_changed(self, _index: int) -> None:
        if self.workspace.current_page() == "actions":
            self.workspace.sidebar.set_current(self._sidebar_key())

    def _request_page(self, key: str) -> None:
        if key == "briefs":
            # Briefs loads first; the sidebar moves once its page shows.
            self.workspace.sidebar.set_current(self._sidebar_key())
            if not self._ready:
                self.status.setText(_BRIEFS_UNAVAILABLE)
            elif not self.start(self._open_history) and not self._closing:
                self.status.setText(_BUSY)
            return
        self._show_page(key)

    def _leave_run_page(self) -> None:
        """Back to Today once neither the review nor the consent is waiting."""
        if not self._gate_pending() and self.workspace.current_page() == "run":
            self._show_page("today")

    def _open_source(self, url: str) -> None:
        if is_gmail_link(url):
            QDesktopServices.openUrl(QUrl(url))

    def _stamp_checked(self) -> None:
        self.workspace.header.set_status(
            f"Checked Gmail at {self.now().astimezone(self.zone):%H:%M}"
        )

    def _set_counts(self, **counts: int | None) -> None:
        self._counts.update(counts)
        self.workspace.sidebar.set_counts(
            self._counts["actions"], self._counts["waiting"], self._counts["drafts"]
        )

    async def _open_data(self) -> None:
        if self._unsaved_drafts:
            self.status.setText("Resolve the failed draft save before managing data.")
            return
        if (
            self.action_editor.isVisible()
            or self.draft_editor.isVisible()
            or not self.draft_writes.idle
        ):
            self.status.setText(
                "Close the editors and let pending saves finish before managing data."
            )
            return
        if self.data_dialog is not None:
            accounts = await self.backend.cached_accounts() if self._ready else ()
            if not self._closing:
                self.data_dialog.configure(accounts, available=self._ready)
                self.data_dialog.open()

    def _run_data(self, operation: Callable[[], Awaitable[None]]) -> bool:
        if self.data_dialog is None:
            return False
        dialog = self.data_dialog
        return self.start(lambda: dialog.guarded(operation), cancellable=False)

    def _request_restore(self, path: Path, metadata: BackupMetadata) -> None:
        self.pending_restore = (path, metadata)
        self.close()

    async def _refresh_data(self) -> None:
        if self._closing:
            return
        self._offer_undo()
        self._shown = None
        self.viewing.hide()
        saved = await self.backend.load_saved()
        if saved is None:
            self.workspace.clear("")
        else:
            await self._show_digest(saved)
        await self._refresh_actions()
        await self._refresh_drafts()
        self.cached_dialog.reject()
        if self.workspace.current_page() == "briefs":
            await self._load_history()
        self.proposals_dialog.reject()
        if self.data_dialog is not None:
            self.data_dialog.configure(await self.backend.cached_accounts(), available=True)

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

    def _set_shown(self, shown: tuple[str, date] | None) -> None:
        """Show the latest brief (None), or say which past brief is shown."""
        self._shown = shown
        self.viewing.setVisible(shown is not None)
        if shown is not None:
            self.viewing_label.setText(f"Viewing the brief for {shown[1].isoformat()}.")

    async def _reload_brief(self) -> None:
        """Reload the brief shown: the latest, or the past brief being viewed."""
        shown = self._shown
        try:
            saved = await (
                self.backend.load_saved() if shown is None else self.backend.load_brief(*shown)
            )
        except Exception as exc:
            self._note_not_refreshed(exc)
            return
        if saved is None and shown is not None:
            self._set_shown(None)  # That brief is gone; show the latest instead.
            await self._reload_brief()
            return
        if saved is not None:
            await self._show_digest(saved)

    async def _show_digest(self, digest: DailyDigest) -> None:
        """Every brief is shown here, with the actions that continue its threads and the
        updates its emails propose; when either can't be read, the brief is shown without it."""
        links: dict[str, tuple[ThreadLink, ...]] | None
        try:
            links = await self.backend.brief_links(digest)
        except Exception as exc:
            log_failure(exc)
            links = None
        proposals: dict[str, tuple[ActionProposal, ...]] | None
        try:
            proposals = await self.backend.brief_proposals(digest)
        except Exception as exc:
            log_failure(exc)
            proposals = None
        self.workspace.show_digest(digest, links=links, proposals=proposals, owner_zone=self.zone)

    async def _refresh_actions(self) -> None:
        now = self.now()
        today = now.astimezone(self.zone).date()
        try:
            for view in ActionFilter:
                actions = await self.backend.list_actions(view)
                # Only the completed list is capped; say so when more exist.
                total = (
                    await self.backend.count_actions(view)
                    if view is ActionFilter.COMPLETED and len(actions) >= COMPLETED_LIST_LIMIT
                    else None
                )
                self.actions_panel.show_actions(
                    view, actions, today=today, zone=self.zone, now=now, total=total
                )
                if view is ActionFilter.OPEN:
                    self._set_counts(actions=len(actions))
                elif view is ActionFilter.WAITING:
                    self._set_counts(waiting=len(actions))
        except Exception as exc:
            self._note_not_refreshed(exc)

    async def _refresh_drafts(self) -> None:
        try:
            drafts = await self.backend.list_drafts()
            self.drafts_panel.show_drafts(drafts, self.zone)
            self._set_counts(drafts=len(drafts))
        except Exception as exc:
            self._note_not_refreshed(exc)

    def _refresh_proposals_dialog(self) -> None:
        """An open Proposals dialog follows the action's latest state."""
        dialog = self.proposals_dialog
        public_id = dialog.action_public_id
        if dialog.isVisible() and public_id is not None:
            dialog.show_proposals(self.actions_panel.action_with_id(public_id), self.zone)

    async def _refresh_views(self) -> None:
        await self._reload_brief()
        await self._refresh_actions()
        await self._refresh_drafts()
        self._refresh_proposals_dialog()

    async def _stale(self, exc: Exception) -> None:
        log_failure(exc)
        self.status.setText("That changed or is no longer available; the view was reloaded.")
        await self._refresh_views()

    def _start_from_link(self, operation: Callable[[], Awaitable[None]]) -> None:
        """Brief links stay clickable while busy, so a refused click says why."""
        if not self.start(operation, cancellable=False) and not self._closing:
            self.status.setText(_BUSY)

    def _request_suggestion(self, kind: str, suggestion_id: int) -> None:
        if kind == ACCEPT:
            self._start_from_link(lambda: self._accept_suggestion(suggestion_id))
        elif kind == DISMISS:
            self._start_from_link(lambda: self._dismiss_suggestion(suggestion_id))

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

    def _request_accept_into(self, suggestion_id: int, public_id: str, revision: int) -> None:
        self._start_from_link(lambda: self._accept_into(suggestion_id, public_id, revision))

    async def _accept_into(self, suggestion_id: int, public_id: str, revision: int) -> None:
        """Add the suggestion's email to an action that continues its thread, with Undo when
        that changed anything."""
        try:
            result = await self.backend.accept_into(suggestion_id, public_id, revision)
        except ActionConflictError as exc:
            log_failure(exc)
            self.status.setText(str(exc))  # Static; names no action or mail.
            await self._refresh_views()
            return
        except _STALE as exc:
            await self._stale(exc)
            return
        action = result.action
        if not result.changed:  # Already there: nothing to undo.
            self.status.setText(f"Already added to: {action.title}.")
            await self._refresh_views()
            return

        async def undo() -> None:
            await self.backend.undo_accept_into(
                suggestion_id,
                action.public_id,
                action.revision,
                result.source_added,
                previous=result.previous,
            )

        self._offer_undo("Undo add", undo)
        self.status.setText(f"Added to: {action.title}.")
        await self._refresh_views()

    def _request_proposal(self, kind: str, proposal_id: int, revision: int) -> None:
        if kind == APPLY:
            self._start_from_link(lambda: self._apply_proposal(proposal_id, revision))
        elif kind == DISMISS:
            self._start_from_link(lambda: self._dismiss_proposal(proposal_id))

    def _request_apply_from_dialog(self, proposal_id: int) -> None:
        proposal = self.proposals_dialog.proposal(proposal_id)
        if proposal is not None:
            revision = proposal.action_revision
            self._start_from_link(lambda: self._apply_proposal(proposal_id, revision))

    async def _apply_proposal(self, proposal_id: int, revision: int) -> None:
        """Apply a proposal the owner chose, at the action revision they saw, with Undo."""
        try:
            action = await self.backend.apply_proposal(proposal_id, revision)
        except ActionConflictError as exc:
            log_failure(exc)
            self.status.setText(str(exc))  # Static; names no action or mail.
            await self._refresh_views()
            return
        except _STALE as exc:
            await self._stale(exc)
            return
        applied_revision = action.revision

        async def undo() -> None:
            await self.backend.undo_apply_proposal(proposal_id, applied_revision)

        self._offer_undo("Undo apply", undo)
        self.status.setText(f"Applied to: {action.title}.")
        await self._refresh_views()

    async def _dismiss_proposal(self, proposal_id: int) -> None:
        try:
            await self.backend.dismiss_proposal(proposal_id)
        except ActionConflictError as exc:
            log_failure(exc)
            self.status.setText(str(exc))
            await self._refresh_views()
            return
        except _STALE as exc:
            await self._stale(exc)
            return
        self._offer_undo("Undo dismiss", lambda: self.backend.restore_proposal(proposal_id))
        self.status.setText("Proposal dismissed. It won't be proposed again.")
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
        elif kind == SEEN:
            self.start(lambda: self._mark_seen(action), cancellable=False)
        elif kind == PROPOSALS:
            self.proposals_dialog.show_proposals(action, self.zone)
            self.proposals_dialog.open()

    def _request_save_action(
        self, action: Action, edit: ActionEdit, steps: Sequence[StepEdit] | None
    ) -> None:
        started = self.start(lambda: self._save_action(action, edit, steps), cancellable=False)
        if not started and not self._closing:
            self.action_editor.keep_open(_BUSY)

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

    async def _mark_seen(self, action: Action) -> None:
        try:
            marked = await self.backend.mark_thread_seen(action.public_id, action.revision)
        except _STALE as exc:
            await self._stale(exc)
            return
        self._offer_undo()  # Like an edit, it has no undo; an older offer could be stale.
        self.status.setText(f"Marked seen: {marked.title}.")
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
        self._start_from_link(
            lambda: self._open_new_draft(
                lambda: self.backend.create_reply_draft(account_email, message_id)
            )
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

    # Writing with AI. Every step runs in draft_writes after pending autosaves; the
    # generation itself is a task of its own, so Cancel and quitting can stop it.

    async def _offer_drafting(self) -> None:
        editor = self.draft_editor
        draft = editor.draft
        if draft is None:
            return
        try:
            parts = await self.backend.available_drafting_parts(draft.public_id)
        except ConfigurationError as exc:
            log_failure(exc)
            editor.ai_failed(configuration_guidance(exc))
            return
        except Exception as exc:
            log_failure(exc)
            editor.ai_failed(_AI_FAILED)
            return
        editor.offer_ai(parts)

    async def _prepare_drafting(self, options: DraftingOptions) -> None:
        editor = self.draft_editor
        draft = editor.draft
        if draft is None or not editor.ai_active():
            return
        try:
            plan = await self.backend.prepare_drafting(draft.public_id, options)
        except DraftingContextError as exc:
            log_failure(exc)
            editor.ai_failed(str(exc))  # Static messages that name no mail content.
            return
        except _DRAFT_STALE as exc:
            log_failure(exc)
            editor.show_conflict()
            editor.ai_failed("")
            return
        except AuthenticationRequiredError as exc:
            log_failure(exc)
            editor.ai_failed("Connect Gmail to download the email, then try again.")
            return
        except ConfigurationError as exc:
            log_failure(exc)
            editor.ai_failed(configuration_guidance(exc))
            return
        except ProviderError as exc:
            log_failure(exc)
            editor.ai_failed("Gmail is offline or unavailable, so the email couldn't be read.")
            return
        except Exception as exc:
            log_failure(exc)
            editor.ai_failed(_AI_FAILED)
            return
        if not editor.ai_active():
            return  # Cancelled while preparing.
        self._drafting_plan = plan
        editor.show_ai_preview(plan.preview, drafting_disclosure_lines(plan.preview))

    async def _generate_draft(self, agreed: bool) -> None:
        editor = self.draft_editor
        plan, self._drafting_plan = self._drafting_plan, None
        if plan is None or not editor.ai_active():
            return
        cancel = asyncio.Event()
        task = asyncio.create_task(self.backend.generate_draft(plan, _ApprovedGate(agreed), cancel))
        self._drafting_cancel, self._drafting_task = cancel, task
        try:
            outcome = await task
        except asyncio.CancelledError:
            if not task.cancelled():
                raise  # This queue task itself was cancelled.
            await self._after_cancelled_generation(plan)
            return
        except Exception as exc:
            log_failure(exc)
            editor.ai_failed(_AI_FAILED)
            return
        finally:
            self._drafting_cancel = self._drafting_task = None
        if outcome.status is DraftingStatus.GENERATED:
            assert outcome.draft is not None and outcome.version_number is not None
            editor.ai_generated(
                outcome.draft,
                outcome.version_number,
                outcome.previous_version,
                outcome.missing_context,
            )
            await self._refresh_drafts()
        elif outcome.status is DraftingStatus.DECLINED:
            editor.ai_failed(_AI_DECLINED)
        elif outcome.status is DraftingStatus.CANCELLED:
            editor.ai_failed(_AI_CANCELLED)
        else:
            editor.ai_failed(error_guidance(outcome.error_code or "AI_PROVIDER_ERROR"))

    async def _after_cancelled_generation(self, plan: DraftingPlan) -> None:
        """Cancel can arrive just as Groq's text is saved; show whatever was stored."""
        editor = self.draft_editor
        try:
            stored = await self.backend.get_draft(plan.public_id)
        except Exception as exc:
            log_failure(exc)
            return
        current = editor.draft
        if (
            current is not None
            and current.public_id == stored.public_id
            and stored.revision != current.revision
            and not self._closing
        ):
            editor.restored(stored, 0)
            editor.set_status(
                "Groq finished before Cancel took effect; Versions has your earlier text."
            )

    def _cancel_drafting(self) -> None:
        """Drop a previewed plan and stop a generation; nothing is written after this."""
        self._drafting_plan = None
        if self._drafting_cancel is not None:
            self._drafting_cancel.set()
        if self._drafting_task is not None and not self._drafting_task.done():
            self._drafting_task.cancel()

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
        editor.ai_parts_requested.connect(lambda: writes.run(self._offer_drafting))
        editor.ai_prepare_requested.connect(
            lambda options: writes.run(lambda: self._prepare_drafting(options))
        )
        editor.ai_generate_requested.connect(
            lambda agreed: writes.run(lambda: self._generate_draft(agreed))
        )
        editor.ai_cancel_requested.connect(self._cancel_drafting)

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
            self._unsaved_drafts.add(public_id)
            log_failure(exc)
            editor.show_conflict()
            return False
        except Exception as exc:
            self._unsaved_drafts.add(public_id)
            log_failure(exc)
            editor.save_failed()
            return False
        editor.saved(saved, edit)
        self._unsaved_drafts.discard(public_id)
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
            self._unsaved_drafts.discard(draft.public_id)
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
        failed = True
        try:
            await operation()
            failed = False
        except asyncio.CancelledError:
            failed = False
            self.status.setText("Cancelled. The displayed saved brief is unchanged.")
        except AuthenticationRequiredError as exc:
            log_failure(exc)
            self._set_account(None)
            self.connection.setText("Gmail: session expired or missing. Connect Gmail to continue.")
            self.status.setText("Sign in to Gmail, then retry. The saved brief is still available.")
        except ConfigurationError as exc:
            log_failure(exc)
            self.status.setText(configuration_guidance(exc))
        except BriefDateError as exc:
            log_failure(exc)
            self.status.setText(str(exc))  # Static: the dates that can be briefed.
        except ProviderError as exc:
            log_failure(exc)
            self.status.setText("Provider unavailable. Check your connection and retry.")
        except Exception as exc:
            log_failure(exc)
            # Validation and HTTP exceptions can contain secret or mail-derived values.
            self.status.setText("Operation failed. The displayed saved brief is unchanged.")
        finally:
            if self._automatic_active:
                self._automatic_active = False
                if failed:
                    self.status.setText(_AUTOMATIC + self.status.text())
            if self.cached_dialog.isVisible() and not self.cached_dialog.has_page:
                self.cached_dialog.status.setText(self.status.text())
            if self.settings_dialog.isVisible():
                self.settings_dialog.status.setText(self.status.text())
            self.review_panel.hide()
            self.consent_panel.hide()
            self.shortlist.clear()
            self.disclosure.setText("")
            self._review = None
            self._consent = None
            self._leave_run_page()
            self._set_busy(False)

    async def _open_cached(self) -> None:
        self.cached_dialog.show_today()  # Today in the owner's zone, each time it opens.
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
        panel = self.settings_dialog.preferences_panel
        panel.set_zones(str(resolve_timezone(None)), await asyncio.to_thread(region_zones))
        await self._show_auto_send()
        self.status.setText("Edit connection and AI settings, and your preferences.")
        try:
            panel.set_preferences(await self.backend.get_owner_preferences())
        except PreferencesUnavailableError as exc:
            # Unreadable preferences stay repairable: the panel offers a reset.
            log_failure(exc)
            panel.set_unavailable()
            self.status.setText(str(exc))
        self.settings_dialog.open()

    def _apply_owner_preferences(self, preferences: OwnerPreferences) -> None:
        """The owner's zone for every local day and time shown, and the drafting defaults."""
        self.zone = owner_zone(preferences)
        self.cached_dialog.zone = self.zone
        self.draft_editor.ai_panel.set_defaults(preferences.draft_tone, preferences.draft_length)
        self.scheduler.configure(
            preferences.refresh_on_launch, preferences.refresh_interval_minutes
        )

    async def _load_owner_preferences(self) -> None:
        """Views only display local data, so unreadable preferences fall back to the system
        zone here; a brief or AI drafting still refuses to run until they are reset."""
        try:
            preferences = await self.backend.get_owner_preferences()
        except Exception as exc:
            log_failure(exc)
            self._apply_owner_preferences(OwnerPreferences.defaults())
            if isinstance(exc, PreferencesUnavailableError):
                self.status.setText(str(exc))
            return
        self._apply_owner_preferences(preferences)

    def _request_save_owner_preferences(self, edit: PreferencesEdit, revision: int) -> None:
        self.start(lambda: self._save_owner_preferences(edit, revision), cancellable=False)

    async def _save_owner_preferences(self, edit: PreferencesEdit, revision: int) -> None:
        try:
            saved = await self.backend.save_owner_preferences(edit, revision)
        except PreferencesConflictError as exc:
            log_failure(exc)
            self.status.setText(str(exc))  # Static; _run shows it in the dialog too.
            return
        await self._owner_preferences_changed(saved, "Preferences saved.")

    async def _reset_owner_preferences(self) -> None:
        saved = await self.backend.reset_owner_preferences()
        await self._owner_preferences_changed(saved, "Preferences reset to the defaults.")

    async def _owner_preferences_changed(self, saved: OwnerPreferences, message: str) -> None:
        self._apply_owner_preferences(saved)
        self.settings_dialog.preferences_panel.set_preferences(saved)
        self.status.setText(message)
        await self._refresh_views()

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
        self.status.setText(
            f"AI consent revoked for briefs and drafting ({count} consent records). "
            "MailBrief asks again before sending anything."
        )

    async def _refresh_ai_status(self) -> None:
        try:
            self.ai.setText(await self.backend.ai_status())
            self.draft_editor.set_ai_ready(await self.backend.drafting_ready())
        except Exception as exc:
            log_failure(exc)
            self.ai.setText("AI: configuration or secure key store unavailable.")
            self.draft_editor.set_ai_ready(False)

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
            self.workspace.clear("Saved brief unavailable until local storage can be opened.")
            return
        self._ready = True
        self.status.setText("Ready. Sync to review today's messages.")
        await self._load_owner_preferences()
        if saved is not None:
            await self._show_digest(saved)
        else:
            self.workspace.clear(
                "No saved brief yet. Connect Gmail, then sync and review your shortlist."
            )
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
            self._set_account(email)
            self.connection.setText(f"Gmail: connected as {email}")
            self._launch_soon()

    def _launch_soon(self) -> None:
        """MailBrief has started and Gmail is connected: let the scheduler run the refresh the
        owner asked for on launch, once this operation has finished, so it isn't turned away
        as busy."""
        asyncio.get_running_loop().call_soon(self.scheduler.launched)

    async def _connect(self) -> None:
        self.status.setText("Connecting to Gmail. Complete sign-in in your browser.")
        email = await self.backend.connect(silent_only=False)
        self._set_account(email)
        self.connection.setText(f"Gmail: connected as {email}")
        self.status.setText("Connected. Sync to review today's messages.")
        await self._show_auto_send()  # The permission shown is the connected account's.
        self._launch_soon()  # Only the first connection of a start counts as the launch.

    async def _disconnect(self) -> None:
        await self.backend.disconnect()
        self._set_account(None)
        self.connection.setText("Gmail: disconnected")
        self.status.setText("Local credentials removed. Saved briefs remain on this device.")
        await self._show_auto_send()

    # Saved briefs by day. Opening one needs no connection; briefing a past day is the
    # owner's explicit choice, one day at a time, for the connected account.

    async def _load_history(self) -> None:
        briefs = await self.backend.list_briefs()
        email = self._account_email
        missed = await self.backend.missed_days(email) if email is not None else ()
        today = self.now().astimezone(self.zone).date()
        self.history_panel.configure(briefs, missed, email, today)

    async def _open_history(self) -> None:
        await self._load_history()
        self.status.setText("Open a saved brief, or brief a missed day.")
        self._show_page("briefs")

    def _request_open_brief(self, account_email: str, local_date: date) -> None:
        self.start(lambda: self._show_brief(account_email, local_date), cancellable=False)

    async def _show_brief(self, account_email: str, local_date: date) -> None:
        digest = await self.backend.load_brief(account_email, local_date)
        if digest is None:
            self.status.setText("That brief is no longer saved.")
            return
        await self._view(digest)  # Back on Today.
        self.status.setText(f"Showing the brief for {local_date.isoformat()}.")

    async def _view(self, digest: DailyDigest) -> None:
        """Show a brief; the banner appears unless it is the latest."""
        latest = await self.backend.load_saved()
        identity = (digest.account_id, digest.local_date)
        is_latest = latest is not None and (latest.account_id, latest.local_date) == identity
        self._set_shown(None if is_latest else identity)
        await self._show_digest(digest)
        self._show_page("today")

    async def _back_to_latest(self) -> None:
        self._set_shown(None)
        await self._reload_brief()
        self._show_page("today")
        self.status.setText("Showing the latest brief.")

    def _request_brief_day(self, local_date: date) -> None:
        if self._account_email is None:
            self.history_panel.status.setText(NEEDS_CONNECTION)
            return
        self.start(lambda: self._generate(local_date))

    async def _generate(self, local_date: date | None = None) -> None:
        """Brief today (Sync and review), or a past day chosen on the Briefs page. However
        it ends, the next automatic run is an interval later (ADR 0017)."""
        try:
            await self._generate_brief(local_date)
        finally:
            self.scheduler.note_run(self.now())

    async def _generate_brief(self, local_date: date | None = None) -> None:
        self._offer_undo()  # A new brief replaces the suggestions that Undo would refer to.
        if local_date is None and self._shown is not None:
            self._set_shown(None)  # Sync and review returns to the latest brief.
            await self._reload_brief()
        day = "today's Inbox" if local_date is None else f"the Inbox for {local_date.isoformat()}"
        self.status.setText(f"Syncing {day}…")
        result = await self.backend.generate(
            self, self, self._cancel, self._progress, local_date=local_date
        )
        if result.sync.status in (SyncStatus.COMPLETE, SyncStatus.PARTIAL):
            self._stamp_checked()
        match result.status:
            case BriefStatus.SAVED:
                digest = result.digest
                assert digest is not None  # A saved result always carries its digest.
                if local_date is None:
                    await self._show_digest(digest)
                else:
                    await self._view(digest)
                self.status.setText(f"Brief saved ({digest.status.value}).")
                if digest.status is DigestStatus.EMPTY:
                    if result.sync.message_count == 0 and result.sync.status is SyncStatus.COMPLETE:
                        self.status.setText(f"No messages in {day}. Empty brief saved.")
                    else:
                        self.status.setText(
                            "No analyzed messages in this selection. Empty brief saved."
                        )
            case BriefStatus.CANCELLED:
                self.status.setText("Cancelled. The displayed saved brief is unchanged.")
            case BriefStatus.CONSENT_DECLINED:
                self.status.setText("Transmission declined. No messages sent to AI in this run.")
            case BriefStatus.SYNC_FAILED:
                self.status.setText(_REFRESH_FAILED)
            case BriefStatus.ANALYSIS_FAILED:
                self.status.setText(_REFRESH_FAILED)
            case BriefStatus.READY_FOR_REVIEW:
                # Only an automatic run ends this way; from here it is reported as a failure.
                self.status.setText(_REFRESH_FAILED)
            case _:
                assert_never(result.status)
        # A cancelled run's news is the cancellation; a partial count would invite misreading.
        threads = "" if result.status is BriefStatus.CANCELLED else thread_check_text(result.sync)
        if threads:
            self.status.setText(f"{self.status.text()} {threads}")
        if result.proposals_created > 0:
            count = result.proposals_created
            noun = "update" if count == 1 else "updates"
            self.status.setText(f"{self.status.text()} Proposed {count} {noun} to your actions.")
        if result.error_code == "AUTH_REQUIRED" or result.sync.error_code == "AUTH_REQUIRED":
            self.connection.setText("Gmail: session expired. Connect Gmail, then retry.")
        for code in dict.fromkeys((result.error_code, result.sync.error_code)):
            if code:
                guidance = error_guidance(code)
                self.status.setText(self.status.text() + " " + guidance)
                if code.startswith("AI_"):
                    self.ai.setText("AI: " + guidance)

    # Automatic refresh (ADR 0017): while MailBrief is open, on the owner's schedule. It has
    # no review, no consent question and no dialog; everything it says goes to the status line.

    def _dialog_open(self) -> bool:
        """Whether the owner is working in one of the window's dialogs or editors, or on
        the Briefs page."""
        if self.workspace.current_page() == "briefs":
            return True
        return (self.data_dialog is not None and self.data_dialog.isVisible()) or any(
            widget.isVisible()
            for widget in (
                self.settings_dialog,
                self.auto_send_dialog,
                self.cached_dialog,
                self.proposals_dialog,
                self.action_editor,
                self.draft_editor,
            )
        )

    def _refresh_due(self) -> None:
        """The scheduler says a run is due: start one unless something is in the way.

        Another operation, or a dialog the owner is using, means try again a minute later; a
        disconnected Gmail means skip this one, and say so. Nothing opens.
        """
        if self._closing or not self._ready:
            return
        busy = self._dialog_open() or (self.task is not None and not self.task.done())
        if busy:
            self.scheduler.retry_soon()
            return
        if self._account_email is None:
            self.status.setText(_REFRESH_DISCONNECTED)
            return
        if not self.start(self._automatic_refresh):
            self.scheduler.retry_soon()

    async def _automatic_refresh(self) -> None:
        """One automatic run. However it ends, the next is an interval later."""
        self._automatic_active = True
        self.status.setText(_AUTOMATIC + "checking Gmail…")
        try:
            result = await self.backend.generate_automatic(self._cancel, self._progress)
            await self._automatic_finished(result)
        finally:
            self.scheduler.note_run(self.now())

    async def _automatic_finished(self, result: BriefRunResult) -> None:
        """Say what the run did, in the status line, and show what it saved."""
        log_automatic_run(result)
        stamp = f"{self.now().astimezone(self.zone):%H:%M}"
        if result.sync.status in (SyncStatus.COMPLETE, SyncStatus.PARTIAL):
            self._stamp_checked()
        match result.status:
            case BriefStatus.READY_FOR_REVIEW:
                # Either it only checked, because nothing may be sent or nothing is new, or it
                # saved nothing because the brief would have lost a carried message. When that
                # was a failure, its guidance follows below.
                found = (
                    needs_review_sentence(result.unrefreshed, result.ready)
                    if result.needs_review
                    else _ready_text(result.ready)
                )
                text = f"Checked Gmail at {stamp}. {found}"
            case BriefStatus.SAVED:
                digest = result.digest
                assert digest is not None  # A saved result always carries its digest.
                analyzed = 0 if result.coverage is None else result.coverage.analyzed
                text = f"Automatic brief at {stamp}: " + (
                    f"analyzed {_count_messages(analyzed, new=True)}"
                    if analyzed
                    else "nothing new to analyze"
                )
                if result.deferred:
                    waiting = "waits" if result.deferred == 1 else "wait"
                    text += f"; {result.deferred} {waiting} for your review."
                else:
                    text += "."
                if result.proposals_created > 0:
                    count = result.proposals_created
                    noun = "update" if count == 1 else "updates"
                    text += f" Proposed {count} {noun} to your actions."
                self._offer_undo()  # A new brief replaces the suggestions an Undo would refer to.
                # Never pull the owner away from a past brief they're reading.
                if self._shown is None:
                    await self._show_digest(digest)
            case BriefStatus.CANCELLED:
                text = "Automatic refresh cancelled."
            case BriefStatus.SYNC_FAILED:
                text = ""  # The guidance for its code, below, says what failed.
            case BriefStatus.ANALYSIS_FAILED:
                text = ""  # As above.
            case BriefStatus.CONSENT_DECLINED:
                text = ""  # An automatic run never asks, so this never ends one.
            case _:
                assert_never(result.status)
        if result.status is not BriefStatus.CANCELLED:
            for code in dict.fromkeys((result.error_code, result.sync.error_code)):
                if code:
                    guidance = error_guidance(code)
                    text = f"{text} {_AUTOMATIC}{guidance}".strip()
                    if code.startswith("AI_"):
                        self.ai.setText("AI: " + guidance)
            if "AUTH_REQUIRED" in (result.error_code, result.sync.error_code):
                self._set_account(None)  # Nothing more runs until the owner signs in again.
                self.connection.setText("Gmail: session expired. Connect Gmail, then retry.")
        self.status.setText(text)
        if result.status is not BriefStatus.CANCELLED:
            await self._refresh_actions()  # Thread activity and proposals may have changed.

    async def _show_auto_send(self) -> None:
        """The automatic-analysis line in Settings, from the connected account's active
        consent; local data only."""
        panel = self.settings_dialog.preferences_panel
        if self._account_email is None:
            panel.set_auto_send(None, self.zone, connected=False)
            return
        try:
            status = await self.backend.auto_send_status(self._account_email)
        except Exception as exc:
            log_failure(exc)
            panel.set_auto_send(None, self.zone, unreadable=True)
            return
        panel.set_auto_send(status, self.zone)

    async def _open_auto_send(self) -> None:
        """Open the dialog on the connected account's permission; without an account or a
        consent there is none to change."""
        if self._account_email is None:
            self.settings_dialog.preferences_panel.set_auto_send(None, self.zone, connected=False)
            self.status.setText(AUTO_DISCONNECTED)
            return
        status = await self.backend.auto_send_status(self._account_email)
        self.settings_dialog.preferences_panel.set_auto_send(status, self.zone)
        if status is None:
            self.status.setText(NO_CONSENT)
            return
        self.auto_send_dialog.configure(status)
        self.auto_send_dialog.open()

    def _request_set_auto_send(self, limit: int) -> None:
        if not self.start(lambda: self._set_auto_send(limit), cancellable=False):
            self.status.setText(_BUSY)

    async def _set_auto_send(self, limit: int) -> None:
        status = await self.backend.set_auto_send(limit, self._account_email)
        self.settings_dialog.preferences_panel.set_auto_send(
            status, self.zone, connected=self._account_email is not None
        )
        if status is None or status.limit == 0:
            self.status.setText("Automatic analysis is off. Every run asks you first.")
        else:
            self.status.setText(
                f"Automatic runs may now send up to {_count_messages(status.limit)} without asking."
            )

    def _progress(self, progress: SyncProgress) -> None:
        self.status.setText(
            f"{progress.stage.value.capitalize()} · {progress.messages_fetched} messages · "
            f"{progress.ai_batches_completed} AI batches"
        )

    async def review(
        self,
        candidates: tuple[RankedMessage, ...],
        selected_ids: tuple[str, ...],
        *,
        blocked_ids: frozenset[str],
        outside_ids: frozenset[str],
        declined_ids: frozenset[str],
        limit: int,
    ) -> tuple[str, ...] | None:
        """Blocked messages are listed but can't be checked; at most ``limit`` can be.

        Replies in tracked threads that aren't in today's Inbox (``outside_ids``) are labelled
        as such and are otherwise like any other message. Messages the owner left out of an
        earlier review (``declined_ids``) start unchecked, with "you left this out earlier"."""
        if not candidates:
            return ()
        self._review = asyncio.get_running_loop().create_future()
        self._show_page("today")  # The run page, while the review waits.
        self._review_limit, self._review_blocked = limit, blocked_ids
        self.review_hint.setText(_review_hint(limit))
        self.shortlist.clear()
        for ranked in candidates:
            message = ranked.message
            label = f"{message.sender.address} — {message.subject}"
            # What the shortlist's delegate paints; the item's text stays what is read.
            row = shortlist_row(
                ranked,
                blocked=message.provider_message_id in blocked_ids,
                outside=message.provider_message_id in outside_ids,
                declined=message.provider_message_id in declined_ids,
            )
            if message.provider_message_id in blocked_ids:
                # Not user-checkable and never given a check box, so neither a click nor
                # Space can select it.
                item = QListWidgetItem(f"{label} — excluded in Settings")
                item.setData(Qt.ItemDataRole.UserRole, message.provider_message_id)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                item.setToolTip(_EXCLUDED_TIP)
                item.setData(ROW_ROLE, row)
                self.shortlist.addItem(item)
                continue
            if message.provider_message_id in outside_ids:
                label = f"{label} — {OUTSIDE_REPLY_TEXT}"
            if message.provider_message_id in declined_ids:
                label = f"{label} — {DECLINED_TEXT}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, message.provider_message_id)
            item.setToolTip(
                f"Rank score: {ranked.score}\n"
                + ", ".join(reason_text(reason) for reason in ranked.reasons)
            )
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if message.provider_message_id in selected_ids
                else Qt.CheckState.Unchecked
            )
            item.setData(ROW_ROLE, row)
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
            self._leave_run_page()

    def _checked_ids(self) -> list[str]:
        """The checked messages; a blocked message never counts, even if checked."""
        selected: list[str] = []
        for index in range(self.shortlist.count()):
            item = self.shortlist.item(index)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                key = str(item.data(Qt.ItemDataRole.UserRole))
                if key not in self._review_blocked:
                    selected.append(key)
        return selected

    def _accept_review(self) -> None:
        if self._review is None or self._review.done():
            return
        selected = self._checked_ids()
        if not selected:
            # Continuing with nothing would save an empty brief over today's saved one.
            self.status.setText(_PICK_ONE)
            return
        if len(selected) > self._review_limit:
            self.status.setText(_too_many(self._review_limit))
            return
        self._review.set_result(tuple(selected))

    def _selection_changed(self) -> None:
        count = len(self._checked_ids())
        noun = "message" if count == 1 else "messages"
        self.review_button.setText(f"Co&ntinue with {count} selected {noun}")
        self.review_button.setEnabled(1 <= count <= self._review_limit)
        if count == 0:
            self.status.setText(_PICK_ONE)
        elif count > self._review_limit:
            self.status.setText(_too_many(self._review_limit))

    async def confirm(self, preview: TransmissionPreview) -> bool:
        self._consent = asyncio.get_running_loop().create_future()
        self._show_page("today")  # The run page, while the consent waits.
        self.disclosure.setText("\n\n".join(disclosure_lines(preview)))
        self.consent_panel.show()
        self.decline_button.setFocus()
        self.status.setText("Review what will be sent to Groq. Enable Zero Data Retention first.")
        try:
            return await self._consent
        finally:
            self.consent_panel.hide()
            self._leave_run_page()

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
        self.scheduler.stop()
        self.auto_send_dialog.reject()
        self.settings_dialog.reject()
        self.cached_dialog.reject()
        self.proposals_dialog.reject()
        if self.data_dialog is not None:
            self.data_dialog.reject()
        self.action_editor.force_close()  # Even mid-save; a Save during shutdown is refused.
        self._cancel_drafting()
        final = self.draft_editor.final_edit() if self.draft_editor.isVisible() else None
        if final is not None:
            self._queue_autosave(final)  # shutdown() drains it before closing the database.
        self.draft_editor.force_close()
        self.cancel()
        self.closing.emit()

    async def shutdown(self) -> None:
        if self._shutdown_complete:
            return
        self.scheduler.stop()  # No timer outlives the window.
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
