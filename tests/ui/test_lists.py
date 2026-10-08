"""Return and Enter activate a list's current row exactly once, on every platform."""

from datetime import UTC, date, datetime

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog, QListWidgetItem, QPushButton, QVBoxLayout
from pytestqt.qtbot import QtBot

from mailbrief.domain.actions import ActionFilter
from mailbrief.domain.digests import DigestStatus, SavedBriefSummary
from mailbrief.ui.actions_view import EDIT, ActionsPanel
from mailbrief.ui.drafts_view import OPEN, DraftsPanel
from mailbrief.ui.history_view import BriefHistoryPanel
from mailbrief.ui.lists import ActivatingList
from tests.factories import make_action
from tests.ui.test_actions_view import NOW, TODAY, UTC_ZONE
from tests.ui.test_drafts_view import TORONTO, summary

KEYS = [Qt.Key.Key_Return, Qt.Key.Key_Enter]


@pytest.mark.parametrize("key", KEYS)
def test_the_key_activates_once_and_never_reaches_the_default_button(
    qtbot: QtBot, key: Qt.Key
) -> None:
    dialog = QDialog()
    qtbot.addWidget(dialog)
    layout = QVBoxLayout(dialog)
    listing = ActivatingList()
    listing.addItem(QListWidgetItem("first"))
    default = QPushButton("Default")
    default.setDefault(True)
    layout.addWidget(listing)
    layout.addWidget(default)
    activated: list[str] = []
    clicked: list[bool] = []
    listing.itemActivated.connect(lambda item: activated.append(item.text()))
    default.clicked.connect(lambda: clicked.append(True))
    dialog.show()
    listing.setCurrentRow(0)
    listing.setFocus()

    QTest.keyClick(listing, key)

    assert activated == ["first"]
    assert clicked == []


def test_other_keys_still_move_between_rows(qtbot: QtBot) -> None:
    listing = ActivatingList()
    qtbot.addWidget(listing)
    for text in ("first", "second"):
        listing.addItem(QListWidgetItem(text))
    listing.show()
    listing.setCurrentRow(0)
    QTest.keyClick(listing, Qt.Key.Key_Down)
    assert listing.currentRow() == 1


def test_an_empty_list_ignores_the_key(qtbot: QtBot) -> None:
    listing = ActivatingList()
    qtbot.addWidget(listing)
    activated: list[object] = []
    listing.itemActivated.connect(activated.append)
    listing.show()
    QTest.keyClick(listing, Qt.Key.Key_Return)
    assert activated == []


@pytest.mark.parametrize("key", KEYS)
def test_the_briefs_page_opens_a_brief_once(qtbot: QtBot, key: Qt.Key) -> None:
    panel = BriefHistoryPanel()
    qtbot.addWidget(panel)
    opened: list[object] = []
    panel.open_requested.connect(lambda email, day: opened.append((email, day)))
    panel.configure(
        (
            SavedBriefSummary(
                account_email="owner@example.com",
                local_date=date(2026, 9, 3),
                timezone_name="UTC",
                status=DigestStatus.EMPTY,
                generated_at_utc=datetime(2026, 9, 5, tzinfo=UTC),
                item_count=0,
            ),
        ),
        (),
        "owner@example.com",
        date(2026, 9, 5),
    )
    panel.show()
    panel.saved.setFocus()

    QTest.keyClick(panel.saved, key)

    assert opened == [("owner@example.com", date(2026, 9, 3))]


@pytest.mark.parametrize("key", KEYS)
def test_the_actions_panel_edits_once(qtbot: QtBot, key: Qt.Key) -> None:
    panel = ActionsPanel()
    qtbot.addWidget(panel)
    requests: list[str] = []
    panel.action_requested.connect(lambda kind, action: requests.append(kind))
    panel.show_actions(ActionFilter.OPEN, (make_action(),), today=TODAY, zone=UTC_ZONE, now=NOW)
    panel.show()
    listing = panel.lists[ActionFilter.OPEN]
    listing.setCurrentRow(0)
    listing.setFocus()

    QTest.keyClick(listing, key)

    assert requests == [EDIT]


@pytest.mark.parametrize("key", KEYS)
def test_the_drafts_panel_opens_once(qtbot: QtBot, key: Qt.Key) -> None:
    panel = DraftsPanel()
    qtbot.addWidget(panel)
    requests: list[str] = []
    panel.draft_requested.connect(lambda kind, value: requests.append(kind))
    panel.show_drafts((summary(1),), TORONTO)
    panel.show()
    panel.list.setCurrentRow(0)
    panel.list.setFocus()

    QTest.keyClick(panel.list, key)

    assert requests == [OPEN]
