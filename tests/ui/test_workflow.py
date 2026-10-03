"""Desktop transitions, cancellation, local restoration and safe rendering."""

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from pytestqt.qtbot import QtBot

from mailbrief.domain.actions import (
    Action,
    ActionEdit,
    ActionFilter,
    ActionProposal,
    StepEdit,
    SuggestionState,
    ThreadLink,
)
from mailbrief.domain.analysis import DeadlinePrecision
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
from mailbrief.domain.messages import RankedMessage
from mailbrief.domain.preferences import OwnerPreferences, PreferencesEdit
from mailbrief.errors import ConfigurationError
from mailbrief.ports.errors import AuthenticationRequiredError, ProviderError
from mailbrief.services.actions import AcceptedInto, DecisionSnapshot
from mailbrief.services.brief import ConsentGate, ShortlistGate
from mailbrief.services.consent import NO_CONSENT
from mailbrief.services.preferences import PreferencesConflictError
from mailbrief.ui.main_window import MainWindow
from mailbrief.ui.preferences import DesktopPreferences
from tests.factories import make_action, make_digest_item, make_message
from tests.ui.fake_drafts import FakeDrafts


class FakeBackend(FakeDrafts):
    def __init__(self) -> None:
        super().__init__()
        self.saved = DailyDigest(
            account_id="owner@example.com",
            local_date=date(2026, 9, 4),
            timezone_name="UTC",
            generated_at_utc=datetime(2026, 9, 4, 12, tzinfo=UTC),
            status=DigestStatus.COMPLETE,
            items=(
                make_digest_item(
                    subject='<a href="https://evil.example">private subject</a>',
                    source_url="https://mail.google.com/mail/u/?authuser=owner%40example.com#all/abc",
                ),
            ),
        )
        self.calls = 0
        self.loads = 0
        self.action_calls: list[tuple[object, ...]] = []
        self.action_fail: Exception | None = None
        self.list_calls = 0
        self.list_fail: Exception | None = None
        self.actions: dict[ActionFilter, tuple[Action, ...]] = {}
        self.action_counts: dict[ActionFilter, int] = {}
        self.count_calls: list[ActionFilter] = []
        self.candidates: tuple[str, ...] = ("message-1",)
        self.blocked: frozenset[str] = frozenset()
        self.outside: frozenset[str] = frozenset()
        self.declined: frozenset[str] = frozenset()
        self.limit = 10
        self.closed = False
        self.cleaned = False
        self.fail: Exception | None = None
        self.connect_fail: Exception | None = None
        self.started = asyncio.Event()
        self.selected: tuple[str, ...] | None = None
        self.approved = False
        self.preferences = DesktopPreferences()
        # The owner's preferences, as the database would hold them.
        self.owner_preferences = OwnerPreferences.defaults()
        self.owner_fail: Exception | None = None
        self.owner_saves: list[tuple[PreferencesEdit, int]] = []
        # Saved briefs by account and day, the missed days offered, and each generate's day.
        self.briefs: dict[tuple[str, date], DailyDigest] = {}
        self.missed: tuple[date, ...] = ()
        self.generated_days: list[date | None] = []
        self.key_value: SecretStr | None = None
        self.revoked = False
        # The actions continuing each message's thread, for every brief shown; links_fail
        # makes every read fail. source_added and add_changed are what accept_into reports,
        # and undo_previous the decision the last undo was given to put back.
        self.links: dict[str, tuple[ThreadLink, ...]] = {}
        self.links_fail: Exception | None = None
        self.link_calls: list[DailyDigest] = []
        # The pending proposals of each message's email, for every brief shown;
        # proposals_fail makes every read fail.
        self.proposals: dict[str, tuple[ActionProposal, ...]] = {}
        self.proposals_fail: Exception | None = None
        self.proposal_calls: list[DailyDigest] = []
        self.proposals_created = 0  # What a brief run reports having proposed.
        # Automatic runs: what each returns (or raises), how many ran, and an optional hold.
        self.automatic_calls = 0
        self.window: MainWindow | None = None  # Set by a test that watches the window's panels.
        self.automatic_result: BriefRunResult | Exception | None = None
        self.automatic_hold: asyncio.Event | None = None
        self.automatic_panels: list[tuple[bool, bool]] = []
        # The automatic-analysis permission: None is "no consent yet".
        self.permission: AutoSendStatus | None = None
        self.permission_fail: Exception | None = None
        self.permission_saves: list[int] = []
        self.permission_accounts: list[str | None] = []
        self.source_added = True
        self.add_changed = True
        self.undo_previous: DecisionSnapshot | None = None
        self.sync = SyncResult(
            account_id="owner@example.com",
            range_start_utc=datetime(2026, 9, 4, tzinfo=UTC),
            range_end_utc=datetime(2026, 9, 5, tzinfo=UTC),
            status=SyncStatus.COMPLETE,
            page_count=1,
            message_count=1,
        )

    async def load_saved(self) -> DailyDigest:
        self.loads += 1
        return self.saved

    # Action calls record what the window asked for; action_fail makes the next one raise.
    async def _act(self, name: str, *args: object) -> None:
        self.action_calls.append((name, *args))
        if self.action_fail is not None:
            failure, self.action_fail = self.action_fail, None
            raise failure

    async def list_actions(self, view: ActionFilter) -> tuple[Action, ...]:
        self.list_calls += 1
        if self.list_fail is not None:
            raise self.list_fail
        return self.actions.get(view, ())

    async def count_actions(self, view: ActionFilter) -> int:
        self.count_calls.append(view)
        return self.action_counts.get(view, len(self.actions.get(view, ())))

    async def accept_suggestion(self, suggestion_id: int) -> Action:
        await self._act("accept_suggestion", suggestion_id)
        return make_action(title="Approve the budget")

    async def brief_links(self, digest: DailyDigest) -> dict[str, tuple[ThreadLink, ...]]:
        self.link_calls.append(digest)
        if self.links_fail is not None:
            raise self.links_fail
        return self.links

    async def brief_proposals(self, digest: DailyDigest) -> dict[str, tuple[ActionProposal, ...]]:
        self.proposal_calls.append(digest)
        if self.proposals_fail is not None:
            raise self.proposals_fail
        return self.proposals

    async def apply_proposal(self, proposal_id: int, revision: int) -> Action:
        await self._act("apply_proposal", proposal_id, revision)
        return make_action(title="Send the deck", revision=revision + 1)

    async def undo_apply_proposal(self, proposal_id: int, revision: int) -> Action:
        await self._act("undo_apply_proposal", proposal_id, revision)
        return make_action(title="Send the deck", revision=revision + 1)

    async def dismiss_proposal(self, proposal_id: int) -> None:
        await self._act("dismiss_proposal", proposal_id)

    async def restore_proposal(self, proposal_id: int) -> None:
        await self._act("restore_proposal", proposal_id)

    async def accept_into(self, suggestion_id: int, public_id: str, revision: int) -> AcceptedInto:
        await self._act("accept_into", suggestion_id, public_id, revision)
        action = make_action(public_id=public_id, title="Send the deck", revision=revision + 1)
        return AcceptedInto(
            action,
            source_added=self.source_added,
            changed=self.add_changed,
            previous=DecisionSnapshot(
                SuggestionState.DISMISSED, None, datetime(2026, 9, 1, tzinfo=UTC)
            ),
        )

    async def undo_accept_into(
        self,
        suggestion_id: int,
        public_id: str,
        revision: int,
        remove_source: bool,
        *,
        previous: DecisionSnapshot,
    ) -> Action:
        self.undo_previous = previous
        await self._act("undo_accept_into", suggestion_id, public_id, revision, remove_source)
        return make_action(public_id=public_id, revision=revision + 1)

    async def mark_thread_seen(self, public_id: str, revision: int) -> Action:
        await self._act("mark_thread_seen", public_id, revision)
        return make_action(public_id=public_id, revision=revision + 1)

    async def dismiss_suggestion(self, suggestion_id: int) -> None:
        await self._act("dismiss_suggestion", suggestion_id)

    async def restore_suggestion(self, suggestion_id: int) -> None:
        await self._act("restore_suggestion", suggestion_id)

    async def unaccept_action(self, public_id: str, revision: int) -> None:
        await self._act("unaccept_action", public_id, revision)

    async def save_action(
        self,
        public_id: str,
        revision: int,
        edit: ActionEdit,
        steps: Sequence[StepEdit] | None = None,
    ) -> Action:
        await self._act("save_action", public_id, revision, edit, steps)
        return make_action(title=edit.title, revision=revision + 1)

    async def complete_action(self, public_id: str, revision: int) -> Action:
        await self._act("complete_action", public_id, revision)
        return make_action(revision=revision + 1)

    async def reopen_action(self, public_id: str, revision: int) -> Action:
        await self._act("reopen_action", public_id, revision)
        return make_action(revision=revision + 1)

    async def delete_action(self, public_id: str, revision: int) -> None:
        await self._act("delete_action", public_id, revision)

    async def restore_action(self, public_id: str) -> Action:
        await self._act("restore_action", public_id)
        return make_action()

    async def cached_accounts(self) -> tuple[CachedAccount, ...]:
        return (CachedAccount(account_id=1, email_address="owner@example.com"),)

    async def cached_messages(self, account_id: int, day: date, offset: int = 0) -> CachedMailPage:
        return CachedMailPage(
            account=(await self.cached_accounts())[0],
            local_date=day,
            timezone_name="UTC",
            messages=(make_message(),),
            offset=offset,
        )

    async def get_preferences(self) -> DesktopPreferences:
        return self.preferences

    async def save_preferences(self, preferences: DesktopPreferences) -> None:
        self.preferences = preferences

    async def get_owner_preferences(self) -> OwnerPreferences:
        if self.owner_fail is not None:
            raise self.owner_fail
        return self.owner_preferences

    async def save_owner_preferences(
        self, edit: PreferencesEdit, revision: int
    ) -> OwnerPreferences:
        self.owner_saves.append((edit, revision))
        if revision != self.owner_preferences.revision:
            raise PreferencesConflictError()
        self.owner_preferences = OwnerPreferences(
            **edit.model_dump(), revision=revision + 1, updated_at_utc=datetime.now(UTC)
        )
        return self.owner_preferences

    async def reset_owner_preferences(self) -> OwnerPreferences:
        self.owner_fail = None
        self.owner_preferences = OwnerPreferences(
            revision=self.owner_preferences.revision + 1, updated_at_utc=datetime.now(UTC)
        )
        return self.owner_preferences

    async def save_key(self, key: SecretStr) -> None:
        self.key_value = key

    async def remove_key(self) -> None:
        self.key_value = None

    async def revoke_consent(self) -> int:
        self.revoked = True
        return 1

    async def connect(self, *, silent_only: bool) -> str:
        if self.connect_fail:
            raise self.connect_fail
        return "owner@example.com"

    async def ai_status(self) -> str:
        return "AI: configured"

    async def disconnect(self) -> None:
        pass

    async def generate(
        self,
        gate: ConsentGate,
        review: ShortlistGate,
        cancel: asyncio.Event,
        progress: Callable[[SyncProgress], None],
        local_date: date | None = None,
    ) -> BriefRunResult:
        self.calls += 1
        self.generated_days.append(local_date)
        self.started.set()
        try:
            if self.fail:
                raise self.fail
            self.selected = await review.review(
                tuple(
                    RankedMessage(
                        message=make_message(provider_message_id=key), score=50, reasons=()
                    )
                    for key in self.candidates
                ),
                tuple(key for key in self.candidates if key not in self.blocked),
                blocked_ids=self.blocked,
                outside_ids=self.outside,
                declined_ids=self.declined,
                limit=self.limit,
            )
            self.approved = await gate.confirm(
                TransmissionPreview(
                    provider_name="groq",
                    model_name="test-model",
                    message_count=1,
                    truncated_count=0,
                    reused_count=0,
                    first_use=True,
                    body_character_limit=4000,
                    privacy_notice="Enable Zero Data Retention.",
                )
            )
            digest = self.saved
            if local_date is not None:
                digest = self.saved.model_copy(update={"local_date": local_date})
                self.briefs[(digest.account_id, local_date)] = digest
            return BriefRunResult(
                status=BriefStatus.SAVED if self.approved else BriefStatus.CONSENT_DECLINED,
                sync=self.sync,
                digest=digest if self.approved else None,
                proposals_created=self.proposals_created,
            )
        finally:
            await asyncio.sleep(0)
            self.cleaned = True

    async def generate_automatic(
        self, cancel: asyncio.Event, progress: Callable[[SyncProgress], None]
    ) -> BriefRunResult:
        self.automatic_calls += 1
        window = self.window
        if window is not None:  # Whether a review or consent panel is ever showing.
            self.automatic_panels.append(
                (window.review_panel.isVisible(), window.consent_panel.isVisible())
            )
        if self.automatic_hold is not None:
            await self.automatic_hold.wait()
        result = self.automatic_result
        if isinstance(result, Exception):
            raise result
        return result or BriefRunResult(
            status=BriefStatus.READY_FOR_REVIEW, sync=self.sync, ready=2
        )

    async def auto_send_status(self, account_email: str | None) -> AutoSendStatus | None:
        self.permission_accounts.append(account_email)
        if self.permission_fail is not None:
            raise self.permission_fail
        return self.permission

    async def set_auto_send(self, limit: int, account_email: str | None) -> AutoSendStatus | None:
        self.permission_accounts.append(account_email)
        self.permission_saves.append(limit)
        if self.permission is None:
            raise ConfigurationError(NO_CONSENT)
        stamp = datetime(2026, 9, 30, 14, tzinfo=UTC) if limit else None
        self.permission = self.permission.model_copy(
            update={"limit": limit, "granted_at_utc": stamp}
        )
        return self.permission

    async def list_briefs(self) -> tuple[SavedBriefSummary, ...]:
        everything = {(self.saved.account_id, self.saved.local_date): self.saved, **self.briefs}
        return tuple(
            SavedBriefSummary(
                account_email=digest.account_id,
                local_date=digest.local_date,
                timezone_name=digest.timezone_name,
                status=digest.status,
                generated_at_utc=digest.generated_at_utc,
                item_count=len(digest.items),
            )
            for _, digest in sorted(everything.items(), key=lambda item: item[0][1], reverse=True)
        )

    async def load_brief(self, account_email: str, local_date: date) -> DailyDigest | None:
        if (account_email, local_date) == (self.saved.account_id, self.saved.local_date):
            return self.saved
        return self.briefs.get((account_email, local_date))

    async def missed_days(self, account_email: str) -> tuple[date, ...]:
        return self.missed

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def window(qtbot: QtBot) -> MainWindow:
    result = MainWindow(FakeBackend())
    qtbot.addWidget(result)
    return result


async def finish(window: MainWindow) -> None:
    assert window.task is not None
    await window.task


async def test_offline_startup_restores_saved_content(window: MainWindow) -> None:
    assert isinstance(window.backend, FakeBackend)
    window.backend.connect_fail = ProviderError("private exception")
    window.start(window.initialize)
    await finish(window)
    assert "private subject" in window.digest.toPlainText()
    assert "offline" in window.connection.text()
    assert window.ai.text() == "AI: configured"
    assert window.generate_button.isEnabled()
    assert "private exception" not in window.status.text()


async def test_review_consent_and_repeat_run(window: MainWindow) -> None:
    window.start(window.initialize)
    await finish(window)
    window.generate_button.click()
    await asyncio.sleep(0)
    assert not window.review_panel.isHidden()
    assert not window.connect_button.isEnabled()
    assert not window.generate_button.isEnabled()
    window.start(window._generate)
    window.review_button.click()
    await asyncio.sleep(0)
    assert not window.consent_panel.isHidden()
    assert "4,000" in window.disclosure.text()
    assert "credentials" in window.disclosure.text()
    window.approve_button.click()
    await finish(window)
    assert isinstance(window.backend, FakeBackend)
    assert window.backend.calls == 1
    assert window.backend.selected == ("message-1",)
    assert window.backend.approved
    assert "Brief saved" in window.status.text()
    window.generate_button.click()
    await asyncio.sleep(0)
    window.cancel_button.click()
    await finish(window)
    assert window.backend.calls == 2
    assert window.backend.cleaned
    assert "Cancelled" in window.status.text()


async def test_unchecked_messages_are_excluded_and_decline_is_explicit(window: MainWindow) -> None:
    assert isinstance(window.backend, FakeBackend)
    window.backend.candidates = ("message-1", "message-2")
    await window.initialize()
    window.start(window._generate)
    await asyncio.sleep(0)
    item = window.shortlist.item(1)
    assert item is not None
    item.setCheckState(Qt.CheckState.Unchecked)
    window.review_button.click()
    await asyncio.sleep(0)
    window.decline_button.click()
    await finish(window)
    assert window.backend.selected == ("message-1",)
    assert not window.backend.approved
    assert "declined" in window.status.text()
    assert "private subject" in window.digest.toPlainText()


async def test_continue_needs_at_least_one_message(window: MainWindow) -> None:
    """Continuing with nothing would save an empty brief over today's saved one."""
    await window.initialize()
    window.start(window._generate)
    await asyncio.sleep(0)
    item = window.shortlist.item(0)
    assert item is not None
    item.setCheckState(Qt.CheckState.Unchecked)

    assert not window.review_button.isEnabled()
    assert "at least one" in window.status.text()
    window._accept_review()  # Even a direct call cannot continue with nothing selected.
    await asyncio.sleep(0)
    assert not window.review_panel.isHidden()
    assert window.task is not None and not window.task.done()

    item.setCheckState(Qt.CheckState.Checked)
    assert window.review_button.isEnabled()
    window.review_button.click()
    await asyncio.sleep(0)
    window.decline_button.click()
    await finish(window)
    assert isinstance(window.backend, FakeBackend)
    assert window.backend.selected == ("message-1",)


@pytest.mark.parametrize("stage", ["before_start", "review", "consent"])
async def test_shutdown_cancels_once_and_cleans_resources(window: MainWindow, stage: str) -> None:
    window.start(window._generate)
    if stage != "before_start":
        await asyncio.sleep(0)
    if stage == "consent":
        window.review_button.click()
        await asyncio.sleep(0)
    window.close()
    await window.shutdown()
    assert isinstance(window.backend, FakeBackend)
    assert window.backend.closed
    if stage != "before_start":
        assert window.backend.cleaned
    assert window.task is not None and window.task.done()


@pytest.mark.parametrize(
    "error",
    [AuthenticationRequiredError("secret"), ProviderError("secret"), RuntimeError("secret")],
)
async def test_failed_refresh_preserves_brief_and_hides_exception(
    window: MainWindow,
    error: Exception,
) -> None:
    await window.initialize()
    before = window.digest.toPlainText()
    assert isinstance(window.backend, FakeBackend)
    window.backend.fail = error
    window.start(window._generate)
    await finish(window)
    assert window.digest.toPlainText() == before
    assert "secret" not in window.status.text()
    assert window.generate_button.isEnabled()


async def test_links_only_open_explicit_saved_gmail_source(
    window: MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await window.initialize()
    opened = Mock(return_value=True)
    monkeypatch.setattr(QDesktopServices, "openUrl", opened)
    assert '<a href="https://evil.example">private subject</a>' in window.digest.toPlainText()
    window.digest.anchorClicked.emit(QUrl("https://evil.example"))
    opened.assert_not_called()
    window.digest.anchorClicked.emit(QUrl("mailbrief:source/0"))
    assert opened.call_count == 1
    assert "authuser=owner%40example.com" in opened.call_args.args[0].toString()


@pytest.mark.parametrize(
    "precision, expected",
    [
        (DeadlinePrecision.DATE, "Due 2026-09-04 (date only)"),
        (DeadlinePrecision.DATETIME, "Due 2026-09-04T21:00+00:00"),
    ],
)
async def test_brief_displays_resolved_deadline(
    window: MainWindow,
    precision: DeadlinePrecision,
    expected: str,
) -> None:
    assert isinstance(window.backend, FakeBackend)
    digest = window.backend.saved.model_copy(
        update={
            "items": (
                make_digest_item(deadline_precision=precision, deadline_date=date(2026, 9, 4)),
            )
        }
    )
    window.digest.show_digest(digest)
    assert expected in window.digest.toPlainText()


@pytest.mark.parametrize(
    "code, expected",
    [
        ("PERMISSION_DENIED", "Disconnect and reconnect"),
        ("PROVIDER_ERROR", "offline"),
        ("RATE_LIMITED", "retry later"),
        ("AI_AUTH_FAILED", "Replace it in Settings"),
        ("AI_NETWORK_BLOCKED", "home or mobile"),
        ("AI_USAGE_LIMIT", "fewer messages"),
        ("AI_RATE_LIMITED", "retry later"),
    ],
)
@pytest.mark.parametrize("partial", [False, True])
async def test_failure_guidance(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch, code: str, expected: str, partial: bool
) -> None:
    assert isinstance(window.backend, FakeBackend)
    result = BriefRunResult(
        status=BriefStatus.SAVED if partial else BriefStatus.SYNC_FAILED,
        sync=window.backend.sync.model_copy(update={"error_code": code}),
        error_code=code,
        digest=window.backend.saved.model_copy(update={"status": DigestStatus.PARTIAL})
        if partial
        else None,
    )
    monkeypatch.setattr(window.backend, "generate", AsyncMock(return_value=result))
    window.start(window._generate)
    await finish(window)
    assert window.status.text().count(expected) == 1


_REFRESH_FAILED = "Refresh failed. The displayed saved brief is unchanged."
# What Sync and review says for each way a run can end without saving a brief.
_UNSAVED = {
    BriefStatus.CANCELLED: "Cancelled. The displayed saved brief is unchanged.",
    BriefStatus.CONSENT_DECLINED: "Transmission declined. No messages sent to AI in this run.",
    BriefStatus.SYNC_FAILED: _REFRESH_FAILED,
    BriefStatus.ANALYSIS_FAILED: _REFRESH_FAILED,
    # Only an automatic run ends this way; from Sync and review it would read as a failure.
    BriefStatus.READY_FOR_REVIEW: _REFRESH_FAILED,
}


@pytest.mark.parametrize("status", list(_UNSAVED), ids=[status.value for status in _UNSAVED])
async def test_a_run_from_sync_and_review_that_saves_no_brief_says_how_it_ended(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch, status: BriefStatus
) -> None:
    assert isinstance(window.backend, FakeBackend)
    result = BriefRunResult(status=status, sync=window.backend.sync)
    monkeypatch.setattr(window.backend, "generate", AsyncMock(return_value=result))

    window.start(window._generate)
    await finish(window)

    assert window.status.text() == _UNSAVED[status]


def test_every_status_but_saved_has_its_text_above() -> None:
    assert {*_UNSAVED, BriefStatus.SAVED} == set(BriefStatus)


async def test_startup_storage_failure_can_retry(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert isinstance(window.backend, FakeBackend)
    monkeypatch.setattr(
        window.backend,
        "load_saved",
        AsyncMock(side_effect=[RuntimeError("SECRET"), window.backend.saved]),
    )
    window.start(window.initialize)
    await finish(window)
    assert not window.generate_button.isEnabled()
    assert "newer" in window.status.text()
    assert "SECRET" not in window.status.text()
    assert "checking" not in window.connection.text()
    assert "No saved brief yet" not in window.digest.toPlainText()
    window.retry_button.click()
    await finish(window)
    assert window.generate_button.isEnabled()
    assert window.retry_button.isHidden()


async def test_static_setup_error_is_actionable(window: MainWindow) -> None:
    from mailbrief.errors import ConfigurationError

    assert isinstance(window.backend, FakeBackend)
    message = "This is a Web OAuth client. Create a Desktop app OAuth client instead."
    window.backend.connect_fail = ConfigurationError(message)
    window.start(window._connect)
    await finish(window)
    assert window.status.text() == message
