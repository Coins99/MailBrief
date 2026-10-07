"""Automatic refresh in the window (ADR 0017): when it runs, what it says, and what it never
does. It has no review, no consent question and no dialog; everything goes to the status
line, and it stops when the window does."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QDialog
from pytestqt.qtbot import QtBot

from mailbrief.domain.briefs import BriefRunResult, BriefStatus
from mailbrief.domain.digests import DigestCoverage, SavedBriefSummary, SyncStatus
from mailbrief.domain.preferences import OwnerPreferences
from mailbrief.errors import ConfigurationError
from mailbrief.ports.errors import AuthenticationRequiredError, ProviderError
from mailbrief.services.consent import NO_CONSENT
from mailbrief.services.preferences import PreferencesUnavailableError
from mailbrief.ui.diagnostics import configure_logging, logger
from mailbrief.ui.main_window import MainWindow
from tests.factories import make_auto_send
from tests.ui.test_workflow import FakeBackend, finish

NOW = datetime(2026, 9, 30, 10, 2, tzinfo=UTC)
HOUR = timedelta(hours=1)
DISCONNECTED = "Automatic refresh skipped: connect Gmail first."


def preferences(**fields: object) -> OwnerPreferences:
    return OwnerPreferences(revision=1, time_zone="UTC", **fields)


@pytest.fixture
def backend() -> FakeBackend:
    result = FakeBackend()
    result.owner_preferences = preferences()
    return result


@pytest.fixture
def window(qtbot: QtBot, backend: FakeBackend) -> MainWindow:
    result = MainWindow(backend)
    result.now = lambda: NOW
    backend.window = result
    qtbot.addWidget(result)
    return result


async def due(window: MainWindow) -> None:
    """The scheduler says a run is due; wait for it if one starts."""
    window._refresh_due()
    if window.task is not None:
        await window.task


def ready(backend: FakeBackend, count: int) -> BriefRunResult:
    return BriefRunResult(status=BriefStatus.READY_FOR_REVIEW, sync=backend.sync, ready=count)


def saved(
    backend: FakeBackend, *, analyzed: int = 3, deferred: int = 0, **fields: object
) -> BriefRunResult:
    coverage = DigestCoverage(
        sync_complete=True,
        shortlisted=analyzed + deferred,
        analyzed=analyzed,
        reused=0,
        failed=0,
        skipped=0,
        deferred=deferred,
    )
    return BriefRunResult(
        status=BriefStatus.SAVED,
        sync=backend.sync,
        digest=backend.saved,
        coverage=coverage,
        deferred=deferred,
        **fields,
    )


def due_at(window: MainWindow) -> datetime | None:
    """Read fresh each time: mypy would narrow a property across calls that change it."""
    return window.scheduler.next_due


def ticking(window: MainWindow) -> bool:
    return window.scheduler.active


def open_dialogs(window: MainWindow) -> list[QDialog]:
    """This window's own dialogs that are showing: other tests' windows don't count."""
    return [dialog for dialog in window.findChildren(QDialog) if dialog.isVisible()]


# What a due refresh does


@pytest.mark.parametrize(
    ("count", "sentence"),
    [
        (0, "Nothing new to review."),
        (1, "1 new message is ready to review."),
        (4, "4 new messages are ready to review."),
    ],
)
async def test_a_refresh_without_permission_says_how_many_messages_are_ready(
    window: MainWindow, backend: FakeBackend, count: int, sentence: str
) -> None:
    await window.initialize()
    backend.automatic_result = ready(backend, count)
    loads, lists = backend.loads, backend.list_calls

    await due(window)

    assert window.status.text() == f"Checked Gmail at 10:02. {sentence}"
    assert backend.automatic_calls == 1
    assert backend.loads == loads  # No new brief: the one shown is untouched.
    assert backend.list_calls == lists + 3  # Thread activity and proposals may have moved.
    assert not window.undo_button.isEnabled() or window.undo_button.isHidden()


async def test_a_refresh_that_would_drop_a_carried_message_says_the_brief_needs_a_review(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    backend.automatic_result = BriefRunResult(
        status=BriefStatus.READY_FOR_REVIEW, sync=backend.sync, unrefreshed=9, ready=2
    )
    loads = backend.loads

    await due(window)

    assert window.status.text() == (
        "Checked Gmail at 10:02. Today's brief needs your review: 9 messages from it couldn't "
        "be refreshed automatically. 2 new messages are also ready."
    )
    assert backend.loads == loads  # Nothing was saved: the brief shown is untouched.
    assert window.review_panel.isHidden() and open_dialogs(window) == []  # And nothing opens.


async def test_a_refresh_that_failed_a_carried_message_also_says_why(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    backend.automatic_result = BriefRunResult(
        status=BriefStatus.READY_FOR_REVIEW,
        sync=backend.sync,
        unrefreshed=3,
        ready=1,
        coverage=DigestCoverage(
            sync_complete=True, shortlisted=5, analyzed=2, reused=0, failed=3, skipped=0
        ),
        error_code="AI_RATE_LIMITED",
        ai_calls=2,
    )
    loads = backend.loads

    await due(window)

    guidance = "Groq rate limit reached; retry later."
    assert window.status.text() == (
        "Checked Gmail at 10:02. Today's brief needs your review: 3 messages from it couldn't "
        f"be refreshed automatically. 1 new message is also ready. Automatic refresh: {guidance}"
    )
    assert window.ai.text() == f"AI: {guidance}"
    assert backend.loads == loads  # No brief was written: the one shown is untouched.


async def test_a_refresh_that_could_not_read_a_carried_message_says_so(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    ai = window.ai.text()
    backend.automatic_result = BriefRunResult(
        status=BriefStatus.READY_FOR_REVIEW,
        sync=backend.sync,
        unrefreshed=1,
        ready=2,
        error_code="CARRIED_BODY_FAILED",
    )

    await due(window)

    assert window.status.text() == (
        "Checked Gmail at 10:02. Today's brief needs your review: 1 message from it couldn't "
        "be refreshed automatically. 2 new messages are also ready. "
        "Automatic refresh: A message in today's brief couldn't be read."
    )
    assert window.ai.text() == ai  # Gmail's failure, not the AI's.


async def test_a_refresh_never_opens_a_review_a_consent_panel_or_a_dialog(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    backend.automatic_result = saved(backend)
    backend.automatic_hold = asyncio.Event()

    window._refresh_due()
    await asyncio.sleep(0)
    assert window.task is not None and not window.task.done()  # Mid-run...
    assert window.review_panel.isHidden() and window.consent_panel.isHidden()
    assert open_dialogs(window) == []
    assert window.cancel_button.isEnabled()  # ...and it can be cancelled.
    backend.automatic_hold.set()
    await window.task

    assert backend.automatic_panels == [(False, False)]  # None showing when it called out.
    assert window.review_panel.isHidden() and window.consent_panel.isHidden()
    assert open_dialogs(window) == []
    assert not window.disclosure.text() and window.shortlist.count() == 0


async def test_the_status_line_says_when_a_refresh_is_under_way(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    backend.automatic_hold = asyncio.Event()

    window._refresh_due()
    await asyncio.sleep(0)
    assert window.status.text() == "Automatic refresh: checking Gmail…"
    assert not window.generate_button.isEnabled()  # Busy, like any operation.

    backend.automatic_hold.set()
    assert window.task is not None
    await window.task
    assert window.generate_button.isEnabled()


@pytest.mark.parametrize(
    ("analyzed", "deferred", "proposals", "sentence"),
    [
        (3, 0, 0, "analyzed 3 new messages."),
        (1, 0, 0, "analyzed 1 new message."),
        (3, 2, 0, "analyzed 3 new messages; 2 wait for your review."),
        (1, 1, 0, "analyzed 1 new message; 1 waits for your review."),
        (2, 0, 1, "analyzed 2 new messages. Proposed 1 update to your actions."),
        (
            2,
            3,
            4,
            "analyzed 2 new messages; 3 wait for your review. Proposed 4 updates to your actions.",
        ),
        (0, 0, 0, "nothing new to analyze."),
    ],
)
async def test_a_refresh_with_permission_says_what_it_analyzed_and_what_waits(
    window: MainWindow,
    backend: FakeBackend,
    analyzed: int,
    deferred: int,
    proposals: int,
    sentence: str,
) -> None:
    await window.initialize()
    backend.automatic_result = saved(
        backend, analyzed=analyzed, deferred=deferred, proposals_created=proposals
    )

    await due(window)

    assert window.status.text() == f"Automatic brief at 10:02: {sentence}"


async def test_a_saved_automatic_brief_is_shown_and_ends_an_undo_offer(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()

    async def undo() -> None: ...

    window._offer_undo("Undo accept", undo)
    shown = len(backend.link_calls)
    backend.automatic_result = saved(backend)

    await due(window)

    assert len(backend.link_calls) == shown + 1 and backend.link_calls[-1] == backend.saved
    assert window.undo_button.isHidden()  # A new brief replaces what Undo referred to.


async def test_a_refresh_that_only_checked_keeps_an_undo_offer(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()

    async def undo() -> None: ...

    window._offer_undo("Undo accept", undo)
    backend.automatic_result = ready(backend, 2)

    await due(window)

    assert not window.undo_button.isHidden()


async def test_a_refresh_never_pulls_the_owner_away_from_a_past_brief(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    window._set_shown(("owner@example.com", date(2026, 9, 3)))
    shown = len(backend.link_calls)
    backend.automatic_result = saved(backend)

    await due(window)

    assert len(backend.link_calls) == shown  # Not shown: they are reading another day's.
    assert window.viewing_label.text() == "Viewing the brief for 2026-09-03."
    assert window.status.text().startswith("Automatic brief at 10:02: analyzed 3 new messages")


# When it doesn't run


async def test_a_refresh_waits_a_minute_while_another_operation_runs(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    release = asyncio.Event()

    async def hold() -> None:
        await release.wait()

    window.start(hold)
    await asyncio.sleep(0)

    window._refresh_due()

    assert backend.automatic_calls == 0
    assert due_at(window) == NOW and ticking(window)  # Due again next tick.
    release.set()
    await finish(window)
    await due(window)
    assert backend.automatic_calls == 1


@pytest.mark.parametrize(
    "name",
    [
        "settings_dialog",
        "cached_dialog",
        "proposals_dialog",
        "auto_send_dialog",
        "action_editor",
        "draft_editor",
    ],
)
async def test_a_refresh_waits_while_the_owner_is_in_a_dialog(
    window: MainWindow, backend: FakeBackend, name: str
) -> None:
    await window.initialize()
    dialog = getattr(window, name)
    dialog.show()
    try:
        window._refresh_due()

        assert backend.automatic_calls == 0
        assert due_at(window) == NOW  # Retried at the next tick, not skipped.
        assert window.task is None or window.task.done()
    finally:
        dialog.hide()

    await due(window)
    assert backend.automatic_calls == 1


async def open_briefs(window: MainWindow) -> None:
    window.start(window._open_history)
    assert window.task is not None
    await window.task
    assert window.workspace.current_page() == "briefs"


async def test_the_briefs_page_holds_a_refresh_only_while_a_replacement_is_confirmed(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    await open_briefs(window)
    window.history_panel.confirm_panel.show()  # "This replaces the saved brief…"
    window._refresh_due()

    assert backend.automatic_calls == 0
    assert due_at(window) == NOW  # Retried at the next tick, not skipped.
    assert window.task is None or window.task.done()
    window.history_panel.confirm_panel.hide()
    await due(window)
    assert backend.automatic_calls == 1  # Only looking at Briefs doesn't hold it.


async def test_an_automatic_run_reloads_the_briefs_page(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    await open_briefs(window)
    before = window.history_panel.saved.count()
    earlier = backend.saved.model_copy(
        update={"local_date": backend.saved.local_date - timedelta(days=1)}
    )
    backend.briefs[(earlier.account_id, earlier.local_date)] = earlier

    await due(window)

    assert backend.automatic_calls == 1
    assert window.workspace.current_page() == "briefs"
    assert window.history_panel.saved.count() == before + 1


async def test_a_briefs_page_that_can_t_be_reloaded_doesn_t_fail_the_run(
    window: MainWindow, backend: FakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    await window.initialize()
    await open_briefs(window)
    backend.automatic_result = ready(backend, 2)

    async def unreadable() -> tuple[SavedBriefSummary, ...]:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(backend, "list_briefs", unreadable)
    await due(window)

    assert backend.automatic_calls == 1
    text = window.status.text()
    assert text.startswith("Checked Gmail at 10:02. 2 new messages are ready to review.")
    assert text.endswith("The view could not be refreshed; restart MailBrief to see the latest.")
    assert "failed" not in text  # The run itself succeeded.
    assert window.workspace.current_page() == "briefs"


async def test_a_busy_window_keeps_its_status_line_even_if_gmail_is_disconnected(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.connect_fail = AuthenticationRequiredError("expired")
    await window.initialize()
    release = asyncio.Event()

    async def hold() -> None:
        window.status.setText("Applying your change…")
        await release.wait()

    window.start(hold)
    await asyncio.sleep(0)

    window._refresh_due()

    # Something else is running: the refresh waits and says nothing over what is on screen.
    assert window.status.text() == "Applying your change…"
    assert due_at(window) == NOW and backend.automatic_calls == 0
    release.set()
    await finish(window)


async def test_a_refresh_is_skipped_with_a_message_while_gmail_is_disconnected(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.connect_fail = AuthenticationRequiredError("expired")
    await window.initialize()
    window.scheduler.configure(False, 60)
    next_due = due_at(window)

    window._refresh_due()

    assert backend.automatic_calls == 0
    assert window.status.text() == DISCONNECTED
    assert window.task is None or window.task.done()
    assert due_at(window) == next_due  # Skipped, not retried every minute.


async def test_nothing_runs_before_saved_data_is_open_or_while_closing(
    window: MainWindow, backend: FakeBackend
) -> None:
    window._refresh_due()  # Not initialized: the window isn't ready.
    assert backend.automatic_calls == 0 and window.task is None

    await window.initialize()
    window._closing = True
    window._refresh_due()
    assert backend.automatic_calls == 0 and window.task is None


# How it fails


@pytest.mark.parametrize(
    ("code", "guidance"),
    [
        ("RATE_LIMITED", "Gmail rate limit reached; retry later."),
        ("PROVIDER_ERROR", "Gmail is offline or unavailable. Check your connection and retry."),
    ],
)
async def test_a_failed_sync_is_reported_with_the_static_guidance(
    window: MainWindow, backend: FakeBackend, code: str, guidance: str
) -> None:
    await window.initialize()
    failed = backend.sync.model_copy(update={"status": SyncStatus.FAILED, "error_code": code})
    backend.automatic_result = BriefRunResult(
        status=BriefStatus.SYNC_FAILED, sync=failed, error_code=code
    )

    await due(window)

    assert window.status.text() == f"Automatic refresh: {guidance}"


async def test_a_failed_analysis_is_reported_and_shown_on_the_ai_line(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    backend.automatic_result = BriefRunResult(
        status=BriefStatus.ANALYSIS_FAILED, sync=backend.sync, error_code="AI_AUTH_FAILED"
    )

    await due(window)

    guidance = "Groq rejected the API key. Replace it in Settings."
    assert window.status.text() == f"Automatic refresh: {guidance}"
    assert window.ai.text() == f"AI: {guidance}"


async def test_a_partial_brief_reports_what_it_saved_and_why_some_failed(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    backend.automatic_result = saved(backend, analyzed=1, error_code="AI_RATE_LIMITED")

    await due(window)

    assert window.status.text() == (
        "Automatic brief at 10:02: analyzed 1 new message. "
        "Automatic refresh: Groq rate limit reached; retry later."
    )


async def test_a_session_that_expired_stops_later_runs_until_the_owner_signs_in(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    failed = backend.sync.model_copy(
        update={"status": SyncStatus.FAILED, "error_code": "AUTH_REQUIRED"}
    )
    backend.automatic_result = BriefRunResult(
        status=BriefStatus.SYNC_FAILED, sync=failed, error_code="AUTH_REQUIRED"
    )

    await due(window)

    assert window.status.text() == "Automatic refresh: Connect Gmail, then retry."
    assert window.connection.text() == "Gmail: session expired. Connect Gmail, then retry."
    window._refresh_due()
    assert window.status.text() == DISCONNECTED and backend.automatic_calls == 1


@pytest.mark.parametrize(
    ("error", "text"),
    [
        (
            AuthenticationRequiredError("PRIVATE"),
            "Sign in to Gmail, then retry. The saved brief is still available.",
        ),
        (ProviderError("PRIVATE"), "Provider unavailable. Check your connection and retry."),
        (RuntimeError("PRIVATE mail"), "Operation failed. The displayed saved brief is unchanged."),
        (
            ConfigurationError("MAILBRIEF_GROQ_MODEL is not set"),
            "Choose a Groq model that supports Structured Outputs in Settings.",
        ),
        (PreferencesUnavailableError(), str(PreferencesUnavailableError())),
    ],
    ids=["sign-in", "provider", "unexpected", "configuration", "preferences"],
)
async def test_an_error_is_reported_as_an_automatic_refresh_without_its_payload(
    window: MainWindow, backend: FakeBackend, error: Exception, text: str
) -> None:
    await window.initialize()
    backend.automatic_result = error

    await due(window)

    assert window.status.text() == f"Automatic refresh: {text}"
    assert "PRIVATE" not in window.status.text()
    assert window.review_panel.isHidden() and open_dialogs(window) == []
    # Only that run was an automatic one: a later failure is reported as it always was.
    assert not window._automatic_active
    backend.action_fail = RuntimeError("PRIVATE")
    window.start(lambda: window.backend.delete_action("id", 1))
    await finish(window)
    assert not window.status.text().startswith("Automatic refresh")


async def test_the_owner_can_cancel_a_refresh(window: MainWindow, backend: FakeBackend) -> None:
    await window.initialize()
    backend.automatic_hold = asyncio.Event()
    window._refresh_due()
    await asyncio.sleep(0)

    window.cancel_button.click()
    assert window.task is not None
    await window.task

    assert window.status.text() == "Cancelled. The displayed saved brief is unchanged."
    assert window.generate_button.isEnabled()


async def test_a_cancelled_result_says_so(window: MainWindow, backend: FakeBackend) -> None:
    await window.initialize()
    backend.automatic_result = BriefRunResult(status=BriefStatus.CANCELLED, sync=backend.sync)

    await due(window)

    assert window.status.text() == "Automatic refresh cancelled."


# The schedule


async def test_every_finished_run_sets_the_next_one_an_interval_later(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.owner_preferences = preferences(refresh_interval_minutes=120)
    await window.initialize()
    assert due_at(window) == NOW + 2 * HOUR

    window.now = lambda: NOW + HOUR
    await due(window)
    assert due_at(window) == NOW + 3 * HOUR  # An interval after this one ended.

    backend.automatic_result = RuntimeError("fails")  # Even a failed run counts.
    window.now = lambda: NOW + 5 * HOUR
    await due(window)
    assert due_at(window) == NOW + 7 * HOUR


async def test_a_brief_the_owner_makes_also_pushes_the_next_refresh_back(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.owner_preferences = preferences(refresh_interval_minutes=60)
    await window.initialize()
    window.now = lambda: NOW + timedelta(minutes=50)

    window.start(window._generate)
    for _ in range(3):
        await asyncio.sleep(0)
    window.review_button.click()
    for _ in range(3):
        await asyncio.sleep(0)
    window.approve_button.click()
    await finish(window)

    assert due_at(window) == NOW + timedelta(minutes=50) + HOUR


async def test_the_saved_preferences_configure_the_scheduler_on_load_and_on_save(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.owner_preferences = preferences(refresh_interval_minutes=240)
    await window.initialize()
    assert due_at(window) == NOW + 4 * HOUR and ticking(window)

    window.start(window._open_settings)
    await finish(window)
    panel = window.settings_dialog.preferences_panel
    assert panel.refresh_interval.currentText() == "Every 4 hours"
    panel.refresh_interval.setCurrentIndex(1)  # Every hour.
    panel.save_button.click()
    await finish(window)
    assert due_at(window) == NOW + HOUR

    panel.refresh_interval.setCurrentIndex(0)  # Off.
    panel.save_button.click()
    await finish(window)
    assert due_at(window) is None and not ticking(window)
    window.settings_dialog.reject()


async def test_unreadable_preferences_schedule_nothing(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.owner_fail = PreferencesUnavailableError()

    await window.initialize()

    assert due_at(window) is None and not ticking(window)


async def test_resetting_the_preferences_turns_refresh_off(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.owner_preferences = preferences(refresh_on_launch=True, refresh_interval_minutes=60)
    await window.initialize()
    assert ticking(window)

    window.start(window._reset_owner_preferences)
    await finish(window)

    assert due_at(window) is None and not ticking(window)


# On launch


async def test_a_refresh_on_launch_runs_once_when_gmail_is_connected(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.owner_preferences = preferences(refresh_on_launch=True)

    window.start(window.initialize)  # As the app starts it: one operation, then a refresh.
    await finish(window)
    await asyncio.sleep(0)  # The launch is let go once that operation is over...
    assert window.task is not None
    await window.task

    # ...so it ran at once, not turned away as busy and left for a later tick.
    assert backend.automatic_calls == 1
    assert window.status.text().startswith("Checked Gmail at 10:02.")
    assert due_at(window) is None  # No interval: nothing more is scheduled.


async def test_the_launch_runs_when_started_outside_an_operation_too(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.owner_preferences = preferences(refresh_on_launch=True)

    await window.initialize()
    await asyncio.sleep(0)
    assert window.task is not None
    await window.task

    assert backend.automatic_calls == 1


async def test_nothing_runs_on_launch_unless_asked_to(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    await asyncio.sleep(0)

    assert backend.automatic_calls == 0 and window.task is None


async def test_the_launch_waits_for_gmail_then_runs_once_on_the_first_connection(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.owner_preferences = preferences(refresh_on_launch=True)
    backend.connect_fail = AuthenticationRequiredError("expired")
    await window.initialize()
    await asyncio.sleep(0)
    assert backend.automatic_calls == 0  # Not connected: no launch yet.

    backend.connect_fail = None
    window.connect_button.click()
    await finish(window)
    await asyncio.sleep(0)
    assert window.task is not None
    await window.task
    assert backend.automatic_calls == 1

    window.connect_button.click()  # Connecting again is not another launch.
    await finish(window)
    await asyncio.sleep(0)
    assert backend.automatic_calls == 1


async def test_turning_the_setting_on_later_does_not_launch_this_start(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    await asyncio.sleep(0)

    window._apply_owner_preferences(preferences(refresh_on_launch=True))
    window.connect_button.click()
    await finish(window)
    await asyncio.sleep(0)

    assert backend.automatic_calls == 0


# Closing


async def test_closing_the_window_stops_the_schedule_and_what_is_due(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.owner_preferences = preferences(refresh_interval_minutes=60)
    await window.initialize()
    assert ticking(window)

    window.closeEvent(QCloseEvent())

    assert not ticking(window) and due_at(window) is None
    window.scheduler.tick()
    window._refresh_due()
    window.scheduler.launched()
    assert backend.automatic_calls == 0 and window.task is None
    await window.shutdown()
    assert not ticking(window)


async def test_shutting_down_alone_also_stops_the_timer(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.owner_preferences = preferences(refresh_interval_minutes=240)
    await window.initialize()
    assert ticking(window)

    await window.shutdown()

    assert not ticking(window)
    assert window.scheduler.parent() is window  # No free-standing timer to outlive it.


# Settings: the automatic-analysis permission


OWNER = "owner@example.com"  # The account FakeBackend connects as.


async def open_settings(window: MainWindow, *, connected: bool = True) -> None:
    if connected:
        window._account_email = OWNER
    window.start(window._open_settings)
    await finish(window)


async def test_with_no_account_connected_the_permission_can_not_be_changed(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.permission = make_auto_send(limit=2)  # Some account's; none is connected.
    panel = window.settings_dialog.preferences_panel

    await open_settings(window, connected=False)

    assert (panel.auto_line.text(), panel.auto_button.isEnabled()) == (
        "Connect Gmail to change automatic analysis.",
        False,
    )
    assert backend.permission_accounts == []  # Nothing was read for an unknown account.
    window.start(window._open_auto_send)
    await finish(window)
    assert not window.auto_send_dialog.isVisible()
    assert window.status.text() == "Connect Gmail to change automatic analysis."
    window.settings_dialog.reject()


async def test_the_permission_is_read_and_saved_for_the_connected_account(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.permission = make_auto_send(limit=0)
    await window.initialize()  # Connects as the owner.
    await open_settings(window, connected=False)
    panel = window.settings_dialog.preferences_panel
    assert panel.auto_button.isEnabled()

    panel.auto_button.click()
    await finish(window)
    window.auto_send_dialog.limit.setValue(2)
    window.auto_send_dialog.save_button.click()
    await finish(window)

    assert backend.permission_saves == [2]
    assert set(backend.permission_accounts) == {OWNER}
    window.settings_dialog.reject()

    window.start(window._disconnect)
    await finish(window)
    assert (panel.auto_line.text(), panel.auto_button.isEnabled()) == (
        "Connect Gmail to change automatic analysis.",
        False,
    )


async def test_settings_show_the_permission_from_the_active_consent(
    window: MainWindow, backend: FakeBackend
) -> None:
    panel = window.settings_dialog.preferences_panel

    await open_settings(window)
    assert (panel.auto_line.text(), panel.auto_button.isEnabled()) == (NO_CONSENT, False)
    window.settings_dialog.reject()

    backend.permission = make_auto_send(limit=0)
    await open_settings(window)
    assert (panel.auto_line.text(), panel.auto_button.isEnabled()) == (
        "Off — every run asks you first",
        True,
    )
    window.settings_dialog.reject()

    backend.permission = make_auto_send(
        limit=2, granted_at_utc=datetime(2026, 9, 29, 15, tzinfo=UTC)
    )
    await open_settings(window)
    assert panel.auto_line.text() == "Up to 2 messages per run, since 2026-09-29"
    window.settings_dialog.reject()


async def test_a_permission_that_cannot_be_read_is_said_so_and_not_changeable(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.permission_fail = RuntimeError("PRIVATE")

    await open_settings(window)

    panel = window.settings_dialog.preferences_panel
    assert "couldn't be read" in panel.auto_line.text() and not panel.auto_button.isEnabled()
    assert "PRIVATE" not in panel.auto_line.text() + window.status.text()
    window.settings_dialog.reject()


async def test_change_opens_the_dialog_and_saving_gives_the_permission(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.permission = make_auto_send(limit=0)
    await open_settings(window)
    panel = window.settings_dialog.preferences_panel

    panel.auto_button.click()
    await finish(window)
    dialog = window.auto_send_dialog
    assert dialog.isVisible() and dialog.limit.value() == 0

    dialog.limit.setValue(3)
    dialog.save_button.click()
    await finish(window)

    assert backend.permission_saves == [3]
    assert panel.auto_line.text() == "Up to 3 messages per run, since 2026-09-30"
    assert window.status.text() == "Automatic runs may now send up to 3 messages without asking."
    assert window.settings_dialog.status.text() == window.status.text()

    panel.auto_button.click()
    await finish(window)
    assert dialog.limit.value() == 3  # It opens on what is allowed now.
    dialog.limit.setValue(0)
    dialog.save_button.click()
    await finish(window)
    assert backend.permission_saves == [3, 0]
    assert panel.auto_line.text() == "Off — every run asks you first"
    assert window.status.text() == "Automatic analysis is off. Every run asks you first."
    window.settings_dialog.reject()


async def test_change_without_a_consent_opens_nothing_and_says_why(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.permission = make_auto_send(limit=1)
    await open_settings(window)
    backend.permission = None  # Consent was revoked while Settings was open.

    window.settings_dialog.preferences_panel.auto_button.click()
    await finish(window)

    assert not window.auto_send_dialog.isVisible()
    assert window.status.text() == NO_CONSENT
    assert not window.settings_dialog.preferences_panel.auto_button.isEnabled()
    window.settings_dialog.reject()


async def test_saving_after_the_consent_was_revoked_shows_the_static_message(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.permission = make_auto_send(limit=0)
    await open_settings(window)
    window.settings_dialog.preferences_panel.auto_button.click()
    await finish(window)
    backend.permission = None

    window.auto_send_dialog.limit.setValue(4)
    window.auto_send_dialog.save_button.click()
    await finish(window)

    assert window.status.text() == NO_CONSENT  # Not a generic failure.
    window.settings_dialog.reject()


async def test_a_permission_can_not_be_saved_while_another_operation_runs(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.permission = make_auto_send(limit=0)
    await window.initialize()
    release = asyncio.Event()

    async def hold() -> None:
        await release.wait()

    window.start(hold)
    await asyncio.sleep(0)

    window._request_set_auto_send(2)

    assert backend.permission_saves == []
    assert window.status.text() == "MailBrief is busy; try again in a moment."
    release.set()
    await finish(window)


# The log


async def test_each_automatic_run_leaves_one_counts_only_line_in_the_desktop_log(
    window: MainWindow, backend: FakeBackend, tmp_path: Path
) -> None:
    handler = configure_logging(tmp_path)
    try:
        await window.initialize()
        backend.automatic_result = ready(backend, 4)
        await due(window)
        backend.automatic_result = saved(backend, analyzed=3, deferred=2)
        await due(window)
        handler.flush()
        lines = [
            line
            for line in (tmp_path / "desktop.log").read_text().splitlines()
            if "automatic run" in line
        ]
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert len(lines) == 2
    assert (
        "status=ready_for_review ready=4 unrefreshed=0 analyzed=0 deferred=0 ai_requests=0"
        in lines[0]
    )
    assert "status=saved ready=0 unrefreshed=0 analyzed=3 deferred=2 ai_requests=0" in lines[1]
    assert not any(word in " ".join(lines) for word in ("Budget", "owner@", "subject"))
