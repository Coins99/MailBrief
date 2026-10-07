"""The keyboard: Ctrl+1 to Ctrl+5 (Cmd on macOS) show the sidebar's pages, and Tab runs
from Sync and review through the sidebar and the page to Undo, then back to the start.

The theme is applied first: under Fusion, buttons take focus by Tab on every platform.
"""

import asyncio
from datetime import date
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QWidget
from pytestqt.qtbot import QtBot

from mailbrief.domain.actions import ActionFilter
from mailbrief.domain.digests import DailyDigest, SavedBriefSummary
from mailbrief.ui.main_window import PAGE_SHORTCUTS, MainWindow
from mailbrief.ui.theme import apply_theme
from tests.ui.test_main_window_layout import BUSY, NOW, finish, mockup_backend, show
from tests.ui.test_workflow import FakeBackend

# Tab from Sync and review on Today, with the mockup brief's first email shown.
HEADER_AND_SIDEBAR = [
    "generateButton",
    "sidebarNav",
    "savedMailButton",
    "dataButton",
    "settingsButton",
    "disconnectButton",
]
FIRST_EMAIL = [
    "briefList",
    "acceptButton",
    "dismissButton",
    "addToButton",
    "acceptButton",
    "dismissButton",
    "addToButton",
    "replyButton",
    "openInGmailButton",
]


def activate(qtbot: QtBot, window: MainWindow) -> None:
    """Shortcuts and focus need the window shown and active; this waits until it is, and
    fails with a timeout when macOS won't activate it (a locked screen, for one)."""
    with qtbot.waitActive(window, timeout=5000):
        show(qtbot, window)
        window.raise_()
        window.activateWindow()


@pytest.fixture
async def window(qtbot: QtBot, qapp: QApplication, themed: None, tmp_path: Path) -> MainWindow:
    apply_theme(qapp)
    # A database path enables Data, so it joins the Tab order.
    result = MainWindow(mockup_backend(), database_path=tmp_path / "mailbrief.db")
    result.now = lambda: NOW
    qtbot.addWidget(result)
    result.start(result.initialize)
    await finish(result)
    result.resize(1100, 720)
    activate(qtbot, result)
    return result


def press(window: MainWindow, number: int) -> None:
    QTest.keySequence(window, QKeySequence(f"Ctrl+{number}"))


def tab_chain(window: MainWindow, start: QWidget) -> list[str]:
    """The objectNames Tab visits from ``start`` until it is back there."""
    start.setFocus()
    names: list[str] = []
    for _ in range(60):
        widget = QApplication.focusWidget()
        assert widget is not None
        names.append(widget.objectName())
        window.focusNextChild()
        if QApplication.focusWidget() is start:
            return names
    raise AssertionError(f"Tab never came back to the start: {names}")


def detail_buttons(window: MainWindow) -> list[QPushButton]:
    return window.workspace.detail.buttons()


async def hold(window: MainWindow) -> asyncio.Event:
    """Keep the window busy until the returned event is set."""
    release = asyncio.Event()

    async def wait() -> None:
        await release.wait()

    window.start(wait)
    await asyncio.sleep(0)
    return release


@pytest.mark.parametrize(
    ("number", "page", "sidebar"),
    [
        (1, "today", "today"),
        (2, "actions", "actions"),
        (3, "actions", "waiting"),
        (4, "drafts", "drafts"),
        (5, "briefs", "briefs"),
    ],
)
async def test_each_shortcut_shows_its_page(
    window: MainWindow, number: int, page: str, sidebar: str
) -> None:
    assert PAGE_SHORTCUTS[number - 1] == sidebar
    assert window.page_shortcuts[sidebar].key() == QKeySequence(f"Ctrl+{number}")
    window._show_page("drafts" if page != "drafts" else "today")
    press(window, number)
    if sidebar == "briefs":
        await finish(window)  # Briefs loads, then shows its page.
    assert window.workspace.current_page() == page
    assert window.workspace.sidebar.current_key() == sidebar
    if page == "actions":
        expected = ActionFilter.WAITING if sidebar == "waiting" else ActionFilter.OPEN
        assert window.actions_panel.view() is expected


async def test_today_shows_the_waiting_review(window: MainWindow) -> None:
    window._show_page("drafts")
    window._review = asyncio.get_running_loop().create_future()
    press(window, 1)
    assert window.workspace.current_page() == "run"
    assert window.workspace.sidebar.current_key() == "today"
    window._review.cancel()


async def test_the_briefs_shortcut_refuses_while_busy(window: MainWindow) -> None:
    release = await hold(window)
    press(window, 5)
    assert window.status.text() == BUSY
    assert window.workspace.current_page() == "today"
    assert window.workspace.sidebar.current_key() == "today"
    release.set()
    await finish(window)


async def test_the_briefs_shortcut_waits_for_local_storage(
    qtbot: QtBot, qapp: QApplication, themed: None
) -> None:
    apply_theme(qapp)
    window = MainWindow(FakeBackend())
    qtbot.addWidget(window)
    activate(qtbot, window)
    press(window, 5)
    assert window.task is None
    assert window.status.text() == (
        "Saved briefs open once local storage loads. Choose Retry loading saved data."
    )
    assert window.workspace.current_page() == "today"


async def test_a_page_that_loads_late_never_overrides_where_the_owner_went(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = asyncio.Event()
    load = window.backend.list_briefs

    async def slow() -> tuple[SavedBriefSummary, ...]:
        await release.wait()
        return await load()

    monkeypatch.setattr(window.backend, "list_briefs", slow)
    press(window, 5)  # Briefs starts loading…
    await asyncio.sleep(0)
    assert window.task is not None and not window.task.done()
    press(window, 4)  # …and the owner moves to Drafts before it has.
    assert window.workspace.current_page() == "drafts"
    release.set()
    await finish(window)
    assert window.workspace.current_page() == "drafts"
    assert window.workspace.sidebar.current_key() == "drafts"


async def test_a_brief_that_opens_late_leaves_the_owner_where_they_went(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    press(window, 5)
    await finish(window)
    release = asyncio.Event()
    load = window.backend.load_brief

    async def slow(account_email: str, local_date: date) -> DailyDigest | None:
        await release.wait()
        return await load(account_email, local_date)

    monkeypatch.setattr(window.backend, "load_brief", slow)
    window.history_panel.saved.setCurrentRow(0)
    window.history_panel.open_button.click()
    await asyncio.sleep(0)
    press(window, 4)
    release.set()
    await finish(window)
    assert window.workspace.current_page() == "drafts"  # Today has the brief, unshown.


async def test_tab_runs_header_sidebar_brief_then_detail(window: MainWindow) -> None:
    assert tab_chain(window, window.generate_button) == HEADER_AND_SIDEBAR + FIRST_EMAIL
    # The detail's buttons in display order: top to bottom, then left to right.
    places = [
        (button.mapTo(window, QPoint(0, 0)).y(), button.mapTo(window, QPoint(0, 0)).x())
        for button in detail_buttons(window)
    ]
    assert places == sorted(places)


async def test_undo_comes_last_then_the_start(window: MainWindow) -> None:
    async def nothing() -> None:
        pass

    window._offer_undo("Undo accept", nothing)
    QApplication.processEvents()
    chain = tab_chain(window, window.generate_button)
    assert chain == [*HEADER_AND_SIDEBAR, *FIRST_EMAIL, "undoButton"]


async def test_another_email_relinks_its_buttons(window: MainWindow) -> None:
    brief_list = window.workspace.brief_list
    brief_list.setCurrentIndex(brief_list.model().index(4, 0))
    QApplication.processEvents()  # The new buttons show on the next pass.
    names = [button.objectName() for button in detail_buttons(window)]
    assert names and names[0] == "replyButton"
    assert tab_chain(window, window.generate_button) == [*HEADER_AND_SIDEBAR, "briefList", *names]


async def test_connect_takes_disconnects_place(window: MainWindow) -> None:
    window.start(window._disconnect)
    await finish(window)
    chain = tab_chain(window, window.generate_button)
    assert chain[: len(HEADER_AND_SIDEBAR)] == [*HEADER_AND_SIDEBAR[:-1], "connectButton"]


async def test_cancel_follows_sync_and_review_while_busy(window: MainWindow) -> None:
    release = await hold(window)
    assert window.cancel_button.isVisible() and not window.generate_button.isEnabled()
    chain = tab_chain(window, window.cancel_button)
    # Sync and review, Saved mail, Data, Settings and Disconnect wait while busy.
    assert chain == ["cancelButton", "sidebarNav", *FIRST_EMAIL]
    release.set()
    await finish(window)
