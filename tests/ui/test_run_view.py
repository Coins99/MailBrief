"""The guided run page: step headings, shortlist rows, the primary Continue and consent.

The gates themselves are unchanged: the same IDs come back, a blocked row is never
checkable, the limit holds, Decline starts focused, and automatic runs never show the page.
The shortlist fits its rows, never scrolls sideways and paints a visible check box. Every
test here runs with the theme applied.
"""

import asyncio

import pytest
from PySide6.QtCore import QModelIndex, QPoint, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QListWidgetItem,
    QScrollArea,
    QStyleOptionViewItem,
    QWidget,
)
from pytestqt.qtbot import QtBot

from mailbrief.domain.briefs import BriefRunResult, BriefStatus
from mailbrief.domain.messages import EmailContact, RankedMessage
from mailbrief.services.ranking import DECLINED_TEXT, OUTSIDE_REPLY_TEXT
from mailbrief.ui.brief_list import Chip, ChipTone
from mailbrief.ui.main_window import MainWindow
from mailbrief.ui.run_view import (
    EXCLUDED,
    LEFT_OUT,
    NONE,
    ROW_ROLE,
    TRACKED_REPLY,
    ShortlistDelegate,
    ShortlistRow,
    indicator_colors,
    shortlist_row,
)
from mailbrief.ui.theme import DARK, LIGHT, ThemeMode, Tokens, apply_theme
from tests.factories import make_message
from tests.ui.test_main_window_layout import save_shot
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
async def window(
    qtbot: QtBot, qapp: QApplication, themed: None, backend: FakeBackend
) -> MainWindow:
    apply_theme(qapp)
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


def delegate(window: MainWindow) -> ShortlistDelegate:
    found = window.shortlist.itemDelegate()
    assert isinstance(found, ShortlistDelegate)
    return found


def row_option(window: MainWindow, key: str) -> tuple[QStyleOptionViewItem, QModelIndex]:
    """The view's option for ``key``'s row, with its rect, and the row's index."""
    shortlist = window.shortlist
    index = shortlist.indexFromItem(item(window, key))
    option = QStyleOptionViewItem()
    option.initFrom(shortlist.viewport())
    option.rect = shortlist.visualRect(index)
    return option, index


def check_rect(window: MainWindow, key: str) -> QRect:
    """The style's check rect for ``key``'s row, in viewport coordinates."""
    return delegate(window).check_rect(*row_option(window, key))


def check_centre(window: MainWindow, key: str) -> QPoint:
    return check_rect(window, key).center()


def rows_height(window: MainWindow) -> int:
    shortlist = window.shortlist
    return sum(shortlist.sizeHintForRow(number) for number in range(shortlist.count()))


def assert_no_sideways_scrolling(window: MainWindow) -> None:
    bar = window.shortlist.horizontalScrollBar()
    assert not bar.isVisible()
    assert bar.maximum() == 0  # Not even a hidden range a trackpad could scroll.


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
    assert rows["plain"].sender == "Alex <alex@example.com>"  # The name and its address.
    # The item's own text, the one read aloud, is unchanged.
    assert item(window, "outside").text().endswith(OUTSIDE_REPLY_TEXT)
    assert item(window, "declined").text().endswith(DECLINED_TEXT)
    assert item(window, "blocked").text().endswith("excluded in Settings")
    window.cancel()
    await finish(window)


@pytest.mark.parametrize(
    ("sender", "shown"),
    [
        (
            EmailContact(name="Priya Shah <priya@corp.example>", address="attacker@evil.example"),
            "attacker@evil.example",
        ),
        (EmailContact(name="", address="sam@example.com"), "sam@example.com"),
    ],
)
def test_a_review_row_never_hides_the_sender_s_address(sender: EmailContact, shown: str) -> None:
    ranked = RankedMessage(message=make_message(sender=sender), score=50, reasons=())
    row = shortlist_row(ranked, blocked=False, outside=False, declined=False)
    assert row.sender == shown


async def test_clicks_and_space_toggle_only_checkable_rows(window: MainWindow) -> None:
    await reviewing(window)
    shortlist, viewport = window.shortlist, window.shortlist.viewport()
    plain, blocked = item(window, "plain"), item(window, "blocked")
    assert plain.checkState() == Qt.CheckState.Checked
    centre = check_centre(window, "plain")
    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=centre)
    assert plain.checkState() == Qt.CheckState.Unchecked
    # The same place on the blocked row checks nothing: it has no check box.
    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=check_centre(window, "blocked"))
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


async def test_a_short_list_is_as_tall_as_its_rows(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.candidates = ("plain", "outside", "declined")
    await reviewing(window)
    QApplication.processEvents()
    shortlist = window.shortlist
    assert shortlist.count() == 3
    content = rows_height(window) + 2 * shortlist.frameWidth()
    assert shortlist.maximumHeight() == content
    assert shortlist.height() == content  # No empty box below the rows.
    assert not shortlist.verticalScrollBar().isVisible()
    assert shortlist.verticalScrollBar().maximum() == 0
    assert_no_sideways_scrolling(window)
    # Continue follows the list; the height the list can't use goes below it.
    list_bottom = shortlist.mapTo(window, QPoint(0, shortlist.height())).y()
    button_top = window.review_button.mapTo(window, QPoint(0, 0)).y()
    layout = window.review_panel.layout()
    assert layout is not None and button_top - list_bottom == layout.spacing()
    window.cancel()
    await finish(window)


async def test_a_long_list_fills_the_page_and_scrolls(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.candidates = tuple(f"message-{number}" for number in range(20))
    await reviewing(window)
    QApplication.processEvents()
    # Whatever size the screen allowed the window (CI's Mac: about 1024 × 649).
    shortlist = window.shortlist
    assert shortlist.count() == 20
    content = rows_height(window) + 2 * shortlist.frameWidth()
    assert shortlist.height() < content  # Shorter than its rows…
    assert shortlist.verticalScrollBar().isVisible()  # …so the list scrolls…
    page = window.findChild(QScrollArea, "runPage")
    assert page is not None
    assert not page.verticalScrollBar().isVisible()  # …and the page doesn't.
    assert page.verticalScrollBar().maximum() == 0
    button = window.review_button
    button_bottom = button.mapTo(page.viewport(), QPoint(0, button.height())).y()
    assert page.viewport().height() - button_bottom <= 14  # The list fills the page.
    assert_no_sideways_scrolling(window)
    window.resize(window.width() - 100, window.height())  # Narrower than at layout.
    QApplication.processEvents()
    assert_no_sideways_scrolling(window)
    window.cancel()
    await finish(window)


async def test_blocked_rows_line_up_with_checkable_ones(window: MainWindow) -> None:
    await reviewing(window)
    rows = delegate(window)
    plain = rows.text_left(*row_option(window, "plain"))
    assert rows.text_left(*row_option(window, "blocked")) == plain
    assert plain > check_rect(window, "plain").right()  # After the check box.
    window.cancel()
    await finish(window)


@pytest.mark.parametrize("tokens", [DARK, LIGHT])
def test_indicator_colors_follow_the_state_and_theme(tokens: Tokens) -> None:
    assert indicator_colors(False, tokens) == (tokens.border_strong, NONE, NONE)
    assert indicator_colors(True, tokens) == (
        tokens.accent_border,
        tokens.accent_bg,
        tokens.accent_fg,
    )


async def test_the_unchecked_box_is_visible_in_dark(window: MainWindow) -> None:
    await reviewing(window)
    declined = item(window, "declined")
    declined.setCheckState(Qt.CheckState.Unchecked)
    assert window.shortlist.currentItem() is not declined  # Not selected: the plain panel.
    QApplication.processEvents()
    image = window.shortlist.viewport().grab().toImage()
    dpr = image.devicePixelRatio()
    box = check_rect(window, "declined")
    y = round(box.center().y() * dpr)
    # In device pixels: the outline's left edge, and the row inside the box.
    edge = image.pixelColor(round(box.left() * dpr), y)
    background = image.pixelColor(round(box.center().x() * dpr), y)
    assert background.name() == DARK.panel
    assert edge != background
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


@pytest.mark.parametrize("mode", list(ThemeMode))
async def test_review_step_renders(
    qtbot: QtBot, qapp: QApplication, themed: None, mode: ThemeMode
) -> None:
    """A checked row, an unchecked one, a tracked reply and an excluded row."""
    apply_theme(qapp, mode)
    backend = FakeBackend()
    backend.candidates = ("plain", "declined", "outside", "blocked")
    backend.outside = frozenset({"outside"})
    backend.declined = frozenset({"declined"})
    backend.blocked = frozenset({"blocked"})
    window = MainWindow(backend)
    qtbot.addWidget(window)
    window.start(window.initialize)
    await finish(window)
    window.resize(1100, 720)
    window.show()
    QApplication.processEvents()
    await reviewing(window)
    item(window, "declined").setCheckState(Qt.CheckState.Unchecked)  # As a real review would.
    QApplication.processEvents()
    image = window.grab()
    assert not image.isNull()
    save_shot(image, f"run-review-{mode.value}")
    window.cancel()
    await finish(window)
