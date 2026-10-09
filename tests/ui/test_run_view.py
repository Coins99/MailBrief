"""The guided run page: step headings, shortlist rows, the primary Continue and consent.

The gates themselves are unchanged: the same IDs come back, a blocked row is never
checkable, the limit holds, Decline starts focused, and automatic runs never show the page.
The shortlist fits its rows, never scrolls sideways and paints a visible check box. Every
test here runs with the theme applied.
"""

import asyncio

import pytest
from PySide6.QtCore import QModelIndex, QPoint, QRect, Qt
from PySide6.QtGui import QFontMetrics
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
from mailbrief.ui.brief_list import Chip, ChipTone, row_height
from mailbrief.ui.main_window import MainWindow
from mailbrief.ui.run_view import (
    EXCLUDED,
    LEFT_OUT,
    NONE,
    ROW_ROLE,
    TRACKED_REPLY,
    ShortlistDelegate,
    ShortlistRow,
    cut_address,
    indicator_colors,
    sender_line,
    shortlist_row,
)
from mailbrief.ui.theme import DARK, LIGHT, TEXT_PX, ThemeMode, Tokens, apply_theme, ui_font
from tests.factories import make_message
from tests.ui.test_main_window_layout import save_shot
from tests.ui.test_workflow import FakeBackend
from tests.ui.window_wait import settle


def sender_metrics() -> QFontMetrics:
    """The sender line's metrics, as the delegate measures them. Needs the application and
    the theme's Inter: a test calling this takes ``qapp`` and ``themed`` and applies it."""
    return QFontMetrics(ui_font(TEXT_PX))


LONG_NAME = ("Priya Shah, Finance " * 10)[:200]
# Elision measures fractional widths and horizontalAdvance rounds, so a line built to fit
# a width measured from a string can need a pixel or two more (Windows needs it).
SLACK = 4
LOOKALIKE = "billing@paypal.com.secure-login.evil.example"


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
    assert (rows["plain"].name, rows["plain"].address) == ("Alex", "alex@example.com")
    # The item's own text, the one read aloud, is unchanged.
    assert item(window, "outside").text().endswith(OUTSIDE_REPLY_TEXT)
    assert item(window, "declined").text().endswith(DECLINED_TEXT)
    assert item(window, "blocked").text().endswith("excluded in Settings")
    # Shortlist rows are as tall as the brief list's: with chips and without.
    assert delegate(window).sizeHint(*row_option(window, "outside")).height() == row_height(True)
    assert delegate(window).sizeHint(*row_option(window, "plain")).height() == row_height(False)
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
def test_a_review_row_never_hides_the_sender_s_address(
    qapp: QApplication, themed: None, sender: EmailContact, shown: str
) -> None:
    apply_theme(qapp, ThemeMode.DARK)
    ranked = RankedMessage(message=make_message(sender=sender), score=50, reasons=())
    row = shortlist_row(ranked, blocked=False, outside=False, declined=False)
    assert sender_line(sender_metrics(), row.name, row.address, 10_000) == shown


def sender_width(window: MainWindow, key: str) -> int:
    """The width the review step really gives ``key``'s sender line."""
    return delegate(window).text_width(*row_option(window, key))


async def test_a_long_display_name_never_pushes_the_address_out(window: MainWindow) -> None:
    await reviewing(window)
    metrics, width = sender_metrics(), sender_width(window, "plain")
    line = sender_line(metrics, LONG_NAME, "attacker@evil.example", width)
    assert line.endswith("… <attacker@evil.example>")  # The name gave way, not the address.
    assert line.startswith("Priya Shah") and metrics.horizontalAdvance(line) <= width
    # A short name fits whole.
    assert sender_line(metrics, "Alex", "alex@example.com", width) == "Alex <alex@example.com>"
    window.cancel()
    await finish(window)


@pytest.mark.parametrize("name", ["Priya Shah", None])
def test_a_narrow_row_cuts_the_address_in_the_middle(
    qapp: QApplication, themed: None, name: str | None
) -> None:
    apply_theme(qapp, ThemeMode.DARK)
    metrics = sender_metrics()
    address = "attacker@evil.example"
    # Room for the whole domain: the part before the "@" gives way.
    width = metrics.horizontalAdvance("at…@evil.example") + SLACK
    line = sender_line(metrics, name, address, width)
    assert line.endswith("…@evil.example") and metrics.horizontalAdvance(line) <= width
    # Less: the domain's start gives way, never its end.
    width = metrics.horizontalAdvance("…@…example") + SLACK
    line = sender_line(metrics, name, address, width)
    assert line.startswith("…@…") and line.endswith("example")
    assert "Priya" not in line and metrics.horizontalAdvance(line) <= width


@pytest.mark.parametrize("name", [LONG_NAME, None])
def test_every_sender_line_fits_and_keeps_its_at(
    qapp: QApplication, themed: None, name: str | None
) -> None:
    apply_theme(qapp, ThemeMode.DARK)
    metrics = sender_metrics()
    for address in ("attacker@evil.example", LOOKALIKE):
        for width in range(40, 401):
            line = sender_line(metrics, name, address, width)
            assert metrics.horizontalAdvance(line) <= width, (width, line)
            assert "@" in line, (width, line)


def test_a_cut_address_keeps_the_end_of_its_domain(qapp: QApplication, themed: None) -> None:
    """Trust is read from the right of a domain, so only its start is ever cut."""
    apply_theme(qapp, ThemeMode.DARK)
    metrics = sender_metrics()
    domain = LOOKALIKE.split("@")[1]
    # The narrowest line that can end in the whole last label: below it, only "…@…" and
    # the domain's last letters fit (at 40 px, "…@…").
    whole_label = metrics.horizontalAdvance("…@…example") + SLACK
    for width in range(40, 401):
        line = cut_address(metrics, LOOKALIKE, width)
        assert metrics.horizontalAdvance(line) <= width, (width, line)
        assert "@" in line, (width, line)
        shown = line.split("@", 1)[1]
        # The domain is whole, or its end: never a lookalike start without its real end.
        assert shown == domain or domain.endswith(shown.removeprefix("…")), (width, line)
        assert not shown.startswith("pay") or shown == domain, (width, line)
        if width >= whole_label:
            assert line.endswith("example"), (width, line)
    assert cut_address(metrics, LOOKALIKE, 10_000) == LOOKALIKE


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
