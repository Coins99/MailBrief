"""The Briefs dialog, viewing a past brief, and briefing a missed day from the window."""

import asyncio
from datetime import UTC, date, datetime

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot

from mailbrief.domain.digests import DigestStatus, SavedBriefSummary
from mailbrief.domain.preferences import OwnerPreferences
from mailbrief.ui.digest_view import DigestView
from mailbrief.ui.history_view import BriefHistoryDialog
from mailbrief.ui.main_window import MainWindow
from tests.ui.test_workflow import FakeBackend, finish

TODAY = date(2026, 9, 5)
SAVED_DAY = date(2026, 9, 4)  # FakeBackend's saved brief: yesterday.
PAST = date(2026, 9, 3)


def summary(day: date, email: str = "owner@example.com", items: int = 2) -> SavedBriefSummary:
    return SavedBriefSummary(
        account_email=email,
        local_date=day,
        timezone_name="UTC",
        status=DigestStatus.COMPLETE,
        generated_at_utc=datetime(2026, 9, 5, 12, tzinfo=UTC),
        item_count=items,
    )


@pytest.fixture
def dialog(qtbot: QtBot) -> BriefHistoryDialog:
    result = BriefHistoryDialog(None)  # type: ignore[arg-type]
    qtbot.addWidget(result)
    return result


def signals(dialog: BriefHistoryDialog) -> tuple[list[tuple[str, date]], list[date]]:
    opened: list[tuple[str, date]] = []
    briefed: list[date] = []
    dialog.open_requested.connect(lambda email, day: opened.append((email, day)))
    dialog.generate_requested.connect(briefed.append)
    return opened, briefed


def test_saved_briefs_and_missed_days_are_listed(dialog: BriefHistoryDialog) -> None:
    dialog.configure(
        (summary(TODAY), summary(PAST, items=1)),
        (SAVED_DAY, date(2026, 9, 2)),
        "owner@example.com",
        TODAY,
    )

    rows = [dialog.saved.item(row).text() for row in range(dialog.saved.count())]
    assert rows == [
        "2026-09-05 · complete · 2 items · owner@example.com",
        "2026-09-03 · complete · 1 item · owner@example.com",
    ]
    assert dialog.missed.item(0).text() == "2026-09-04 · no brief"
    assert dialog.missed_note.isHidden()
    assert dialog.saved.accessibleName() and dialog.missed.accessibleName()
    # Today's brief is open-only: today is briefed with Sync and review.
    assert dialog.open_button.isEnabled() and not dialog.brief_button.isEnabled()


def test_without_a_connection_missed_days_ask_for_one(dialog: BriefHistoryDialog) -> None:
    opened, briefed = signals(dialog)
    dialog.configure((summary(PAST),), (), None, TODAY)

    assert dialog.missed_note.text() == "Connect Gmail to brief missed days."
    dialog.brief_button.click()  # A saved past day is still offered, but needs Gmail.

    assert briefed == []
    assert dialog.status.text() == "Connect Gmail to brief a past day."
    dialog.open_button.click()
    assert opened == [("owner@example.com", PAST)]


def test_briefing_a_missed_day_needs_no_confirmation(dialog: BriefHistoryDialog) -> None:
    _, briefed = signals(dialog)
    dialog.configure((summary(TODAY),), (SAVED_DAY,), "owner@example.com", TODAY)

    dialog.missed.setCurrentRow(0)
    assert dialog.saved.currentRow() == -1  # One day is chosen at a time.
    assert not dialog.open_button.isEnabled()
    dialog.brief_button.click()

    assert briefed == [SAVED_DAY]


def test_replacing_a_saved_brief_asks_first(dialog: BriefHistoryDialog) -> None:
    _, briefed = signals(dialog)
    dialog.configure((summary(PAST),), (), "owner@example.com", TODAY)

    dialog.brief_button.click()
    assert dialog.confirm_label.text() == "This replaces the saved brief for 2026-09-03."
    assert briefed == []
    dialog.keep_button.click()
    assert dialog.confirm_panel.isHidden() and briefed == []

    dialog.brief_button.click()
    dialog.replace_button.click()
    assert briefed == [PAST]


def test_another_account_s_brief_can_t_be_replaced(dialog: BriefHistoryDialog) -> None:
    _, briefed = signals(dialog)
    dialog.configure((summary(PAST, "other@example.com"),), (), "owner@example.com", TODAY)
    dialog.brief_button.click()
    assert briefed == [] and "another account" in dialog.status.text()


def test_days_outside_the_last_seven_can_t_be_briefed(dialog: BriefHistoryDialog) -> None:
    dialog.configure((summary(date(2026, 8, 28)),), (), "owner@example.com", TODAY)
    assert not dialog.brief_button.isEnabled()


def test_return_opens_a_saved_brief(dialog: BriefHistoryDialog) -> None:
    opened, _ = signals(dialog)
    dialog.configure((summary(PAST),), (), "owner@example.com", TODAY)
    dialog.show()
    dialog.saved.setFocus()
    QTest.keyClick(dialog.saved, Qt.Key.Key_Return)
    assert opened == [("owner@example.com", PAST)]


def test_the_brief_says_what_it_covers(qtbot: QtBot) -> None:
    view = DigestView()
    qtbot.addWidget(view)
    backend = FakeBackend()
    view.show_digest(backend.saved)
    assert (
        "Covers messages received on 2026-09-04 up to 12:00 (UTC) that were in your Inbox "
        "then." in view.toPlainText()
    )


# The window.


@pytest.fixture
def backend() -> FakeBackend:
    result = FakeBackend()
    result.owner_preferences = OwnerPreferences(revision=1, time_zone="UTC")
    result.briefs[("owner@example.com", PAST)] = result.saved.model_copy(
        update={"local_date": PAST}
    )
    result.missed = (date(2026, 9, 2),)
    return result


@pytest.fixture
async def window(qtbot: QtBot, backend: FakeBackend) -> MainWindow:
    result = MainWindow(backend)
    result.now = lambda: datetime(2026, 9, 5, 15, tzinfo=UTC)
    qtbot.addWidget(result)
    result.start(result.initialize)  # Connects silently as owner@example.com.
    await finish(result)
    return result


def heading(window: MainWindow) -> str:
    return window.digest.toPlainText().splitlines()[0]


async def settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


async def review_and_approve(window: MainWindow) -> None:
    """FakeBackend's generate() waits for the window's review, then its consent."""
    await settle()
    window.review_button.click()
    await settle()
    window.approve_button.click()
    await finish(window)


async def open_past(window: MainWindow) -> None:
    window.briefs_button.click()
    await finish(window)
    window.history_dialog.saved.setCurrentRow(1)  # 2026-09-04 (latest), then 2026-09-03.
    window.history_dialog.open_button.click()
    await finish(window)


async def test_the_dialog_lists_the_connected_account_s_missed_days(
    window: MainWindow,
) -> None:
    window.briefs_button.click()
    await finish(window)

    assert window.history_dialog.isVisible()
    assert window.history_dialog.saved.count() == 2
    assert window.history_dialog.missed.item(0).text() == "2026-09-02 · no brief"
    window.history_dialog.reject()


async def test_opening_a_past_brief_shows_the_banner_until_back_to_latest(
    window: MainWindow,
) -> None:
    await open_past(window)

    assert heading(window).startswith("2026-09-03")
    assert not window.viewing.isHidden()
    assert window.viewing_label.text() == "Viewing the brief for 2026-09-03."
    assert not window.history_dialog.isVisible()

    window.latest_button.click()
    await finish(window)

    assert window.viewing.isHidden()
    assert heading(window).startswith("2026-09-04")


async def test_opening_the_latest_brief_shows_no_banner(window: MainWindow) -> None:
    window.briefs_button.click()
    await finish(window)
    window.history_dialog.open_button.click()  # The first row: the latest brief.
    await finish(window)
    assert window.viewing.isHidden()


async def test_undo_while_viewing_a_past_brief_keeps_it(
    window: MainWindow, backend: FakeBackend
) -> None:
    await open_past(window)

    window.start(lambda: window._accept_suggestion(7), cancellable=False)
    await finish(window)
    assert heading(window).startswith("2026-09-03")
    window.undo_button.click()
    await finish(window)

    assert [call[0] for call in backend.action_calls] == ["accept_suggestion", "unaccept_action"]
    assert heading(window).startswith("2026-09-03")
    assert not window.viewing.isHidden()


async def test_briefing_a_missed_day_passes_its_date_and_shows_it(
    window: MainWindow, backend: FakeBackend
) -> None:
    window.briefs_button.click()
    await finish(window)
    window.history_dialog.missed.setCurrentRow(0)
    window.history_dialog.brief_button.click()
    await review_and_approve(window)

    assert backend.generated_days[-1] == date(2026, 9, 2)
    assert heading(window).startswith("2026-09-02")
    assert window.viewing_label.text() == "Viewing the brief for 2026-09-02."


async def test_briefing_a_saved_day_asks_before_replacing(
    window: MainWindow, backend: FakeBackend
) -> None:
    window.briefs_button.click()
    await finish(window)
    window.history_dialog.saved.setCurrentRow(1)
    window.history_dialog.brief_button.click()
    assert backend.generated_days == []
    assert "replaces the saved brief for 2026-09-03" in window.history_dialog.confirm_label.text()

    window.history_dialog.replace_button.click()
    await review_and_approve(window)

    assert backend.generated_days == [PAST]
    assert window.viewing_label.text() == "Viewing the brief for 2026-09-03."


async def test_offline_briefing_says_a_connection_is_needed(
    window: MainWindow, backend: FakeBackend
) -> None:
    window.disconnect_button.click()
    await finish(window)
    window.briefs_button.click()
    await finish(window)
    dialog = window.history_dialog
    assert dialog.missed.count() == 0 and dialog.missed_note.text().startswith("Connect Gmail")

    dialog.saved.setCurrentRow(1)
    dialog.brief_button.click()
    assert dialog.status.text() == "Connect Gmail to brief a past day."
    window._request_brief_day(PAST)  # Even a direct request can't start without Gmail.
    assert window.task is not None and window.task.done()
    assert backend.generated_days == []
    dialog.reject()


async def test_sync_and_review_returns_to_the_latest_brief(
    window: MainWindow, backend: FakeBackend
) -> None:
    await open_past(window)

    window.generate_button.click()
    await settle()
    assert window.viewing.isHidden()
    assert heading(window).startswith("2026-09-04")
    await review_and_approve(window)

    assert backend.generated_days == [None]
    assert window.viewing.isHidden()
