"""The three-pane workspace renders in both themes at the mockup and desktop sizes.

With MAILBRIEF_UI_SHOTS set to a folder, each render is saved there as
``{size}-{mode}-{dpr}x.png`` for the screenshot review loop (.claude/skills/mailbrief-ui).
"""

import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QLabel, QWidget
from pytestqt.qtbot import QtBot

from mailbrief.domain.digests import DailyDigest, DigestCoverage, DigestItem, DigestStatus
from mailbrief.domain.messages import EmailContact
from mailbrief.ui.brief_detail import DETAIL_MAX_WIDTH
from mailbrief.ui.theme import ThemeMode, apply_theme, current_tokens
from mailbrief.ui.workspace import (
    EMPTY_BRIEF,
    KEY_ROLE,
    ThreePaneWorkspace,
    brief_meta,
    brief_title,
)
from tests.factories import make_digest_item
from tests.ui.window_wait import show
from tests.ui.workspace_fixtures import ZONE, mockup_digest

SIZES = {"mockup": (680, 520), "desktop": (1100, 720)}
OWNER_ZONE = ZoneInfo(ZONE)
TODAY = date(2026, 10, 6)  # The mockup brief's day.


def build(qtbot: QtBot) -> ThreePaneWorkspace:
    workspace = ThreePaneWorkspace()
    qtbot.addWidget(workspace)
    brief = mockup_digest()
    workspace.show_digest(
        brief.digest,
        links=brief.links,
        proposals=brief.proposals,
        owner_zone=OWNER_ZONE,
        today=TODAY,
    )
    workspace.header.set_status("Checked Gmail at 09:14")
    workspace.sidebar.set_counts(5, 2, 3)
    workspace.sidebar.set_note("Gmail connected. AI asks before sending.")
    return workspace


@pytest.mark.parametrize("size", list(SIZES))
@pytest.mark.parametrize("mode", list(ThemeMode))
def test_workspace_renders(
    qtbot: QtBot, qapp: QApplication, themed: None, mode: ThemeMode, size: str
) -> None:
    apply_theme(qapp, mode)
    workspace = build(qtbot)
    workspace.resize(*SIZES[size])
    show(qtbot, workspace)
    image = workspace.grab()
    assert not image.isNull()
    folder = os.environ.get("MAILBRIEF_UI_SHOTS")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        dpr = image.devicePixelRatio()
        assert image.save(str(Path(folder) / f"{size}-{mode.value}-{dpr:g}x.png"))


def test_sidebar_requests_pages_and_settings(qtbot: QtBot) -> None:
    workspace = build(qtbot)
    sidebar = workspace.sidebar
    pages = QSignalSpy(sidebar.page_requested)
    settings = QSignalSpy(sidebar.settings_requested)
    sidebar.set_current("drafts")
    assert pages.count() == 0  # Showing a page doesn't request it.
    sidebar.nav.setCurrentIndex(sidebar.pages.index(1, 0))
    assert pages.at(0) == ["actions"]
    sidebar.nav.setFocus()
    QTest.keyClick(sidebar.nav, Qt.Key.Key_Return)
    assert pages.at(1) == ["actions"]
    sidebar.settings.click()
    assert settings.count() == 1
    assert sidebar.count_text("actions") == "5"
    sidebar.set_counts(None, 2, 3)
    assert sidebar.count_text("actions") is None
    assert sidebar.note.text() == "Gmail connected. AI asks before sending."


def test_selecting_a_row_updates_the_detail(qtbot: QtBot) -> None:
    workspace = build(qtbot)
    assert workspace.detail.title is not None
    assert workspace.detail.title.text() == "Q3 budget: approval needed by Friday"
    workspace.brief_list.setCurrentIndex(workspace.brief_list.model().index(4, 0))
    assert workspace.detail.title.text() == "Invoice 2041"
    assert workspace.coverage.text() == "Inbox on Oct 6, up to 09:14, plus 1 tracked reply"
    assert workspace.coverage.accessibleName().startswith("Covers messages received on 2026-10-06")
    assert workspace.header.status.text() == "Checked Gmail at 09:14"


def test_empty_brief_says_so(qtbot: QtBot) -> None:
    workspace = ThreePaneWorkspace()
    qtbot.addWidget(workspace)
    empty = DailyDigest.model_validate(
        {**mockup_digest().digest.model_dump(), "items": (), "status": DigestStatus.EMPTY}
    )
    workspace.show_digest(empty, owner_zone=OWNER_ZONE, today=TODAY)
    assert workspace.brief_list.model().rowCount() == 0
    assert workspace.detail.title is None
    texts = [label.text() for label in workspace.detail.findChildren(QLabel)]
    assert EMPTY_BRIEF in texts


def test_sidebar_rows_are_read_with_their_counts(qtbot: QtBot) -> None:
    workspace = build(qtbot)  # Counts 5, 2 and 3.
    sidebar = workspace.sidebar

    def read(key: str) -> str:
        row = next(
            number
            for number in range(sidebar.pages.rowCount())
            if sidebar.pages.item(number).data(KEY_ROLE) == key
        )
        return str(sidebar.pages.item(row).data(Qt.ItemDataRole.AccessibleTextRole))

    assert [read(key) for key in ("today", "actions", "waiting", "drafts", "briefs")] == [
        "Today",
        "Actions, 5",
        "Waiting, 2",
        "Drafts, 3",
        "Briefs",
    ]
    sidebar.set_counts(None, 0, None)  # No count: the label alone.
    assert [read(key) for key in ("actions", "waiting", "drafts")] == [
        "Actions",
        "Waiting, 0",
        "Drafts",
    ]


def test_settings_lines_up_with_the_page_rows(qtbot: QtBot) -> None:
    from mailbrief.ui import workspace as module

    workspace = build(qtbot)
    workspace.resize(*SIZES["mockup"])
    show(qtbot, workspace)
    settings = workspace.sidebar.settings
    nav = workspace.sidebar.nav
    # Both start at the same x inside the sidebar, and paint the same offsets.
    assert (
        settings.mapTo(workspace.sidebar, settings.rect().topLeft()).x()
        == nav.mapTo(workspace.sidebar, nav.rect().topLeft()).x()
    )
    assert settings.height() == module._NAV_ROW
    assert settings.accessibleName() == "Settings"
    assert not settings.grab().isNull()


def test_pages_switch_and_refuse_unknown_keys(qtbot: QtBot) -> None:
    workspace = build(qtbot)
    assert workspace.current_page() == "today"
    page = QWidget()
    workspace.add_page("drafts", page)
    workspace.show_page("drafts")
    assert workspace.current_page() == "drafts"
    assert workspace.pages.currentWidget() is page
    with pytest.raises(KeyError):
        workspace.show_page("nowhere")
    workspace.show_page("today")
    today = workspace.pages.currentWidget()
    assert today is not None and today.objectName() == "todayPage"


def test_a_long_status_never_widens_the_header(qtbot: QtBot) -> None:
    workspace = build(qtbot)
    header = workspace.header
    header.set_status("")
    empty = header.minimumSizeHint().width()
    assert header.refresh_icon.isHidden()
    header.set_status("Checked Gmail at 09:14 " * 14)
    assert len(header.status.text()) > 300
    assert header.minimumSizeHint().width() == empty
    assert not header.refresh_icon.isHidden()
    added = QLabel("added")
    header.add_widget(added)
    assert added.parent() is header


def test_sidebar_footer_order_and_mnemonics(qtbot: QtBot) -> None:
    workspace = build(qtbot)
    sidebar = workspace.sidebar
    layout = sidebar.layout()
    assert layout is not None
    items = [layout.itemAt(index) for index in range(layout.count())]
    widgets = [None if item is None else item.widget() for item in items]
    order = [
        sidebar.nav,
        None,  # The stretch.
        sidebar.saved_mail,
        sidebar.data,
        sidebar.settings,
        sidebar.note,
        None,  # The footer layout.
    ]
    assert widgets == order
    last = items[-1]
    assert last is not None and last.layout() is sidebar.footer
    assert sidebar.saved_mail.text() == "Saved &mail"
    assert sidebar.saved_mail.accessibleName() == "Saved mail"
    assert sidebar.data.text() == "&Data"
    assert sidebar.data.accessibleName() == "Data and recovery"
    assert sidebar.settings.text() == "Se&ttings"
    assert sidebar.settings.accessibleName() == "Settings"
    sidebar.set_note("")
    assert sidebar.note.isHidden()


def test_arrow_keys_reach_briefs_like_any_page(qtbot: QtBot) -> None:
    workspace = build(qtbot)
    workspace.resize(*SIZES["mockup"])
    show(qtbot, workspace)
    sidebar = workspace.sidebar
    pages = QSignalSpy(sidebar.page_requested)
    sidebar.set_current("drafts")
    sidebar.nav.setFocus()
    QTest.keyClick(sidebar.nav, Qt.Key.Key_Down)
    assert sidebar.current_key() == "briefs"
    assert pages.at(pages.count() - 1) == ["briefs"]
    QTest.keyClick(sidebar.nav, Qt.Key.Key_Up)
    assert pages.at(pages.count() - 1) == ["drafts"]


def brief_with(**updates: object) -> DailyDigest:
    return mockup_digest().digest.model_copy(update=updates)


def coverage(**counts: int | bool) -> DigestCoverage:
    values: dict[str, int | bool] = {
        "sync_complete": True,
        "analyzed": 3,
        "reused": 1,
        "failed": 0,
        "skipped": 1,
        "deferred": 0,
    }
    values.update(counts)
    values["shortlisted"] = sum(
        int(values[key]) for key in ("analyzed", "reused", "failed", "skipped", "deferred")
    )
    return DigestCoverage.model_validate(values)


def test_brief_title_names_the_day_and_an_incomplete_brief() -> None:
    assert brief_title(brief_with()) == "Tue Oct 6"
    assert brief_title(brief_with(status=DigestStatus.PARTIAL)) == "Tue Oct 6 · Partial"
    assert brief_title(brief_with(status=DigestStatus.EMPTY)) == "Tue Oct 6 · Empty"


def test_brief_meta_short_and_full() -> None:
    complete = brief_with(coverage=coverage())
    assert brief_meta(complete, OWNER_ZONE) == (
        "owner@example.com · saved 09:14",
        "owner@example.com. Saved 2026-10-06T09:14-04:00. "
        "3 analyzed, 1 reused, 0 failed, 1 skipped. Inbox sync complete.",
    )
    later = brief_with(generated_at_utc=datetime(2026, 10, 7, 13, 5, tzinfo=UTC))
    assert brief_meta(later, OWNER_ZONE)[0] == "owner@example.com · saved Oct 7 09:05"
    troubled = brief_with(coverage=coverage(failed=2, deferred=1, sync_complete=False))
    short, full = brief_meta(troubled, OWNER_ZONE)
    assert short == (
        "owner@example.com · saved 09:14 · 2 failed · 1 deferred · Inbox sync incomplete"
    )
    assert full.endswith(
        "3 analyzed, 1 reused, 2 failed, 1 skipped, 1 deferred. Inbox sync incomplete."
    )
    unknown = brief_with(coverage=None)
    assert brief_meta(unknown, OWNER_ZONE) == (
        "owner@example.com · saved 09:14",
        "owner@example.com. Saved 2026-10-06T09:14-04:00.",
    )


def test_the_heading_shows_the_brief(qtbot: QtBot) -> None:
    workspace = build(qtbot)
    heading = workspace.heading
    assert not heading.isHidden()
    assert heading.title.text() == "Tue Oct 6"
    assert heading.meta.text() == "owner@example.com · saved 09:14"
    assert heading.meta.accessibleName().startswith("owner@example.com. Saved 2026-10-06T09:14")


def workspace_with(
    qtbot: QtBot, items: tuple[DigestItem, ...], size: tuple[int, int]
) -> ThreePaneWorkspace:
    workspace = ThreePaneWorkspace()
    qtbot.addWidget(workspace)
    digest = brief_with(items=items)
    workspace.show_digest(digest, owner_zone=OWNER_ZONE, today=TODAY)
    workspace.resize(*size)
    show(qtbot, workspace)
    return workspace


def long_brief(count: int) -> tuple[DigestItem, ...]:
    return tuple(
        make_digest_item(message_key=f"m{number}", position=number, subject=f"Email {number}")
        for number in range(count)
    )


def three_with_a_long_detail() -> tuple[DigestItem, ...]:
    """Three emails; the first one's detail is taller than the pane."""
    first, second, third = mockup_digest().digest.items[:3]
    first = first.model_copy(update={"action_text": "Read the attached notes. " * 38})
    return tuple(
        item.model_copy(update={"position": number})
        for number, item in enumerate((first, second, third))
    )


def test_page_keys_page_a_list_that_scrolls(qtbot: QtBot) -> None:
    workspace = workspace_with(qtbot, long_brief(20), (680, 420))
    view, detail = workspace.brief_list, workspace.detail.verticalScrollBar()
    assert view.verticalScrollBar().maximum() > 0  # 20 rows don't fit.
    first = view.currentIndex().row()
    QTest.keyClick(view, Qt.Key.Key_PageDown)
    assert view.currentIndex().row() > first  # The list paged…
    assert detail.value() == 0  # …and the detail stayed put.


def test_page_keys_scroll_the_detail_while_the_list_fits(qtbot: QtBot) -> None:
    workspace = workspace_with(qtbot, three_with_a_long_detail(), (680, 520))
    view, bar = workspace.brief_list, workspace.detail.verticalScrollBar()
    assert view.verticalScrollBar().maximum() == 0  # Three rows fit.
    assert bar.maximum() > 0  # The first email's detail doesn't.
    selected = view.selected_key()
    QTest.keyClick(view, Qt.Key.Key_PageDown)
    assert bar.value() > 0
    assert view.selected_key() == selected  # The selection stays put.
    QTest.keyClick(view, Qt.Key.Key_PageUp)
    assert bar.value() == 0


def test_page_up_from_a_detail_button_scrolls_the_pane(qtbot: QtBot) -> None:
    workspace = workspace_with(qtbot, three_with_a_long_detail(), (680, 520))
    bar = workspace.detail.verticalScrollBar()
    QTest.keyClick(workspace.brief_list, Qt.Key.Key_PageDown)
    scrolled = bar.value()
    assert scrolled > 0
    button = workspace.detail.buttons()[0]
    button.setFocus()
    # The button ignores Page Up, so it reaches the scroll area, which pages back.
    QTest.keyClick(button, Qt.Key.Key_PageUp)
    assert bar.value() < scrolled


def test_showing_the_same_brief_again_keeps_selection_and_scroll(qtbot: QtBot) -> None:
    brief = mockup_digest()
    workspace = build(qtbot)
    # At its minimum height the first email's detail scrolls.
    workspace.resize(680, workspace.minimumSizeHint().height())
    show(qtbot, workspace)
    view = workspace.brief_list
    rows = view.brief_model.rows()
    keys = [row.item.message_key for row in rows if row.item is not None]

    def select(key: str) -> None:
        number = next(
            index
            for index, row in enumerate(rows)
            if row.item is not None and row.item.message_key == key
        )
        view.setCurrentIndex(view.model().index(number, 0))

    def again(digest: DailyDigest) -> None:
        workspace.show_digest(
            digest, links=brief.links, proposals=brief.proposals, owner_zone=OWNER_ZONE, today=TODAY
        )

    select(keys[1])
    again(brief.digest)  # As after Accept or Dismiss.
    assert view.selected_key() == keys[1]
    select(keys[0])
    bar = workspace.detail.verticalScrollBar()
    qtbot.waitUntil(lambda: bar.maximum() > 0, timeout=2000)
    bar.setValue(bar.maximum())
    kept = bar.value()
    again(brief.digest)
    assert view.selected_key() == keys[0]
    qtbot.waitUntil(lambda: bar.value() == kept, timeout=2000)

    later = brief.digest.generated_at_utc + timedelta(hours=1)
    again(brief.digest.model_copy(update={"generated_at_utc": later}))  # Another brief.
    QApplication.processEvents()
    assert view.selected_key() == keys[0]
    assert bar.value() == 0
    select(keys[1])
    again(brief.digest)
    assert view.selected_key() == keys[0]  # Another brief again: back to the first.


def test_clear_shows_only_the_message(qtbot: QtBot) -> None:
    workspace = build(qtbot)
    workspace.clear("Saved brief unavailable.")
    assert workspace.brief_list.model().rowCount() == 0
    assert workspace.brief_list.selected_key() is None
    assert workspace.coverage.text() == ""
    assert workspace.heading.isHidden()
    texts = [label.text() for label in workspace.detail.findChildren(QLabel)]
    assert texts == ["Saved brief unavailable."]


def test_nav_buttons_are_muted_while_disabled(qtbot: QtBot) -> None:
    from mailbrief.ui.workspace import NavButton

    tokens = current_tokens()
    row = NavButton("Data", "database")
    qtbot.addWidget(row)
    assert row._color() == tokens.text_secondary
    row.setDown(True)
    assert row._color() == tokens.text
    row.setEnabled(False)
    assert row._color() == tokens.text_muted
    assert not row._active()  # No fill while disabled.
    assert not row.grab().isNull()


def test_unbroken_mail_text_never_widens_the_workspace(
    qtbot: QtBot, qapp: QApplication, themed: None
) -> None:
    apply_theme(qapp)
    token = "y" * 150
    brief = mockup_digest()
    first = brief.digest.items[0]
    long_item = first.model_copy(
        update={
            "subject": f"Subject {token}",
            "summary": f"Summary {token}",
            "sender": EmailContact(name=None, address=f"{token}@example.com"),
        }
    )
    hostile = brief.digest.model_copy(update={"items": (long_item, *brief.digest.items[1:])})

    def narrowest(digest: DailyDigest) -> int:
        workspace = ThreePaneWorkspace()
        qtbot.addWidget(workspace)
        workspace.show_digest(
            digest, links=brief.links, proposals=brief.proposals, owner_zone=OWNER_ZONE, today=TODAY
        )
        workspace.resize(*SIZES["mockup"])
        show(qtbot, workspace)
        content = workspace.detail.widget()
        assert content is not None
        assert content.width() <= workspace.detail.viewport().width()
        return workspace.minimumSizeHint().width()

    assert narrowest(hostile) == narrowest(brief.digest)


def test_the_detail_content_is_capped_and_at_the_left(qtbot: QtBot) -> None:
    workspace = build(qtbot)
    workspace.resize(*SIZES["mockup"])
    show(qtbot, workspace)
    content = workspace.detail.widget()
    assert content is not None
    assert content.maximumWidth() == DETAIL_MAX_WIDTH == 720
    assert content.x() == 0  # Left-aligned.


def test_the_detail_keeps_a_readable_width_on_wide_windows(qtbot: QtBot) -> None:
    workspace = build(qtbot)
    # As wide as the screen allows: a platform that keeps windows on screen (Cocoa; CI's Mac
    # has about 1024 × 649) gives less than this, and offscreen gives all of it. The static
    # lookup, not workspace.screen(): PySide re-parents the shared QScreen wrapper under the
    # widget it was asked from, and deleting the widget then invalidates it for everyone.
    screen = QGuiApplication.primaryScreen()
    workspace.resize(screen.availableGeometry().size().expandedTo(QSize(1600, 900)))
    show(qtbot, workspace)
    if workspace.detail.viewport().width() <= DETAIL_MAX_WIDTH:
        pytest.skip("screen too small for the cap to apply")
    content = workspace.detail.widget()
    assert content is not None
    assert 0 < content.width() <= DETAIL_MAX_WIDTH
    assert content.x() == 0  # Left-aligned.
