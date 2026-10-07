"""The guided run page: step headings, shortlist rows, the primary Continue and consent.

The gates themselves are unchanged: the same IDs come back, a blocked row is never
checkable, the limit holds, Decline starts focused, and automatic runs never show the page.
"""

import asyncio

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QListWidgetItem, QStyleOptionViewItem, QWidget
from pytestqt.qtbot import QtBot

from mailbrief.domain.briefs import BriefRunResult, BriefStatus
from mailbrief.services.ranking import DECLINED_TEXT, OUTSIDE_REPLY_TEXT
from mailbrief.ui.brief_list import Chip, ChipTone
from mailbrief.ui.main_window import MainWindow
from mailbrief.ui.run_view import (
    EXCLUDED,
    LEFT_OUT,
    ROW_ROLE,
    TRACKED_REPLY,
    ShortlistDelegate,
    ShortlistRow,
)
from tests.ui.test_workflow import FakeBackend


@pytest.fixture
def backend() -> FakeBackend:
    result = FakeBackend()
    result.candidates = ("plain", "outside", "declined", "blocked")
    result.outside = frozenset({"outside"})
    result.declined = frozenset({"declined"})
    result.blocked = frozenset({"blocked"})
    return result


@pytest.fixture
async def window(qtbot: QtBot, backend: FakeBackend) -> MainWindow:
    result = MainWindow(backend)
    qtbot.addWidget(result)
    result.start(result.initialize)
    assert result.task is not None
    await result.task
    result.resize(1100, 720)
    result.show()
    QApplication.processEvents()
    return result


async def settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


async def reviewing(window: MainWindow) -> None:
    """Start a run and stop at the review step."""
    window.start(window._generate)
    await settle()
    assert window.workspace.current_page() == "run"


async def finish(window: MainWindow) -> None:
    assert window.task is not None
    await window.task


def texts(panel: QWidget, name: str) -> list[str]:
    return [label.text() for label in panel.findChildren(QLabel, name)]


def item(window: MainWindow, key: str) -> QListWidgetItem:
    for number in range(window.shortlist.count()):
        found = window.shortlist.item(number)
        if found is not None and found.data(Qt.ItemDataRole.UserRole) == key:
            return found
    raise AssertionError(key)


def check_centre(window: MainWindow, key: str) -> QPoint:
    """The centre of the style's check rect for ``key``'s row, in viewport coordinates."""
    shortlist = window.shortlist
    index = shortlist.indexFromItem(item(window, key))
    option = QStyleOptionViewItem()
    option.initFrom(shortlist.viewport())
    option.rect = shortlist.visualRect(index)
    delegate = shortlist.itemDelegate()
    assert isinstance(delegate, ShortlistDelegate)
    return delegate.check_rect(option, index).center()


async def test_each_step_shows_its_heading(window: MainWindow) -> None:
    await reviewing(window)
    assert texts(window.review_panel, "stepCaption") == ["Step 1 of 2"]
    assert texts(window.review_panel, "stepTitle") == ["Choose what MailBrief reads"]
    assert window.review_panel.isVisible() and not window.consent_panel.isVisible()
    column = window.findChild(QWidget, "runColumn")
    assert column is not None and 0 < column.width() <= 760  # A centred column.
    window.review_button.click()
    await settle()
    assert texts(window.consent_panel, "stepCaption") == ["Step 2 of 2"]
    assert texts(window.consent_panel, "stepTitle") == ["Approve sending to Groq"]
    assert window.consent_panel.isVisible() and not window.review_panel.isVisible()
    assert "groq" in window.disclosure.text().lower()
    window.decline_button.click()
    await finish(window)


async def test_rows_carry_their_chips_and_keep_their_text(window: MainWindow) -> None:
    await reviewing(window)
    rows = {
        key: item(window, key).data(ROW_ROLE) for key in ("plain", "outside", "declined", "blocked")
    }
    assert all(isinstance(row, ShortlistRow) for row in rows.values())
    assert rows["plain"].chips == ()
    assert rows["outside"].chips == (Chip(TRACKED_REPLY, ChipTone.ACCENT),)
    assert rows["declined"].chips == (Chip(LEFT_OUT, ChipTone.ACCENT),)
    assert rows["blocked"].chips == (Chip(EXCLUDED, ChipTone.WARNING),)
    assert rows["blocked"].blocked and not rows["plain"].blocked
    assert rows["plain"].subject == "Approval needed by Friday"
    # The item's own text, the one read aloud, is unchanged.
    assert item(window, "outside").text().endswith(OUTSIDE_REPLY_TEXT)
    assert item(window, "declined").text().endswith(DECLINED_TEXT)
    assert item(window, "blocked").text().endswith("excluded in Settings")
    window.cancel()
    await finish(window)


async def test_clicks_and_space_toggle_only_checkable_rows(window: MainWindow) -> None:
    await reviewing(window)
    shortlist, viewport = window.shortlist, window.shortlist.viewport()
    plain, blocked = item(window, "plain"), item(window, "blocked")
    assert plain.checkState() == Qt.CheckState.Checked
    centre = check_centre(window, "plain")
    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=centre)
    assert plain.checkState() == Qt.CheckState.Unchecked
    # The same place on the blocked row checks nothing: it has no check box.
    blocked_rect = shortlist.visualRect(shortlist.indexFromItem(blocked))
    centre.setY(blocked_rect.center().y())
    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=centre)
    assert blocked.data(Qt.ItemDataRole.CheckStateRole) is None
    shortlist.setFocus()
    shortlist.setCurrentItem(plain)
    QTest.keyClick(shortlist, Qt.Key.Key_Space)
    assert plain.checkState() == Qt.CheckState.Checked
    shortlist.setCurrentItem(blocked)
    QTest.keyClick(shortlist, Qt.Key.Key_Space)
    assert blocked.data(Qt.ItemDataRole.CheckStateRole) is None
    assert "blocked" not in window._checked_ids()
    window.cancel()
    await finish(window)


async def test_continue_is_primary_and_follows_the_limit(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.limit = 2
    await reviewing(window)
    button = window.review_button
    assert button.property("variant") == "primary" and not button.autoDefault()
    assert len(window._checked_ids()) == 3 and not button.isEnabled()  # Above the limit.
    item(window, "declined").setCheckState(Qt.CheckState.Unchecked)
    assert button.isEnabled() and button.text() == "Co&ntinue with 2 selected messages"
    for key in ("plain", "outside"):
        item(window, key).setCheckState(Qt.CheckState.Unchecked)
    assert not button.isEnabled()  # Nothing selected.
    item(window, "outside").setCheckState(Qt.CheckState.Checked)
    button.click()
    await settle()
    assert backend.selected == ("outside",)  # The same IDs come back.
    window.decline_button.click()
    await finish(window)


async def test_decline_has_focus_when_the_consent_step_opens(window: MainWindow) -> None:
    await reviewing(window)
    window.review_button.click()
    await settle()
    assert window.focusWidget() is window.decline_button
    assert [b.property("variant") for b in (window.approve_button, window.decline_button)] == [
        "outline",
        "outline",
    ]
    approve_x = window.approve_button.mapTo(window, window.approve_button.rect().topLeft()).x()
    decline_x = window.decline_button.mapTo(window, window.decline_button.rect().topLeft()).x()
    assert approve_x < decline_x  # Side by side: Approve, then Decline.
    window.decline_button.click()
    await finish(window)


async def test_automatic_runs_never_show_the_run_page(
    window: MainWindow, backend: FakeBackend
) -> None:
    hold = asyncio.Event()
    backend.automatic_hold = hold
    backend.automatic_result = BriefRunResult(
        status=BriefStatus.READY_FOR_REVIEW, sync=backend.sync
    )
    window.start(window._automatic_refresh)
    await settle()
    assert window.workspace.current_page() == "today"
    assert not window.review_panel.isVisible() and not window.consent_panel.isVisible()
    hold.set()
    await finish(window)
    assert window.workspace.current_page() == "today"
