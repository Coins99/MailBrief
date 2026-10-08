"""The Briefs panel, viewing a past brief, and briefing a missed day from the window."""

from datetime import UTC, date, datetime

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot

from mailbrief.domain.digests import DigestStatus, SavedBriefSummary
from mailbrief.domain.preferences import OwnerPreferences
from mailbrief.ui.history_view import (
    NEEDS_CONNECTION,
    NONE_MISSED,
    NOT_CONNECTED,
    BriefHistoryPanel,
)
from mailbrief.ui.main_window import MainWindow
from tests.ui.brief_view import shown_text
from tests.ui.test_workflow import FakeBackend, finish
from tests.ui.window_wait import settle

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
def panel(qtbot: QtBot) -> BriefHistoryPanel:
    result = BriefHistoryPanel()
    qtbot.addWidget(result)
    return result


def signals(panel: BriefHistoryPanel) -> tuple[list[tuple[str, date]], list[date]]:
    opened: list[tuple[str, date]] = []
    briefed: list[date] = []
    panel.open_requested.connect(lambda email, day: opened.append((email, day)))
    panel.generate_requested.connect(briefed.append)
    return opened, briefed


def test_saved_briefs_and_missed_days_are_listed(panel: BriefHistoryPanel) -> None:
    panel.configure(
        (summary(TODAY), summary(PAST, items=1)),
        (SAVED_DAY, date(2026, 9, 2)),
        "owner@example.com",
        TODAY,
    )

    rows = [panel.saved.item(row).text() for row in range(panel.saved.count())]
    assert rows == [
        "2026-09-05 · complete · 2 items · owner@example.com",
        "2026-09-03 · complete · 1 item · owner@example.com",
    ]
    assert panel.missed.item(0).text() == "2026-09-04 · no brief"
    assert panel.missed_note.isHidden()
    assert panel.saved.accessibleName() and panel.missed.accessibleName()
    # Today's brief is open-only: today is briefed with Sync and review.
    assert panel.open_button.isEnabled() and not panel.brief_button.isEnabled()


def test_without_a_connection_missed_days_ask_for_one(panel: BriefHistoryPanel) -> None:
    opened, briefed = signals(panel)
    panel.configure((summary(PAST),), (), None, TODAY)

    assert panel.missed_note.text() == "Connect Gmail to brief missed days."
    panel.brief_button.click()  # A saved past day is still offered, but needs Gmail.

    assert briefed == []
    assert panel.status.text() == "Connect Gmail to brief a past day."
    panel.open_button.click()
    assert opened == [("owner@example.com", PAST)]


def test_briefing_a_missed_day_needs_no_confirmation(panel: BriefHistoryPanel) -> None:
    _, briefed = signals(panel)
    panel.configure((summary(TODAY),), (SAVED_DAY,), "owner@example.com", TODAY)

    panel.missed.setCurrentRow(0)
    assert panel.saved.currentRow() == -1  # One day is chosen at a time.
    assert not panel.open_button.isEnabled()
    panel.brief_button.click()

    assert briefed == [SAVED_DAY]


def test_replacing_a_saved_brief_asks_first(panel: BriefHistoryPanel) -> None:
    _, briefed = signals(panel)
    panel.configure((summary(PAST),), (), "owner@example.com", TODAY)

    panel.brief_button.click()
    assert panel.confirm_label.text() == "This replaces the saved brief for 2026-09-03."
    assert briefed == []
    panel.keep_button.click()
    assert panel.confirm_panel.isHidden() and briefed == []

    panel.brief_button.click()
    panel.replace_button.click()
    assert briefed == [PAST]


def test_another_account_s_brief_can_t_be_replaced(panel: BriefHistoryPanel) -> None:
    _, briefed = signals(panel)
    panel.configure((summary(PAST, "other@example.com"),), (), "owner@example.com", TODAY)
    panel.brief_button.click()
    assert briefed == [] and "another account" in panel.status.text()


def test_days_outside_the_last_seven_can_t_be_briefed(panel: BriefHistoryPanel) -> None:
    panel.configure((summary(date(2026, 8, 28)),), (), "owner@example.com", TODAY)
    assert not panel.brief_button.isEnabled()


def test_return_opens_a_saved_brief(panel: BriefHistoryPanel) -> None:
    opened, _ = signals(panel)
    panel.configure((summary(PAST),), (), "owner@example.com", TODAY)
    panel.show()
    panel.saved.setFocus()
    QTest.keyClick(panel.saved, Qt.Key.Key_Return)
    assert opened == [("owner@example.com", PAST)]


def test_disconnecting_shows_the_not_connected_state(panel: BriefHistoryPanel) -> None:
    panel.configure((summary(PAST),), (date(2026, 9, 2),), "owner@example.com", TODAY)
    panel.saved.setCurrentRow(0)
    panel.brief_button.click()
    assert not panel.confirm_panel.isHidden()  # "This replaces the saved brief…"

    panel.set_account(None)

    assert panel.missed.isHidden() and panel.missed.count() == 0
    assert not panel.missed_note.isHidden() and panel.missed_note.text() == NOT_CONNECTED
    assert panel.confirm_panel.isHidden()  # It was for the account that left.
    opened, briefed = signals(panel)
    panel.brief_button.click()
    assert briefed == [] and panel.status.text() == NEEDS_CONNECTION


def test_connecting_drops_what_belonged_to_no_account(panel: BriefHistoryPanel) -> None:
    panel.configure((summary(PAST),), (), None, TODAY)
    panel.saved.setCurrentRow(0)
    panel.brief_button.click()
    assert panel.status.text() == NEEDS_CONNECTION

    panel.set_account("owner@example.com")  # No storage read: the window reloads the page.

    assert panel.status.text() == ""  # That message was about having no account.
    assert panel.missed_note.isHidden() and panel.missed.isHidden()
    panel.brief_button.click()
    assert not panel.confirm_panel.isHidden()  # Now it can replace the saved brief.


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
    return shown_text(window).splitlines()[0]


async def review_and_approve(window: MainWindow) -> None:
    """FakeBackend's generate() waits for the window's review, then its consent."""
    await settle()
    window.review_button.click()
    await settle()
    window.approve_button.click()
    await finish(window)


async def open_past(window: MainWindow) -> None:
    window.workspace.sidebar.page_requested.emit("briefs")
    await finish(window)
    window.history_panel.saved.setCurrentRow(1)  # 2026-09-04 (latest), then 2026-09-03.
    window.history_panel.open_button.click()
    await finish(window)


async def test_the_page_lists_the_connected_account_s_missed_days(
    window: MainWindow,
) -> None:
    window.workspace.sidebar.page_requested.emit("briefs")
    await finish(window)

    assert window.workspace.current_page() == "briefs"
    assert window.history_panel.saved.count() == 2
    assert window.history_panel.missed.item(0).text() == "2026-09-02 · no brief"
    window._show_page("today")


async def test_the_page_follows_disconnect_and_connect(window: MainWindow) -> None:
    window.workspace.sidebar.page_requested.emit("briefs")
    await finish(window)
    panel = window.history_panel
    assert panel.missed.item(0).text() == "2026-09-02 · no brief"

    window.disconnect_button.click()
    await finish(window)
    assert panel.missed.isHidden() and panel.missed_note.text() == NOT_CONNECTED

    window.connect_button.click()  # From the sidebar, with Briefs still showing.
    await finish(window)
    assert window.workspace.current_page() == "briefs"
    assert not panel.missed.isHidden()
    assert panel.missed.item(0).text() == "2026-09-02 · no brief"
    window._show_page("today")


async def test_opening_a_past_brief_shows_the_banner_until_back_to_latest(
    window: MainWindow,
) -> None:
    await open_past(window)

    assert heading(window).startswith("Thu Sep 3")
    assert not window.viewing.isHidden()
    assert window.viewing_label.text() == "Viewing the brief for 2026-09-03."
    assert window.workspace.current_page() == "today"

    window.latest_button.click()
    await finish(window)

    assert window.viewing.isHidden()
    assert heading(window).startswith("Fri Sep 4")


async def test_opening_the_latest_brief_shows_no_banner(window: MainWindow) -> None:
    window.workspace.sidebar.page_requested.emit("briefs")
    await finish(window)
    window.history_panel.open_button.click()  # The first row: the latest brief.
    await finish(window)
    assert window.viewing.isHidden()


async def test_undo_while_viewing_a_past_brief_keeps_it(
    window: MainWindow, backend: FakeBackend
) -> None:
    await open_past(window)

    window.start(lambda: window._accept_suggestion(7), cancellable=False)
    await finish(window)
    assert heading(window).startswith("Thu Sep 3")
    window.undo_button.click()
    await finish(window)

    assert [call[0] for call in backend.action_calls] == ["accept_suggestion", "unaccept_action"]
    assert heading(window).startswith("Thu Sep 3")
    assert not window.viewing.isHidden()


async def test_briefing_a_missed_day_passes_its_date_and_shows_it(
    window: MainWindow, backend: FakeBackend
) -> None:
    window.workspace.sidebar.page_requested.emit("briefs")
    await finish(window)
    window.history_panel.missed.setCurrentRow(0)
    window.history_panel.brief_button.click()
    await review_and_approve(window)

    assert backend.generated_days[-1] == date(2026, 9, 2)
    assert heading(window).startswith("Wed Sep 2")
    assert window.viewing_label.text() == "Viewing the brief for 2026-09-02."


async def test_briefing_a_saved_day_asks_before_replacing(
    window: MainWindow, backend: FakeBackend
) -> None:
    window.workspace.sidebar.page_requested.emit("briefs")
    await finish(window)
    window.history_panel.saved.setCurrentRow(1)
    window.history_panel.brief_button.click()
    assert backend.generated_days == []
    assert "replaces the saved brief for 2026-09-03" in window.history_panel.confirm_label.text()

    window.history_panel.replace_button.click()
    await review_and_approve(window)

    assert backend.generated_days == [PAST]
    assert window.viewing_label.text() == "Viewing the brief for 2026-09-03."


async def test_offline_briefing_says_a_connection_is_needed(
    window: MainWindow, backend: FakeBackend
) -> None:
    window.disconnect_button.click()
    await finish(window)
    window.workspace.sidebar.page_requested.emit("briefs")
    await finish(window)
    panel = window.history_panel
    assert panel.missed.count() == 0 and panel.missed_note.text().startswith("Connect Gmail")

    panel.saved.setCurrentRow(1)
    panel.brief_button.click()
    assert panel.status.text() == "Connect Gmail to brief a past day."
    window._request_brief_day(PAST)  # Even a direct request can't start without Gmail.
    assert window.task is not None and window.task.done()
    assert backend.generated_days == []
    window._show_page("today")


async def test_sync_and_review_returns_to_the_latest_brief(
    window: MainWindow, backend: FakeBackend
) -> None:
    await open_past(window)

    window.generate_button.click()
    await settle()
    assert window.viewing.isHidden()
    assert heading(window).startswith("Fri Sep 4")
    await review_and_approve(window)

    assert backend.generated_days == [None]
    assert window.viewing.isHidden()


def test_the_missed_list_hides_when_no_day_was_missed(panel: BriefHistoryPanel) -> None:
    panel.configure((summary(PAST),), (date(2026, 9, 2),), "owner@example.com", TODAY)
    assert not panel.missed.isHidden() and panel.missed_note.isHidden()
    panel.configure((summary(PAST),), (), "owner@example.com", TODAY)
    assert panel.missed.isHidden()
    assert not panel.missed_note.isHidden() and panel.missed_note.text() == NONE_MISSED
    panel.configure((summary(PAST),), (), None, TODAY)
    assert panel.missed.isHidden() and panel.missed_note.text() == NOT_CONNECTED
