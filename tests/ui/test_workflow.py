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

from mailbrief.domain.actions import Action, ActionEdit, ActionFilter, StepEdit
from mailbrief.domain.analysis import DeadlinePrecision
from mailbrief.domain.briefs import BriefRunResult, BriefStatus, TransmissionPreview
from mailbrief.domain.cached_mail import CachedAccount, CachedMailPage
from mailbrief.domain.digests import DailyDigest, DigestStatus, SyncProgress, SyncResult, SyncStatus
from mailbrief.domain.messages import RankedMessage
from mailbrief.domain.preferences import OwnerPreferences, PreferencesEdit
from mailbrief.ports.errors import AuthenticationRequiredError, ProviderError
from mailbrief.services.brief import ConsentGate, ShortlistGate
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
        self.key_value: SecretStr | None = None
        self.revoked = False
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
    ) -> BriefRunResult:
        self.calls += 1
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
            return BriefRunResult(
                status=BriefStatus.SAVED if self.approved else BriefStatus.CONSENT_DECLINED,
                sync=self.sync,
                digest=self.saved if self.approved else None,
            )
        finally:
            await asyncio.sleep(0)
            self.cleaned = True

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
