"""The window's draft flows: entry points, the save queue, conflicts, closing and quitting."""

import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from pydantic import HttpUrl
from PySide6.QtCore import QUrl
from pytestqt.qtbot import QtBot

from mailbrief.domain.actions import ActionFilter, ActionSource
from mailbrief.domain.drafts import DraftEdit, DraftKind, DraftVersionOrigin
from mailbrief.domain.preferences import OwnerPreferences
from mailbrief.ui.draft_editor import CONFLICT, NOT_SAVED
from mailbrief.ui.drafts_view import OPEN
from mailbrief.ui.main_window import DraftWrites, MainWindow
from tests.factories import make_action
from tests.ui.test_workflow import FakeBackend

ACTION = make_action(
    title="Send the deck",
    sources=(
        ActionSource(
            provider_message_id="local-1",
            subject="Approval needed by Friday",
            sender_address="alex@example.com",
            web_link=HttpUrl("https://mail.google.com/mail/u/?authuser=me#all/local-1"),
            received_at_utc=datetime(2026, 9, 4, 9, tzinfo=UTC),
            available=True,
        ),
    ),
)
UNSOURCED = make_action(public_id="22222222-2222-4222-8222-222222222222", title="Call Sam")


@pytest.fixture
def backend() -> FakeBackend:
    result = FakeBackend()
    result.actions = {ActionFilter.OPEN: (ACTION, UNSOURCED)}
    result.action_titles = {ACTION.public_id: ACTION.title, UNSOURCED.public_id: "Call Sam"}
    result.owner_preferences = OwnerPreferences(revision=1, time_zone="UTC")
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


async def settle(window: MainWindow) -> None:
    await window.draft_writes.drain()


async def open_new(window: MainWindow, kind: DraftKind = DraftKind.NOTE) -> None:
    window.drafts_panel.new_menu.actions()[
        [DraftKind.EMAIL, DraftKind.NOTE, DraftKind.MESSAGE].index(kind)
    ].trigger()
    await finish(window)


def is_closed(backend: FakeBackend) -> bool:
    return backend.closed


def is_idle(writes: DraftWrites) -> bool:
    return writes.idle


def calls(backend: FakeBackend, name: str) -> list[tuple[object, ...]]:
    return [call for call in backend.draft_calls if call[0] == name]


async def test_startup_lists_drafts(window: MainWindow, backend: FakeBackend) -> None:
    await backend.create_draft(DraftKind.NOTE)

    await window.initialize()

    assert window.drafts_panel.list.count() == 1
    assert window.drafts_panel.list.item(0).text().startswith("Note · Untitled note")


async def test_a_brief_item_opens_a_prefilled_reply(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    assert "Draft a reply" in window.digest.toPlainText()

    window.digest.anchorClicked.emit(QUrl("mailbrief:reply/0"))
    await finish(window)

    assert backend.draft_calls == [("create_reply_draft", "owner@example.com", "local-1")]
    editor = window.draft_editor
    assert editor.isVisible()
    assert (editor.to_edit.text(), editor.title_edit.text()) == (
        "alex@example.com",
        "Re: Approval needed by Friday",
    )
    assert window.status.text() == "Reply draft started. It saves as you type."
    assert window.drafts_panel.list.count() == 1


async def test_a_reply_to_mail_no_longer_local_says_so(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    backend.reply_sources.clear()

    window.digest.anchorClicked.emit(QUrl("mailbrief:reply/0"))
    await finish(window)

    assert window.status.text() == "That email is no longer in local mail."
    assert not window.draft_editor.isVisible()


async def test_the_actions_panel_drafts_a_reply_note_email_or_message(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    menu = window.actions_panel.draft_menu.actions()
    assert [action.text() for action in menu] == [
        "Reply to its email",
        "Email",
        "Note",
        "Message",
    ]
    assert menu[0].isEnabled()

    menu[0].trigger()
    await finish(window)
    editor = window.draft_editor
    assert editor.title_edit.text() == "Re: Approval needed by Friday"
    editor.force_close()
    menu[2].trigger()
    await finish(window)

    assert calls(backend, "create_draft_for_action") == [
        ("create_draft_for_action", ACTION.public_id, DraftKind.REPLY),
        ("create_draft_for_action", ACTION.public_id, DraftKind.NOTE),
    ]
    assert editor.title_edit.text() == "Send the deck"
    assert editor.context.text().endswith("For action: “Send the deck”")
    window.draft_editor.force_close()
    window.actions_panel.lists[ActionFilter.OPEN].setCurrentRow(1)
    assert not menu[0].isEnabled()  # Call Sam has no email to reply to.
    menu[0].trigger()
    assert window.task is None or window.task.done()
    assert len(calls(backend, "create_draft_for_action")) == 2


async def test_new_and_open_from_the_drafts_panel(window: MainWindow, backend: FakeBackend) -> None:
    await window.initialize()

    await open_new(window, DraftKind.MESSAGE)
    assert window.draft_editor.windowTitle() == "Message draft"
    assert window.draft_editor.to_edit.isHidden()
    window.draft_editor.force_close()
    window.drafts_panel.open_button.click()
    await finish(window)

    public_id = next(iter(backend.drafts))
    assert backend.draft_calls == [
        ("create_draft", DraftKind.MESSAGE),
        ("get_draft", public_id),
    ]
    assert window.draft_editor.isVisible()


async def test_opening_a_draft_deleted_elsewhere_reloads(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    await open_new(window)
    window.draft_editor.force_close()
    backend.deleted.add(next(iter(backend.drafts)))
    lists = backend.draft_lists

    window.drafts_panel.draft_requested.emit(OPEN, window.drafts_panel.selected())
    await finish(window)

    assert "reloaded" in window.status.text()
    assert backend.draft_lists == lists + 1
    assert not window.draft_editor.isVisible()


async def test_typing_autosaves_without_making_the_window_busy(
    window: MainWindow, backend: FakeBackend, qtbot: QtBot
) -> None:
    await window.initialize()
    await open_new(window)
    editor = window.draft_editor
    editor.set_timing(debounce_ms=20, max_wait_ms=5_000, retry_ms=5_000)

    editor.body.setPlainText("Dear team")
    qtbot.waitUntil(lambda: not window.draft_writes.idle, timeout=2_000)
    assert window.generate_button.isEnabled()  # Not busy.
    await settle(window)

    (autosave,) = calls(backend, "autosave_draft")
    assert autosave[2:] == (1, DraftEdit(body="Dear team"))
    assert editor.draft is not None and editor.draft.revision == 2
    assert editor.status.text().startswith("Saved ")


async def test_waiting_autosaves_coalesce_to_the_newest(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    await open_new(window)
    editor = window.draft_editor
    backend.draft_gate = asyncio.Event()
    for text in ("one", "two", "three"):
        editor.body.setPlainText(text)
        editor._autosave_now()
        await asyncio.sleep(0)

    backend.draft_gate.set()
    await settle(window)

    assert [call[2] for call in calls(backend, "autosave_draft")] == [1, 2]
    assert [call[3].body for call in calls(backend, "autosave_draft")] == ["one", "three"]  # type: ignore[attr-defined]
    assert editor.draft is not None and editor.draft.body == "three"


async def test_versions_wait_for_pending_autosaves(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    await open_new(window)
    editor = window.draft_editor
    editor.body.setPlainText("Version me")

    editor.checkpoint_button.click()
    await settle(window)

    assert [call[0] for call in backend.draft_calls[-2:]] == ["autosave_draft", "checkpoint_draft"]
    assert backend.draft_calls[-1][2] == 2  # The revision after the autosave.
    assert editor.status.text() == "Saved version 2."

    editor.versions_button.click()
    await settle(window)
    assert editor.versions_list.count() == 2
    assert editor.version_preview.toPlainText() == "Version me"
    editor.versions_list.setCurrentRow(1)
    await settle(window)
    editor.body.setPlainText("Changed")
    editor.restore_button.click()
    editor.confirm_button.click()
    await settle(window)

    assert editor.body.toPlainText() == ""
    assert editor.status.text().startswith("Restored version 1.")
    assert [v.origin for v in backend.versions[editor.draft.public_id]] == [  # type: ignore[union-attr]
        DraftVersionOrigin.CREATED,
        DraftVersionOrigin.EDITED,
        DraftVersionOrigin.EDITED,
        DraftVersionOrigin.RESTORED,
    ]
    assert editor.versions_list.count() == 4


async def test_a_conflict_keeps_the_text_and_saves_it_as_a_new_draft(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    await open_new(window)
    editor = window.draft_editor
    assert editor.draft is not None
    original = editor.draft.public_id
    backend.change_elsewhere(original)

    editor.body.setPlainText("My words")
    editor._autosave_now()
    await settle(window)

    assert editor.in_conflict and editor.status.text() == CONFLICT
    assert editor.body.toPlainText() == "My words"
    editor.save_as_new_button.click()
    await settle(window)

    assert calls(backend, "save_draft_as_new") == [
        ("save_draft_as_new", original, DraftEdit(body="My words"))
    ]
    assert editor.draft is not None and editor.draft.public_id != original
    assert not window.draft_editor.in_conflict  # Read afresh: the conflict has ended.
    editor.body.setPlainText("My words, continued")
    editor._autosave_now()
    await settle(window)
    assert calls(backend, "autosave_draft")[-1][1] == editor.draft.public_id
    assert window.drafts_panel.list.count() == 2


async def test_a_failed_autosave_keeps_the_text_and_retries(
    window: MainWindow, backend: FakeBackend, caplog: pytest.LogCaptureFixture
) -> None:
    await window.initialize()
    await open_new(window)
    editor = window.draft_editor
    backend.draft_fail = RuntimeError("private draft text")
    editor.body.setPlainText("private draft text")
    editor._autosave_now()
    with caplog.at_level(logging.WARNING, logger="mailbrief.desktop"):
        await settle(window)

    assert editor.status.text() == NOT_SAVED
    assert editor.dirty
    assert "private" not in caplog.text
    editor._autosave_now()  # The retry timer or the next change does this.
    await settle(window)
    assert editor.status.text().startswith("Saved ")
    assert backend.drafts[editor.draft.public_id].body == "private draft text"  # type: ignore[union-attr]


async def test_closing_saves_and_checkpoints_then_closes(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    await open_new(window)
    editor = window.draft_editor
    editor.title_edit.setText("Plan")
    editor.body.setPlainText("Final words")

    editor.close_button.click()
    assert editor.isVisible()
    await settle(window)

    assert [call[0] for call in backend.draft_calls[-2:]] == ["autosave_draft", "checkpoint_draft"]
    assert not editor.isVisible()
    assert window.status.text() == "Saved: Plan."
    assert window.drafts_panel.list.item(0).text().startswith("Note · Plan")
    public_id = next(iter(backend.drafts))
    assert backend.versions[public_id][-1].body == "Final words"


@pytest.mark.parametrize("failure", ["autosave", "checkpoint", "conflict", "checkpoint_conflict"])
async def test_a_failed_close_keeps_the_editor_open(
    window: MainWindow, backend: FakeBackend, failure: str
) -> None:
    await window.initialize()
    await open_new(window)
    editor = window.draft_editor
    assert editor.draft is not None
    editor.body.setPlainText("Keep me")
    if failure == "autosave":
        backend.draft_fail = OSError("disk")
    elif failure == "conflict":
        backend.change_elsewhere(editor.draft.public_id)
    else:
        editor._autosave_now()
        await settle(window)
        if failure == "checkpoint":
            backend.draft_fail = OSError("disk")
        else:
            backend.change_elsewhere(editor.draft.public_id, "Keep me")

    editor.close_button.click()
    await settle(window)

    assert editor.isVisible()
    assert editor.body.toPlainText() == "Keep me"
    expected = CONFLICT if "conflict" in failure else "Couldn't save."
    assert editor.status.text().startswith(expected)
    assert editor.status.text().endswith(
        "Press Close again to close without saving your latest changes."
    )


async def test_quitting_mid_autosave_drains_the_queue_before_closing_storage(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    await open_new(window)
    editor = window.draft_editor
    backend.draft_gate = asyncio.Event()
    editor.body.setPlainText("Saving now")
    editor._autosave_now()
    await asyncio.sleep(0)
    editor.body.setPlainText("Typed after")  # Not yet sent: the debounce is still waiting.
    seen_at_close: list[str] = []
    real_close = backend.close

    async def close() -> None:
        seen_at_close.extend(
            cast(DraftEdit, call[3]).body for call in calls(backend, "autosave_draft")
        )
        await real_close()

    backend.close = close  # type: ignore[method-assign]

    window.close()
    assert not editor.isVisible()
    shutdown = asyncio.create_task(window.shutdown())
    for _ in range(5):
        await asyncio.sleep(0)
    assert not is_closed(backend)
    backend.draft_gate.set()
    await shutdown

    assert backend.closed
    assert seen_at_close == ["Saving now", "Typed after"]
    assert backend.drafts[editor.draft.public_id].body == "Typed after"  # type: ignore[union-attr]


async def test_deleting_a_draft_offers_undo(window: MainWindow, backend: FakeBackend) -> None:
    await window.initialize()
    await open_new(window)
    window.draft_editor.force_close()
    public_id = next(iter(backend.drafts))

    window.drafts_panel.delete_button.click()
    await finish(window)

    assert calls(backend, "delete_draft") == [("delete_draft", public_id, 1)]
    assert window.drafts_panel.list.count() == 0
    assert window.undo_button.text() == "&Undo delete"
    assert window.status.text() == "Deleted: Untitled note."
    window.undo_button.click()
    await finish(window)
    assert calls(backend, "restore_draft") == [("restore_draft", public_id)]
    assert window.drafts_panel.list.count() == 1


async def test_a_stale_delete_reloads(window: MainWindow, backend: FakeBackend) -> None:
    await window.initialize()
    await open_new(window)
    window.draft_editor.force_close()
    backend.change_elsewhere(next(iter(backend.drafts)))

    window.drafts_panel.delete_button.click()
    await finish(window)

    assert "reloaded" in window.status.text()
    assert window.undo_button.isHidden()


async def test_export_writes_utf8_atomically_and_warns_about_placeholders(
    window: MainWindow, tmp_path: Path
) -> None:
    await window.initialize()
    await open_new(window)
    editor = window.draft_editor
    editor.title_edit.setText("Café")
    editor.body.setPlainText("Bonjour [[name]]\n")
    target = tmp_path / "Café.md"
    target.write_text("old", encoding="utf-8")

    editor.export_to(str(target))
    await settle(window)

    assert target.read_bytes() == "# Café\n\nBonjour [[name]]\n".encode()
    assert editor.status.text() == "Exported Café.md. 1 placeholder still needs filling."
    assert os.listdir(tmp_path) == ["Café.md"]  # No temporary file left behind.
    editor.body.setPlainText("Bonjour Alex")
    editor.export_to(str(tmp_path / "plain.txt"))
    await settle(window)
    assert (tmp_path / "plain.txt").read_text(encoding="utf-8") == "Café\n\nBonjour Alex\n"
    assert editor.status.text() == "Exported plain.txt."


async def test_a_failed_export_says_so(window: MainWindow, tmp_path: Path) -> None:
    await window.initialize()
    await open_new(window)

    window.draft_editor.export_to(str(tmp_path / "missing" / "out.txt"))
    await settle(window)

    assert window.draft_editor.status.text() == "Couldn't export. Check the folder and try again."


async def test_a_failed_draft_list_refresh_says_so(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.draft_list_fail = RuntimeError("locked")
    await window.initialize()
    assert window.status.text().endswith(
        "The view could not be refreshed; restart MailBrief to see the latest."
    )


async def test_the_queue_runs_in_order_and_survives_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    writes = DraftWrites()
    order: list[str] = []
    gate = asyncio.Event()

    async def save(name: str) -> None:
        if name == "first":
            await gate.wait()
        order.append(name)

    async def fail() -> None:
        raise ValueError("private")

    writes.autosave(lambda: save("first"))
    await asyncio.sleep(0)
    writes.run(fail)
    writes.autosave(lambda: save("skipped"))
    writes.autosave(lambda: save("newest"))
    writes.run(lambda: save("after"))
    assert not is_idle(writes)
    gate.set()
    with caplog.at_level(logging.WARNING, logger="mailbrief.desktop"):
        await writes.drain()

    assert order == ["first", "newest", "after"]
    assert writes.idle
    assert "exception=ValueError" in caplog.text and "private" not in caplog.text


@pytest.mark.parametrize(
    ("operation", "stale", "message"),
    [
        ("checkpoint", False, "Couldn't save a version; try again."),
        ("checkpoint", True, CONFLICT),
        ("versions", False, "Couldn't load the saved versions; try again."),
        ("preview", False, "Couldn't load that version; try again."),
        ("restore", False, "Couldn't restore that version; try again."),
        ("restore", True, CONFLICT),
        ("save_as_new", False, "Couldn't save a new draft; try again."),
    ],
)
async def test_failed_draft_operations_keep_the_editor_usable(
    window: MainWindow, backend: FakeBackend, operation: str, stale: bool, message: str
) -> None:
    await window.initialize()
    await open_new(window)
    editor = window.draft_editor
    assert editor.draft is not None
    editor.body.setPlainText("Mine")
    editor._autosave_now()
    await settle(window)
    if operation in ("preview", "restore"):
        editor.checkpoint_button.click()
        editor.versions_button.click()
        await settle(window)
    if stale:
        backend.change_elsewhere(editor.draft.public_id)
    else:
        backend.draft_fail = RuntimeError("private")

    if operation == "checkpoint":
        editor.checkpoint_button.click()
    elif operation == "versions":
        editor.versions_button.click()
    elif operation == "preview":
        editor.versions_list.setCurrentRow(1)
    elif operation == "restore":
        editor.restore_button.click()
        editor.confirm_button.click()
    else:
        editor.save_as_new_requested.emit(DraftEdit(body="Mine"))
    await settle(window)

    assert editor.status.text() == message
    assert editor.body.toPlainText() == "Mine"
    assert editor._fields.isEnabled()
    assert editor.in_conflict is stale


async def test_a_draft_for_an_action_deleted_elsewhere_reloads(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    backend.action_titles.clear()

    window.actions_panel.draft_menu.actions()[2].trigger()
    await finish(window)

    assert "reloaded" in window.status.text()
    assert not window.draft_editor.isVisible()


async def test_draft_operations_without_an_open_draft_do_nothing(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window._checkpoint_draft()
    await window._list_draft_versions()
    await window._show_draft_version(1)
    await window._restore_draft_version(1)
    await window._save_draft_as_new(DraftEdit())
    await window._close_draft(None)
    assert not await window._autosave_draft("00000000-0000-4000-8000-000000000001", DraftEdit())
    window._queue_autosave(DraftEdit())

    assert backend.draft_calls == []
    assert window.draft_writes.idle
    assert window.draft_editor.status.text() == "Save your text as a new draft first."


def test_a_write_queued_without_a_running_loop_is_dropped_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    writes = DraftWrites()
    ran: list[str] = []

    async def save() -> None:
        ran.append("save")

    with caplog.at_level(logging.WARNING, logger="mailbrief.desktop"):
        writes.autosave(save)
        writes.run(save)

    assert ran == [] and writes.idle
    assert caplog.text.count("exception=RuntimeError") == 2
