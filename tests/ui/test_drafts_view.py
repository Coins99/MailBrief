"""The drafts panel: plain rows, New, Open (Return or double-click), Delete and busy state."""

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot

from mailbrief.domain.drafts import DraftKind, DraftSummary
from mailbrief.ui.drafts_view import DELETE, NEW, OPEN, DraftsPanel, describe

TORONTO = ZoneInfo("America/Toronto")


def summary(number: int, **overrides: Any) -> DraftSummary:
    values: dict[str, Any] = {
        "public_id": f"00000000-0000-4000-8000-{number:012d}",
        "kind": DraftKind.NOTE,
        "display_title": f"Draft {number}",
        "updated_at_utc": datetime(2026, 9, 28, 14, 5, tzinfo=UTC),
        "placeholder_count": 0,
        "revision": 1,
    }
    values.update(overrides)
    return DraftSummary.model_validate(values)


@pytest.fixture
def panel(qtbot: QtBot) -> DraftsPanel:
    result = DraftsPanel()
    qtbot.addWidget(result)
    return result


def requests(panel: DraftsPanel) -> list[tuple[str, object]]:
    found: list[tuple[str, object]] = []
    panel.draft_requested.connect(lambda kind, value: found.append((kind, value)))
    return found


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, "Note · Draft 1 — updated 2026-09-28 10:05"),
        (
            {"kind": DraftKind.REPLY, "placeholder_count": 1, "action_title": "Send it"},
            "Reply · Draft 1 — updated 2026-09-28 10:05 · 1 placeholder · for “Send it”",
        ),
        (
            {"kind": DraftKind.MESSAGE, "placeholder_count": 3},
            "Message · Draft 1 — updated 2026-09-28 10:05 · 3 placeholders",
        ),
    ],
)
def test_describe(fields: dict[str, Any], expected: str) -> None:
    assert describe(summary(1, **fields), TORONTO) == expected


def test_rows_are_plain_text_and_keep_the_selection(panel: DraftsPanel) -> None:
    assert not panel.empty.isHidden()
    assert not panel.open_button.isEnabled()
    first, second = summary(1, display_title="<b>bold</b>"), summary(2)
    panel.show_drafts((first, second), TORONTO)
    panel.list.setCurrentRow(1)

    panel.show_drafts((summary(3), first, second), TORONTO)

    assert panel.empty.isHidden()
    assert panel.selected() == second
    assert panel.list.item(1).text().startswith("Note · <b>bold</b>")
    panel.show_drafts((summary(3),), TORONTO)
    assert panel.selected() == summary(3)
    panel.show_drafts((), TORONTO)
    assert panel.selected() is None and not panel.delete_button.isEnabled()


def test_new_offers_email_note_and_message(panel: DraftsPanel) -> None:
    found = requests(panel)
    names = [action.text() for action in panel.new_menu.actions()]
    assert names == ["Email", "Note", "Message"]

    for action in panel.new_menu.actions():
        action.trigger()

    assert found == [(NEW, DraftKind.EMAIL), (NEW, DraftKind.NOTE), (NEW, DraftKind.MESSAGE)]


def test_open_by_button_return_or_double_click_and_delete(panel: DraftsPanel, qtbot: QtBot) -> None:
    found = requests(panel)
    panel.show_drafts((summary(1),), TORONTO)

    panel.open_button.click()
    QTest.keyClick(panel.list, Qt.Key.Key_Return)
    item = panel.list.item(0)
    panel.list.itemActivated.emit(item)
    panel.delete_button.click()
    QTest.keyClick(panel.list, Qt.Key.Key_Down)  # Other keys still move the selection.

    assert found == [(OPEN, summary(1))] * 3 + [(DELETE, summary(1))]


def test_busy_disables_everything(panel: DraftsPanel) -> None:
    found = requests(panel)
    panel.show_drafts((summary(1),), TORONTO)

    panel.set_busy(True)
    panel.new_menu.actions()[0].trigger()
    panel.list.itemActivated.emit(panel.list.item(0))

    assert found == []
    assert not any(
        button.isEnabled() for button in (panel.new_button, panel.open_button, panel.delete_button)
    )
    panel.set_busy(False)
    assert panel.open_button.isEnabled()


def test_without_drafts_a_centred_message_replaces_the_list(panel: DraftsPanel) -> None:
    assert panel.list.isHidden() and not panel.empty.isHidden()
    assert panel.empty.property("tone") == "muted"
    assert panel.empty.alignment() & Qt.AlignmentFlag.AlignCenter
    assert panel.empty.wordWrap()
    panel.show_drafts((summary(1),), TORONTO)
    assert not panel.list.isHidden() and panel.empty.isHidden()
    panel.show_drafts((), TORONTO)
    assert panel.list.isHidden() and not panel.empty.isHidden()
