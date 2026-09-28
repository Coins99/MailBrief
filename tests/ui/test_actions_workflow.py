"""The window's action flows: lists at startup, complete, reopen, delete, edit and undo."""

import asyncio
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialogButtonBox
from pytestqt.qtbot import QtBot

from mailbrief.domain.actions import Action, ActionEdit, ActionFilter, ActionStatus
from mailbrief.domain.analysis import DeadlinePrecision
from mailbrief.services.actions import ActionConflictError
from mailbrief.ui.digest_view import ACCEPT, DISMISS
from mailbrief.ui.main_window import MainWindow
from tests.factories import make_action
from tests.ui.test_workflow import FakeBackend

OPEN = make_action(title="Send the deck")
DONE = make_action(
    public_id="11111111-1111-4111-8111-111111111111",
    title="Book the room",
    status=ActionStatus.COMPLETED,
    completed_at_utc=datetime(2026, 9, 28, 12, tzinfo=UTC),
    revision=4,
)


@pytest.fixture
def backend() -> FakeBackend:
    result = FakeBackend()
    result.actions = {ActionFilter.OPEN: (OPEN,), ActionFilter.COMPLETED: (DONE,)}
    return result


@pytest.fixture
def window(qtbot: QtBot, backend: FakeBackend) -> MainWindow:
    result = MainWindow(backend)
    result.zone = ZoneInfo("UTC")
    result.now = lambda: datetime(2026, 9, 28, 15, tzinfo=UTC)
    qtbot.addWidget(result)
    return result


async def finish(window: MainWindow) -> None:
    assert window.task is not None
    await window.task


async def test_startup_lists_every_view(window: MainWindow, backend: FakeBackend) -> None:
    await window.initialize()

    assert backend.list_calls == 3
    assert window.actions_panel.tabs.tabText(0) == "Open (1)"
    assert window.actions_panel.tabs.tabText(2) == "Completed (1)"
    assert window.actions_panel.selected() == OPEN


async def test_completing_then_undoing(window: MainWindow, backend: FakeBackend) -> None:
    await window.initialize()

    window.actions_panel.complete_button.click()
    await finish(window)
    assert backend.action_calls == [("complete_action", OPEN.public_id, 1)]
    assert window.status.text() == "Completed: Approve the proposal."
    assert window.undo_button.text() == "&Undo complete"
    assert backend.list_calls == 6  # Refreshed after the change.

    window.undo_button.click()
    await finish(window)
    assert backend.action_calls[-1] == ("reopen_action", OPEN.public_id, 2)


async def test_reopening_from_the_completed_tab_then_undoing(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    window.actions_panel.tabs.setCurrentIndex(2)

    window.actions_panel.complete_button.click()
    await finish(window)
    window.undo_button.click()
    await finish(window)

    assert backend.action_calls == [
        ("reopen_action", DONE.public_id, 4),
        ("complete_action", OPEN.public_id, 5),
    ]


async def test_deleting_then_undoing(window: MainWindow, backend: FakeBackend) -> None:
    await window.initialize()

    window.actions_panel.delete_button.click()
    await finish(window)
    assert window.status.text() == "Deleted: Send the deck."
    window.undo_button.click()
    await finish(window)

    assert backend.action_calls == [
        ("delete_action", OPEN.public_id, 1),
        ("restore_action", OPEN.public_id),
    ]


async def test_editing_saves_through_the_backend_and_withdraws_undo(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    window.actions_panel.delete_button.click()
    await finish(window)
    assert not window.undo_button.isHidden()

    window.actions_panel.edit_button.click()
    assert window.action_editor.title.text() == "Send the deck"
    window.action_editor.title.setText("Send the final deck")
    save = window.action_editor.buttons.button(QDialogButtonBox.StandardButton.Save)
    assert save is not None
    save.click()
    await finish(window)

    name, public_id, revision, edit, steps = backend.action_calls[-1]
    assert (name, public_id, revision, steps) == ("save_action", OPEN.public_id, 1, None)
    assert isinstance(edit, ActionEdit) and edit.title == "Send the final deck"
    assert window.status.text() == "Saved: Send the final deck."
    assert window.undo_button.isHidden()
    assert not window.action_editor.isVisible()  # Closed only once the save succeeded.


async def test_a_stale_action_reloads_the_views(window: MainWindow, backend: FakeBackend) -> None:
    await window.initialize()
    backend.action_fail = ActionConflictError("stale")
    loads, lists = backend.loads, backend.list_calls

    window.actions_panel.complete_button.click()
    await finish(window)

    assert "reloaded" in window.status.text()
    assert (backend.loads, backend.list_calls) == (loads + 1, lists + 3)
    assert window.undo_button.isHidden()


async def test_a_failed_refresh_after_a_saved_change_says_so_once(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    backend.list_fail = RuntimeError("database locked")

    window.actions_panel.complete_button.click()
    await finish(window)

    assert window.status.text() == (
        "Completed: Approve the proposal. "
        "The view could not be refreshed; restart MailBrief to see the latest."
    )
    assert not window.undo_button.isHidden()  # The change was saved, so it can be undone.


async def test_action_buttons_are_disabled_while_another_operation_runs(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    window.start(window._generate)
    await asyncio.sleep(0)

    assert not window.actions_panel.complete_button.isEnabled()
    window.cancel()
    await finish(window)
    assert window.actions_panel.complete_button.isEnabled()


@pytest.mark.parametrize("change", ["reopen", "delete", "save"])
async def test_a_stale_reopen_delete_or_save_reloads_and_offers_no_undo(
    window: MainWindow, backend: FakeBackend, change: str
) -> None:
    await window.initialize()
    backend.action_fail = ActionConflictError("stale")
    panel = window.actions_panel

    if change == "reopen":
        panel.tabs.setCurrentIndex(2)
        panel.complete_button.click()  # It reads "Reopen" on the Completed tab.
    elif change == "delete":
        panel.delete_button.click()
    else:
        panel.edit_button.click()
        button = window.action_editor.buttons.button(QDialogButtonBox.StandardButton.Save)
        assert button is not None
        button.click()
    await finish(window)

    assert backend.action_calls[-1][0] == f"{change}_action"
    assert window.status.text() == "That changed or is no longer available; the view was reloaded."
    assert window.undo_button.isHidden()


@pytest.mark.parametrize("lists_fail", [False, True], ids=["brief", "brief-and-lists"])
async def test_a_failed_refresh_after_a_dismiss_keeps_the_message_and_notes_it_once(
    window: MainWindow,
    backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
    lists_fail: bool,
) -> None:
    await window.initialize()

    async def locked() -> None:
        raise RuntimeError("database locked")

    monkeypatch.setattr(backend, "load_saved", locked)
    if lists_fail:
        backend.list_fail = RuntimeError("database locked")

    window.digest.suggestion_requested.emit(DISMISS, 7)
    await finish(window)

    assert window.status.text() == (
        "Suggestion dismissed. It won't be suggested again for this email. "
        "The view could not be refreshed; restart MailBrief to see the latest."
    )
    assert window.undo_button.text() == "&Undo dismiss"  # The dismissal itself was saved.


async def test_accepting_a_suggestion_refreshes_every_action_list(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    lists = backend.list_calls
    accepted = make_action(
        public_id="22222222-2222-4222-8222-222222222222", title="Approve the budget"
    )
    backend.actions[ActionFilter.OPEN] = (OPEN, accepted)

    window.digest.suggestion_requested.emit(ACCEPT, 7)
    await finish(window)

    assert backend.list_calls == lists + 3
    panel = window.actions_panel
    assert panel.tabs.tabText(0) == "Open (2)"
    assert panel.lists[ActionFilter.OPEN].item(1).text().startswith("Approve the budget")


@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_Enter])
async def test_enter_on_an_action_row_opens_the_editor_for_it(
    window: MainWindow, key: Qt.Key
) -> None:
    await window.initialize()
    listing = window.actions_panel.lists[ActionFilter.OPEN]
    listing.setCurrentRow(0)

    QTest.keyClick(listing, key)  # What qtbot.keyClick forwards to, with types.

    assert window.action_editor.isVisible()
    assert window.action_editor.title.text() == "Send the deck"
    window.action_editor.reject()


async def test_the_editor_shows_deadlines_in_the_window_s_zone(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.actions = {
        ActionFilter.OPEN: (
            make_action(
                deadline_text="Friday 4 PM Central",
                deadline_precision=DeadlinePrecision.DATETIME,
                deadline_date=date(2026, 10, 2),
                deadline_at_utc=datetime(2026, 10, 2, 21, 0, tzinfo=UTC),
                deadline_timezone="America/Chicago",
            ),
        )
    }
    window.zone = ZoneInfo("America/Toronto")
    await window.initialize()

    window.actions_panel.edit_button.click()

    assert window.action_editor.deadline.text().startswith("2026-10-02T17:00-04:00 (16:00")


async def test_closing_the_window_dismisses_an_open_editor(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    window.actions_panel.edit_button.click()
    assert window.action_editor.isVisible()

    window.close()

    assert not window.action_editor.isVisible()
    assert window.task is None or window.task.done()
    await window.shutdown()


async def test_a_save_after_the_window_closes_starts_nothing(window: MainWindow) -> None:
    await window.initialize()
    spy = AsyncMock()
    window.backend = spy
    window.close()

    window.action_editor.save_requested.emit(
        OPEN,
        ActionEdit(
            title="Too late", ownership=OPEN.ownership, effort=None, target_date=None, notes=""
        ),
        None,
    )
    await asyncio.sleep(0)

    assert window.task is None
    assert spy.mock_calls == []
    await window.shutdown()


def click_save(window: MainWindow) -> None:
    button = window.action_editor.buttons.button(QDialogButtonBox.StandardButton.Save)
    assert button is not None
    button.click()


def edit_everything(window: MainWindow) -> None:
    """Open the editor on OPEN and change its title, notes and plan."""
    window.actions_panel.edit_button.click()
    editor = window.action_editor
    editor.title.setText("Send the final deck")
    editor.notes.setPlainText("Ask Sam for the churn slide.")
    editor.add_step.click()
    added = editor.steps.item(editor.steps.count() - 1)
    assert added is not None
    added.setText("Book a review")


EDITED = ("Send the final deck", "Ask Sam for the churn slide.", [(None, "Book a review", False)])


def edits(window: MainWindow) -> tuple[str, str, list[tuple[int | None, str, bool]]]:
    editor = window.action_editor
    return editor.title.text(), editor.notes.toPlainText(), editor._current_steps()


async def test_the_editor_stays_open_while_its_save_runs(
    window: MainWindow, backend: FakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    await window.initialize()
    release = asyncio.Event()
    save_action = backend.save_action

    async def slow_save(*args: Any) -> Action:
        await release.wait()
        return await save_action(*args)

    monkeypatch.setattr(backend, "save_action", slow_save)
    edit_everything(window)

    click_save(window)
    await asyncio.sleep(0)

    editor = window.action_editor
    assert editor.isVisible()
    assert editor.status.text() == "Saving…"
    assert not editor.title.isEnabled()
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    assert editor.isVisible()
    release.set()
    await finish(window)
    assert not editor.isVisible()
    assert window.status.text() == "Saved: Send the final deck."


async def test_a_conflicting_save_keeps_the_edits_and_reloads(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    edit_everything(window)
    backend.action_fail = ActionConflictError("stale")
    loads, lists = backend.loads, backend.list_calls

    click_save(window)
    await finish(window)

    editor = window.action_editor
    assert editor.isVisible()
    assert editor.status.text() == (
        "This action changed since you opened it. Your edits are still here — copy what you "
        "need, then Cancel and reopen the action."
    )
    assert edits(window) == EDITED
    assert editor.title.isEnabled()
    assert (backend.loads, backend.list_calls) == (loads + 1, lists + 3)
    assert window.status.text() == "That changed or is no longer available; the view was reloaded."
    editor.reject()  # Cancel works again.
    assert not editor.isVisible()


async def test_a_failed_save_keeps_the_edits_and_lets_the_owner_retry(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    edit_everything(window)
    backend.action_fail = RuntimeError("database is locked")

    click_save(window)
    await finish(window)

    editor = window.action_editor
    assert editor.isVisible()
    assert editor.status.text() == "Couldn't save. Your edits are still here; try again."
    assert edits(window) == EDITED
    assert window.status.text() == "Operation failed. The displayed saved brief is unchanged."
    assert "database is locked" not in editor.status.text() + window.status.text()

    click_save(window)  # The failure was one-off, so the retry saves.
    await finish(window)

    assert not editor.isVisible()
    assert window.status.text() == "Saved: Send the final deck."
    assert [call[0] for call in backend.action_calls] == ["save_action", "save_action"]


async def test_a_save_while_another_operation_runs_keeps_the_editor_open(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    edit_everything(window)
    editor = window.action_editor
    editor._set_saving(True)  # As if Save was clicked just before another operation began.
    window.start(window._generate)
    await asyncio.sleep(0)

    editor.save_requested.emit(
        OPEN,
        ActionEdit(
            title="Too soon", ownership=OPEN.ownership, effort=None, target_date=None, notes=""
        ),
        None,
    )

    assert editor.isVisible()
    assert editor.status.text() == "MailBrief is busy; try again in a moment."
    assert edits(window) == EDITED
    assert editor.title.isEnabled()
    assert backend.action_calls == []
    window.cancel()
    await finish(window)


@pytest.mark.parametrize("outcome", [None, ActionConflictError("stale"), RuntimeError("locked")])
async def test_quitting_during_a_save_lets_it_finish_and_shuts_down(
    window: MainWindow,
    backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
    outcome: Exception | None,
) -> None:
    await window.initialize()
    release = asyncio.Event()
    save_action = backend.save_action

    async def slow_save(*args: Any) -> Action:
        await release.wait()
        backend.action_fail = outcome
        return await save_action(*args)

    monkeypatch.setattr(backend, "save_action", slow_save)
    edit_everything(window)
    click_save(window)
    await asyncio.sleep(0)

    window.close()

    assert not window.action_editor.isVisible()
    shutdown = asyncio.create_task(window.shutdown())
    await asyncio.sleep(0)
    assert not shutdown.done()  # The save is not cancelled; shutdown waits for it.
    release.set()
    await shutdown
    assert backend.closed
    assert backend.action_calls[-1][0] == "save_action"
    assert not window.action_editor.isVisible()
    assert window.action_editor.status.text() == "Saving…"  # Nothing more to tell anyone.
