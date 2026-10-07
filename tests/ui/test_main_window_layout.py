"""MainWindow hosts the three-pane workspace: its layout, pages, header, footer and the
detail pane's buttons.

With MAILBRIEF_UI_SHOTS set to a folder, the window is also saved there as
``window-desktop-{mode}-{dpr}x.png`` for the screenshot review loop.
"""

import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QSize, QUrl
from PySide6.QtGui import QAccessible, QDesktopServices, QGuiApplication, QKeySequence, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QScrollArea, QStyle
from pytestqt.exceptions import TimeoutError as QtTimeoutError
from pytestqt.qtbot import QtBot

from mailbrief.domain.actions import ActionFilter
from mailbrief.domain.digests import SyncStatus
from mailbrief.ports.errors import AuthenticationRequiredError
from mailbrief.ui.actions_view import ActionsPanel
from mailbrief.ui.drafts_view import DraftsPanel
from mailbrief.ui.hairline import HairlineDivider, HairlineFrame
from mailbrief.ui.history_view import NEEDS_CONNECTION
from mailbrief.ui.main_window import MainWindow
from mailbrief.ui.theme import SIDEBAR_WIDTH, TITLE_PX, ThemeMode, apply_theme
from tests.factories import make_action
from tests.ui.brief_view import detail, select_item
from tests.ui.test_workflow import FakeBackend
from tests.ui.workspace_fixtures import ACCOUNT, FINANCE_ACTION, mockup_digest

BUSY = "MailBrief is busy; try again in a moment."
NOW = datetime(2026, 10, 6, 13, 30, tzinfo=UTC)


def mockup_backend() -> FakeBackend:
    backend = FakeBackend()
    brief = mockup_digest()
    backend.saved = brief.digest
    backend.links = {key: tuple(links) for key, links in brief.links.items()}
    backend.proposals = {key: tuple(found) for key, found in brief.proposals.items()}
    backend.actions = {
        ActionFilter.OPEN: (make_action(title="One"), make_action(title="Two")),
        ActionFilter.WAITING: (make_action(title="Three"),),
    }
    return backend


async def finish(window: MainWindow) -> None:
    assert window.task is not None
    await window.task


async def settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


def show(qtbot: QtBot, window: MainWindow) -> None:
    """Show ``window`` and wait until it is on screen (waitExposed is a context manager)."""
    try:
        with qtbot.waitExposed(window, timeout=2000):
            window.show()
    except QtTimeoutError:  # Some platforms never report exposure; go on once shown.
        QApplication.processEvents()
    QApplication.processEvents()


@pytest.fixture
def backend() -> FakeBackend:
    return mockup_backend()


@pytest.fixture
async def window(qtbot: QtBot, backend: FakeBackend) -> MainWindow:
    result = MainWindow(backend)
    result.now = lambda: NOW
    qtbot.addWidget(result)
    result.start(result.initialize)
    await finish(result)
    return result


def button(window: MainWindow, text: str) -> QPushButton:
    content = detail(window).widget()
    assert content is not None
    return next(b for b in content.findChildren(QPushButton) if b.text() == text)


async def test_the_central_widget_is_the_workspace_a_hairline_and_the_status_strip(
    window: MainWindow,
) -> None:
    central = window.centralWidget()
    assert central is not None and not isinstance(central, QScrollArea)
    layout = central.layout()
    assert layout is not None and layout.count() == 3
    items = [layout.itemAt(index) for index in range(3)]
    widgets = [None if item is None else item.widget() for item in items]
    assert widgets[0] is window.workspace
    assert isinstance(widgets[1], HairlineDivider)
    strip = widgets[2]
    assert strip is not None and strip.objectName() == "statusStrip"
    assert window.status.parent() is strip and window.undo_button.parent() is strip
    # The window asks for 1100 × 720; a small screen may give it less (CI's Mac: about
    # 1024 × 649), so compare within the screen's available area and the window's maximum.
    room = QGuiApplication.primaryScreen().availableGeometry().size()
    room = room.boundedTo(window.maximumSize())
    assert window.size().boundedTo(room) == QSize(1100, 720).boundedTo(room)
    header = window.workspace.header
    assert window.generate_button.parent() is header
    assert window.cancel_button.parent() is header
    sidebar = window.workspace.sidebar
    assert window.settings_button is sidebar.settings
    assert window.cached_button is sidebar.saved_mail
    assert window.data_button is sidebar.data
    footer = [sidebar.footer.itemAt(index) for index in range(sidebar.footer.count())]
    assert [None if item is None else item.widget() for item in footer] == [
        window.connection,
        window.ai,
        window.connect_button,
        window.disconnect_button,
    ]
    assert not hasattr(window, "digest") and not hasattr(window, "briefs_button")


async def test_the_footer_buttons_fit_and_a_long_status_never_widens_the_window(
    qtbot: QtBot, qapp: QApplication, themed: None
) -> None:
    apply_theme(qapp, ThemeMode.DARK)
    window = MainWindow(FakeBackend())
    qtbot.addWidget(window)
    sidebar = window.workspace.sidebar
    room = sidebar.contentsRect().width()
    for footer_button in (window.connect_button, window.disconnect_button):
        assert footer_button.sizeHint().width() <= room
    assert window.connect_button.accessibleName() == "Connect Gmail"
    narrow = window.minimumSizeHint().width()
    assert narrow <= 900
    window.status.setText("A status that keeps going " * 20)
    window.workspace.header.set_status("Checked Gmail at 09:14 " * 20)
    assert window.minimumSizeHint().width() == narrow


@pytest.mark.parametrize(("key", "view"), [("actions", "open"), ("waiting", "waiting")])
async def test_the_sidebar_opens_the_actions_tabs(window: MainWindow, key: str, view: str) -> None:
    window.workspace.sidebar.page_requested.emit(key)
    assert window.workspace.current_page() == "actions"
    assert window.actions_panel.view() is ActionFilter(view)
    assert window.workspace.sidebar.current_key() == key


async def test_changing_the_actions_tab_moves_the_sidebar(window: MainWindow) -> None:
    window.workspace.sidebar.page_requested.emit("actions")
    window.actions_panel.show_view(ActionFilter.WAITING)
    assert window.workspace.sidebar.current_key() == "waiting"
    window.actions_panel.show_view(ActionFilter.COMPLETED)
    assert window.workspace.sidebar.current_key() == "actions"


async def test_the_sidebar_opens_drafts_and_returns_to_today(window: MainWindow) -> None:
    window.workspace.sidebar.page_requested.emit("drafts")
    assert window.workspace.current_page() == "drafts"
    window.workspace.sidebar.page_requested.emit("today")
    assert window.workspace.current_page() == "today"
    assert window.workspace.sidebar.current_key() == "today"


async def test_briefs_loads_then_shows_its_page(window: MainWindow, backend: FakeBackend) -> None:
    window.workspace.sidebar.page_requested.emit("drafts")
    window.workspace.sidebar.page_requested.emit("briefs")
    # Until it has loaded, the sidebar stays on the page shown.
    assert window.workspace.sidebar.current_key() == "drafts"
    assert window.task is not None
    await finish(window)
    assert window.workspace.current_page() == "briefs"
    assert window.workspace.sidebar.current_key() == "briefs"
    assert window.history_panel.saved.count() == len(await backend.list_briefs())
    assert window.status.text() == "Open a saved brief, or brief a missed day."


async def test_opening_a_saved_brief_lands_on_today_with_the_banner(
    qtbot: QtBot, window: MainWindow, backend: FakeBackend
) -> None:
    show(qtbot, window)
    past = backend.saved.model_copy(update={"local_date": backend.saved.local_date.replace(day=5)})
    backend.briefs[(past.account_id, past.local_date)] = past
    window.history_panel.open_requested.emit(past.account_id, past.local_date)
    await finish(window)
    assert window.workspace.current_page() == "today"
    assert window.workspace.sidebar.current_key() == "today"
    assert window.viewing.isVisible()


async def test_briefing_a_day_without_gmail_starts_nothing(
    window: MainWindow, backend: FakeBackend
) -> None:
    window.start(window._disconnect)
    await finish(window)
    done = window.task
    window.history_panel.generate_requested.emit(backend.saved.local_date.replace(day=5))
    assert window.history_panel.status.text() == NEEDS_CONNECTION
    assert window.task is done and backend.generated_days == []


async def test_briefs_while_busy_says_so(window: MainWindow) -> None:
    release = asyncio.Event()

    async def hold() -> None:
        await release.wait()

    window.start(hold)
    await asyncio.sleep(0)
    window.workspace.sidebar.page_requested.emit("briefs")
    assert window.status.text() == BUSY
    assert window.workspace.current_page() == "today"
    assert window.workspace.sidebar.current_key() == "today"
    release.set()
    await finish(window)


async def test_review_and_consent_use_the_run_page_then_return_to_today(
    qtbot: QtBot, window: MainWindow, backend: FakeBackend
) -> None:
    show(qtbot, window)
    window.workspace.sidebar.page_requested.emit("drafts")
    window.start(window._generate)
    await settle()
    assert window.workspace.current_page() == "run"
    assert window.workspace.sidebar.current_key() == "today"
    assert window.review_panel.isVisible()
    window.review_button.click()
    await settle()
    assert window.workspace.current_page() == "run"
    assert window.consent_panel.isVisible()
    window.approve_button.click()
    await finish(window)
    assert window.workspace.current_page() == "today"
    assert not window.review_panel.isVisible() and not window.consent_panel.isVisible()


async def test_a_past_brief_shows_the_banner_on_today(
    qtbot: QtBot, window: MainWindow, backend: FakeBackend
) -> None:
    show(qtbot, window)
    past = backend.saved.model_copy(update={"local_date": backend.saved.local_date.replace(day=5)})
    backend.briefs[(past.account_id, past.local_date)] = past
    window.workspace.sidebar.page_requested.emit("drafts")
    window.start(lambda: window._show_brief(past.account_id, past.local_date))
    await finish(window)
    assert window.workspace.current_page() == "today"
    assert window.viewing.isVisible()
    assert window.viewing_label.text() == "Viewing the brief for 2026-10-05."


async def test_counts_follow_each_refresh_and_survive_a_failure(
    window: MainWindow, backend: FakeBackend
) -> None:
    sidebar = window.workspace.sidebar
    assert (sidebar.count_text("actions"), sidebar.count_text("waiting")) == ("2", "1")
    assert sidebar.count_text("drafts") == str(window.drafts_panel.list.count())
    backend.actions[ActionFilter.WAITING] = ()
    window.start(window._refresh_actions)
    await finish(window)
    assert (sidebar.count_text("actions"), sidebar.count_text("waiting")) == ("2", "0")
    backend.list_fail = RuntimeError("private")
    window.start(window._refresh_actions)
    await finish(window)
    assert (sidebar.count_text("actions"), sidebar.count_text("waiting")) == ("2", "0")


@pytest.mark.parametrize(
    ("status", "stamped"), [(SyncStatus.COMPLETE, True), (SyncStatus.FAILED, False)]
)
async def test_the_header_says_when_gmail_was_checked(
    window: MainWindow, backend: FakeBackend, status: SyncStatus, stamped: bool
) -> None:
    window.zone = ZoneInfo("America/Toronto")
    updates: dict[str, object] = {"status": status}
    if status is SyncStatus.FAILED:
        updates["error_code"] = "PROVIDER_ERROR"
    backend.sync = backend.sync.model_copy(update=updates)
    assert window.workspace.header.status.text() == ""
    window.start(window._generate)
    await settle()
    window.review_button.click()
    await settle()
    window.approve_button.click()
    await finish(window)
    expected = "Checked Gmail at 09:30" if stamped else ""
    assert window.workspace.header.status.text() == expected


async def test_the_header_gives_the_time_the_sync_finished(window: MainWindow) -> None:
    """A review left open from 09:00 to 09:40 still checked Gmail at 09:00."""
    window.zone = ZoneInfo("UTC")
    clock = [datetime(2026, 10, 6, 9, 0, tzinfo=UTC)]
    window.now = lambda: clock[0]
    window.start(window._generate)
    await settle()
    assert not window.review_panel.isHidden()  # The review opened at 09:00.
    clock[0] = datetime(2026, 10, 6, 9, 40, tzinfo=UTC)
    window.review_button.click()
    await settle()
    window.approve_button.click()
    await finish(window)
    assert window.workspace.header.status.text() == "Checked Gmail at 09:00"


def last_call(backend: FakeBackend) -> tuple[object, ...]:
    return backend.action_calls[-1]


async def click(window: MainWindow, target: QPushButton) -> None:
    target.click()
    await finish(window)


async def test_each_detail_button_reaches_the_backend(
    window: MainWindow, backend: FakeBackend
) -> None:
    await click(window, button(window, "Accept"))
    assert last_call(backend) == ("accept_suggestion", 11)
    await click(window, button(window, "Dismiss"))
    assert last_call(backend) == ("dismiss_suggestion", 11)
    into = next(
        b
        for b in detail(window).findChildren(QPushButton)
        if b.accessibleName() == "Add to “Finance review prep”"
    )
    await click(window, into)
    assert last_call(backend) == ("accept_into", 11, FINANCE_ACTION, 3)
    select_item(window, "marco")
    card = detail(window).findChild(HairlineFrame, "proposalCard")
    assert card is not None
    await click(window, card.findChildren(QPushButton)[0])
    assert last_call(backend) == ("apply_proposal", 21, 2)
    select_item(window, "marco")
    await click(window, button(window, "Draft a reply"))
    assert backend.draft_calls[-1] == ("create_reply_draft", ACCOUNT, "marco")


async def test_open_in_gmail_opens_only_gmail_links(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = Mock(return_value=True)
    monkeypatch.setattr(QDesktopServices, "openUrl", opened)
    button(window, "Open in Gmail").click()
    assert opened.call_count == 1
    assert opened.call_args.args[0] == QUrl("https://mail.google.com/mail/u/0/#inbox/priya")
    detail(window).source_requested.emit("https://evil.example/mail.google.com")
    assert opened.call_count == 1


async def test_the_same_email_stays_selected_after_accept(
    qtbot: QtBot, backend: FakeBackend
) -> None:
    # The suggestions move to a later email, so keeping the selection is visible.
    items = list(backend.saved.items)
    items[2] = items[2].model_copy(update={"suggestions": items[0].suggestions})
    items[0] = items[0].model_copy(update={"suggestions": ()})
    backend.saved = backend.saved.model_copy(update={"items": tuple(items)})
    window = MainWindow(backend)
    qtbot.addWidget(window)
    window.start(window.initialize)
    await finish(window)
    select_item(window, "sam")
    await click(window, button(window, "Accept"))
    assert last_call(backend) == ("accept_suggestion", 11)
    assert window.workspace.brief_list.selected_key() == "sam"


@pytest.mark.parametrize("mode", list(ThemeMode))
async def test_window_renders(
    qtbot: QtBot, qapp: QApplication, themed: None, mode: ThemeMode
) -> None:
    apply_theme(qapp, mode)
    window = MainWindow(mockup_backend())
    window.now = lambda: NOW
    qtbot.addWidget(window)
    window.start(window.initialize)
    await finish(window)
    window._stamp_checked()
    window.resize(1100, 720)
    show(qtbot, window)
    image = window.grab()
    assert not image.isNull()
    save_shot(image, f"window-desktop-{mode.value}")


def save_shot(image: QPixmap, name: str) -> None:
    """Save ``image`` as ``{name}-{dpr}x.png`` in MAILBRIEF_UI_SHOTS, when it is set."""
    folder = os.environ.get("MAILBRIEF_UI_SHOTS")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        assert image.save(str(Path(folder) / f"{name}-{image.devicePixelRatio():g}x.png"))


@pytest.mark.parametrize(
    ("panel_type", "title"),
    [(ActionsPanel, "Your actions"), (DraftsPanel, "Your drafts and notes")],
)
def test_page_headings_use_the_title_size(
    qtbot: QtBot,
    qapp: QApplication,
    themed: None,
    panel_type: type[ActionsPanel] | type[DraftsPanel],
    title: str,
) -> None:
    apply_theme(qapp)
    panel = panel_type()
    qtbot.addWidget(panel)
    heading = next(label for label in panel.findChildren(QLabel) if label.text() == title)
    assert heading.font().pixelSize() == TITLE_PX
    for page_button in panel.findChildren(QPushButton):
        assert page_button.property("variant") == "outline"
        assert not page_button.autoDefault()


async def test_mnemonics_are_never_underlined(
    qtbot: QtBot, qapp: QApplication, themed: None
) -> None:
    apply_theme(qapp)
    window = MainWindow(FakeBackend())
    qtbot.addWidget(window)
    assert qapp.style().styleHint(QStyle.StyleHint.SH_UnderlineShortcut) == 0
    assert window.generate_button.text() == "&Sync and review"


@pytest.mark.skipif(sys.platform == "darwin", reason="Qt turns mnemonics off on macOS")
async def test_mnemonics_still_press_buttons(
    qtbot: QtBot, qapp: QApplication, themed: None
) -> None:
    apply_theme(qapp)
    window = MainWindow(FakeBackend())
    qtbot.addWidget(window)
    assert window.generate_button.shortcut() == QKeySequence("Alt+S")


async def test_cancel_shows_only_while_busy(window: MainWindow) -> None:
    assert window.cancel_button.isHidden()
    release = asyncio.Event()

    async def hold() -> None:
        await release.wait()

    window.start(hold)
    assert not window.cancel_button.isHidden()
    release.set()
    await finish(window)
    assert window.cancel_button.isHidden()


def accounts(window: MainWindow) -> tuple[bool, bool]:
    """Whether Connect and Disconnect are shown."""
    return (not window.connect_button.isHidden(), not window.disconnect_button.isHidden())


async def test_connect_and_disconnect_follow_the_account(
    window: MainWindow, backend: FakeBackend
) -> None:
    assert accounts(window) == (False, True)  # Connected silently on startup.
    window.start(window._disconnect)
    await finish(window)
    assert accounts(window) == (True, False)
    window.start(window._connect)
    await finish(window)
    assert accounts(window) == (False, True)
    backend.fail = AuthenticationRequiredError("expired")
    window.start(window._generate)
    await finish(window)
    assert accounts(window) == (True, False)


async def test_startup_without_a_session_offers_connect(qtbot: QtBot) -> None:
    backend = FakeBackend()
    backend.connect_fail = AuthenticationRequiredError("none")
    window = MainWindow(backend)
    qtbot.addWidget(window)
    assert accounts(window) == (True, False)
    window.start(window.initialize)
    await finish(window)
    assert accounts(window) == (True, False)


async def test_briefs_waits_for_local_storage(qtbot: QtBot) -> None:
    window = MainWindow(FakeBackend())
    qtbot.addWidget(window)
    window.workspace.sidebar.page_requested.emit("briefs")
    assert window.task is None
    assert window.status.text() == (
        "Saved briefs open once local storage loads. Choose Retry loading saved data."
    )
    assert window.workspace.sidebar.current_key() == "today"


async def test_undo_is_read_by_its_text(window: MainWindow) -> None:
    async def nothing() -> None:
        pass

    window._offer_undo("Undo accept", nothing)
    assert window.undo_button.accessibleName() == ""  # Qt derives it from the text.
    accessible = QAccessible.queryAccessibleInterface(window.undo_button)
    assert accessible is not None
    assert accessible.text(QAccessible.Text.Name) == "Undo accept"


@pytest.mark.parametrize("mode", list(ThemeMode))
async def test_pages_render(
    qtbot: QtBot, qapp: QApplication, themed: None, mode: ThemeMode
) -> None:
    apply_theme(qapp, mode)
    backend = mockup_backend()
    backend.candidates = ("message-1", "message-2", "message-3")
    window = MainWindow(backend)
    window.now = lambda: NOW
    qtbot.addWidget(window)
    window.start(window.initialize)
    await finish(window)
    window.resize(1100, 720)
    show(qtbot, window)
    for key in ("actions", "drafts"):
        window._show_page(key)
        QApplication.processEvents()
        image = window.grab()
        assert not image.isNull()
        save_shot(image, f"page-{key}-{mode.value}")
    window.workspace.sidebar.page_requested.emit("briefs")  # Loads, then shows the page.
    await finish(window)
    assert window.workspace.current_page() == "briefs"
    QApplication.processEvents()
    save_shot(window.grab(), f"page-briefs-{mode.value}")
    window.start(window._generate)  # The review shows itself on the run page.
    await settle()
    assert window.workspace.current_page() == "run"
    assert window.shortlist.count() == 3
    QApplication.processEvents()
    save_shot(window.grab(), f"page-run-{mode.value}")
    window.review_button.click()  # Step 2: the consent.
    await settle()
    assert window.consent_panel.isVisible()
    QApplication.processEvents()
    save_shot(window.grab(), f"page-consent-{mode.value}")
    window.decline_button.click()
    await finish(window)


async def test_long_account_and_status_wrap_inside_the_window(
    qtbot: QtBot, qapp: QApplication, themed: None
) -> None:
    apply_theme(qapp)
    window = MainWindow(mockup_backend())
    window.now = lambda: NOW
    qtbot.addWidget(window)
    window.start(window.initialize)
    await finish(window)
    window.resize(1100, 720)
    show(qtbot, window)
    narrow = window.minimumSizeHint().width()
    email = f"{'z' * 48}@example.com"
    assert len(email) == 60
    window._set_account(email)
    window.connection.setText(f"Gmail: connected as {email}")
    window.status.setText("s" * 200)
    lines = (window.connection, window.ai, window.status)
    # A new height reaches each enclosing layout in turn, over a few event-loop passes.
    qtbot.waitUntil(
        lambda: all(line.height() >= line.heightForWidth(line.width()) for line in lines),
        timeout=2000,
    )  # Nothing is clipped.
    assert window.minimumSizeHint().width() == narrow
    assert window.workspace.sidebar.width() == SIDEBAR_WIDTH
    assert window.connection.contentsMargins().left() == 10
