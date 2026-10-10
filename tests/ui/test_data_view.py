"""Data controls never mutate before review and never expose arbitrary exception text."""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from mailbrief.storage.database import Database
from mailbrief.storage.recovery import BackupValidationError, inspect_backup
from mailbrief.storage.tables import DraftTable, MessageTable
from mailbrief.ui import data_view
from mailbrief.ui.deadline_text import moment_text
from mailbrief.ui.main_window import MainWindow
from tests.ui.test_workflow import FakeBackend
from tests.unit.services import test_data as fixtures

owner_database = fixtures.owner_database


@pytest.fixture
async def data_window(
    qtbot: QtBot, owner_database: Database, tmp_path: Path
) -> AsyncIterator[MainWindow]:
    window = MainWindow(FakeBackend(), database_path=tmp_path / "owner.sqlite3")
    qtbot.addWidget(window)
    window.show()
    window._ready = True
    await window._open_data()
    try:
        yield window
    finally:
        window.close()
        await window.shutdown()


async def test_data_dialog_offline_accounts_and_keyboard(
    data_window: MainWindow,
    qtbot: QtBot,
) -> None:
    dialog = data_window.data_dialog
    assert dialog is not None and dialog.isVisible()
    assert dialog.account.count() == 1
    assert dialog.account.currentData() == 1
    assert dialog.account.accessibleName()
    assert not data_window._automatic_active and data_window._dialog_open()
    dialog.set_busy(True)
    assert not any(button.isEnabled() for button in dialog.buttons)
    dialog.set_busy(False)
    assert all(button.isEnabled() for button in dialog.buttons)
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    assert not dialog.isVisible()


async def test_backup_verify_and_portable_export(
    data_window: MainWindow,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialog = data_window.data_dialog
    assert dialog is not None
    archive = tmp_path / "backup.zip"
    choose = AsyncMock(return_value=archive)
    monkeypatch.setattr(dialog, "choose", choose)
    await dialog.backup()
    assert archive.is_file()
    dialog.zone = ZoneInfo("Asia/Tokyo")
    dialog.today = lambda: date(2026, 10, 10)
    await dialog.verify()
    created = inspect_backup(archive).created_at_utc
    assert dialog.status.text() == f"Valid backup from {moment_text(created, dialog.zone)}."
    dialog.today = lambda: date(2027, 1, 4)  # In another year the year is named.
    await dialog.verify()
    assert f", {created.astimezone(dialog.zone).year}," in dialog.status.text()
    choose.return_value = tmp_path / "writing.json"
    await dialog.export()
    payload = json.loads((tmp_path / "writing.json").read_text(encoding="utf-8"))
    assert payload["records"]["actions"][0]["title"] == "Follow up"
    assert payload["records"]["drafts"][0]["body"] == "owner writing"
    choose.return_value = tmp_path / "diagnostics.json"
    await dialog.diagnostics()
    safe = (tmp_path / "diagnostics.json").read_text(encoding="utf-8")
    assert "app_version" in safe
    assert "owner writing" not in safe and "owner@example" not in safe
    assert str(tmp_path) not in safe


async def test_cleanup_cancel_then_apply(
    data_window: MainWindow,
    owner_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialog = data_window.data_dialog
    assert dialog is not None
    dialog.kind.setCurrentIndex(1)
    confirm = AsyncMock(return_value=False)
    refresh = AsyncMock()
    monkeypatch.setattr(dialog, "confirm", confirm)
    dialog.refresh = refresh
    await dialog.cleanup()
    async with owner_database.session() as session:
        assert len(list(await session.scalars(select(MessageTable)))) == 2
    assert "2 cached messages" in confirm.call_args.args[0]
    confirm.return_value = True
    await dialog.cleanup()
    async with owner_database.session() as session:
        assert list(await session.scalars(select(MessageTable))) == []
        assert await session.get(DraftTable, 1) is not None
    refresh.assert_awaited_once()


async def test_restore_only_requested_after_verified_confirmation(
    data_window: MainWindow,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialog = data_window.data_dialog
    assert dialog is not None
    archive = tmp_path / "backup.zip"
    monkeypatch.setattr(dialog, "choose", AsyncMock(return_value=archive))
    await dialog.backup()
    confirm = AsyncMock(return_value=False)
    monkeypatch.setattr(dialog, "confirm", confirm)
    request = Mock()
    dialog.request_restore = request
    await dialog.restore()
    request.assert_not_called()
    confirm.return_value = True
    await dialog.restore()
    assert request.call_count == 1 and request.call_args.args[0] == archive
    archive.write_bytes(b"invalid")
    with pytest.raises(BackupValidationError):
        await dialog.restore()
    assert request.call_count == 1


async def test_native_confirmation_defaults_cancel_and_closes_cleanly(
    data_window: MainWindow,
    qtbot: QtBot,
) -> None:
    dialog = data_window.data_dialog
    assert dialog is not None
    task = asyncio.create_task(dialog.confirm("Synthetic review"))
    await asyncio.sleep(0)
    box = dialog._confirmation
    assert box is not None
    assert box.defaultButton() == box.button(QMessageBox.StandardButton.Cancel)
    # On macOS, Escape uses animateClick, which clicks Cancel from a Qt timer; this test's
    # asyncio loop never runs it, so wait for the result in a Qt event loop. Elsewhere it
    # clicks at once, which the context manager also catches.
    with qtbot.waitSignal(box.finished, timeout=5000):
        QTest.keyClick(box, Qt.Key.Key_Escape)
    assert await task is False
    assert dialog._confirmation is None


@pytest.fixture
def qt_file_dialogs(qapp: QApplication) -> Iterator[None]:
    """Qt's own file dialog. A native macOS one ignores selectFile() until it has run, so
    the test could never pick a file."""
    attribute = Qt.ApplicationAttribute.AA_DontUseNativeDialogs
    before = QApplication.testAttribute(attribute)
    QApplication.setAttribute(attribute, True)
    try:
        yield
    finally:
        QApplication.setAttribute(attribute, before)


async def test_picker_refuses_data_folder_and_cancellation(
    qt_file_dialogs: None,
    data_window: MainWindow,
    tmp_path: Path,
) -> None:
    dialog = data_window.data_dialog
    assert dialog is not None
    task = asyncio.create_task(dialog.choose("Synthetic export", "JSON (*.json)", save=True))
    await asyncio.sleep(0)
    assert dialog._picker is not None
    dialog._picker.selectFile(str(tmp_path / "new.json"))
    dialog._picker.done(QDialog.DialogCode.Accepted)
    with pytest.raises(ValueError, match="outside MailBrief"):
        await task
    task = asyncio.create_task(dialog.choose("Synthetic verify", "ZIP (*.zip)", save=False))
    await asyncio.sleep(0)
    dialog.reject()
    assert await task is None


async def test_backup_inside_data_folder_shows_the_specific_refusal(
    qt_file_dialogs: None,
    data_window: MainWindow,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialog = data_window.data_dialog
    assert dialog is not None
    log_failure = Mock()
    monkeypatch.setattr(data_view, "log_failure", log_failure)
    task = asyncio.create_task(dialog.guarded(dialog.backup))
    await asyncio.sleep(0)
    assert dialog._picker is not None
    dialog._picker.selectFile(str(tmp_path / "backup.zip"))
    dialog._picker.done(QDialog.DialogCode.Accepted)
    await task
    assert dialog.status.text() == "Choose a destination outside MailBrief's data folder."
    assert not (tmp_path / "backup.zip").exists()
    log_failure.assert_not_called()


async def test_failed_operations_hide_private_errors(data_window: MainWindow) -> None:
    dialog = data_window.data_dialog
    assert dialog is not None
    await dialog.guarded(AsyncMock(side_effect=OSError("SECRET MAIL BODY")))
    assert "SECRET" not in dialog.status.text()
    await dialog.guarded(AsyncMock(side_effect=FileExistsError("SECRET FILE")))
    assert "already exists" in dialog.status.text() and "SECRET" not in dialog.status.text()
    await dialog.guarded(AsyncMock(side_effect=BackupValidationError("SECRET ZIP")))
    assert "damaged" in dialog.status.text() and "SECRET" not in dialog.status.text()


async def test_cleanup_reports_commit_even_if_view_refresh_fails(
    data_window: MainWindow,
    owner_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialog = data_window.data_dialog
    assert dialog is not None
    dialog.kind.setCurrentIndex(1)
    monkeypatch.setattr(dialog, "confirm", AsyncMock(return_value=True))
    dialog.refresh = AsyncMock(side_effect=OSError("PRIVATE REFRESH ERROR"))
    await dialog.cleanup()
    assert "Cleanup completed" in dialog.status.text()
    assert "PRIVATE" not in dialog.status.text()
    async with owner_database.session() as session:
        assert list(await session.scalars(select(MessageTable))) == []


async def test_data_waits_for_editors_and_pending_writes(
    data_window: MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialog = data_window.data_dialog
    assert dialog is not None
    dialog.reject()
    data_window.action_editor.show()
    await data_window._open_data()
    assert not dialog.isVisible()
    data_window.action_editor.force_close()
    waiting = asyncio.Event()
    data_window.draft_writes.run(waiting.wait)
    await data_window._open_data()
    assert not dialog.isVisible()
    waiting.set()
    await data_window.draft_writes.drain()
    data_window._unsaved_drafts.add("failed")
    await data_window._open_data()
    assert "failed draft save" in data_window.status.text()
    data_window._unsaved_drafts.clear()
    await data_window._open_data()
    assert dialog.isVisible()


async def test_deleted_writing_cleanup_and_decision_warning(
    data_window: MainWindow,
    owner_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialog = data_window.data_dialog
    assert dialog is not None
    async with owner_database.transaction() as session:
        draft = await session.get(DraftTable, 1)
        assert draft is not None
        draft.deleted_at_utc = datetime.now(UTC) - timedelta(days=100)
    confirm = AsyncMock(return_value=False)
    monkeypatch.setattr(dialog, "confirm", confirm)
    dialog.kind.setCurrentIndex(3)
    await dialog.cleanup()
    assert "1 deleted drafts" in confirm.call_args.args[0]
    assert "cannot be undone" in confirm.call_args.args[0]
    dialog.kind.setCurrentIndex(4)
    await dialog.cleanup()
    assert "may appear again" in confirm.call_args.args[0]


async def test_data_available_when_storage_cannot_open(qtbot: QtBot, tmp_path: Path) -> None:
    path = tmp_path / "broken.sqlite3"
    path.write_bytes(b"damaged database")
    window = MainWindow(FakeBackend(), database_path=path)
    qtbot.addWidget(window)
    try:
        assert window.data_button.isEnabled()
        await window._open_data()
        dialog = window.data_dialog
        assert dialog is not None and dialog.isVisible()
        dialog.set_busy(False)
        assert not dialog.buttons[-1].isEnabled()
        assert dialog.buttons[2].isEnabled()
    finally:
        window.close()
        await window.shutdown()


async def test_closing_during_verification_cannot_reopen_confirmation(
    data_window: MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mailbrief.storage.recovery import inspect_backup

    dialog = data_window.data_dialog
    assert dialog is not None
    archive = dialog.path.parent / "backup.zip"
    monkeypatch.setattr(dialog, "choose", AsyncMock(return_value=archive))
    await dialog.backup()
    metadata = inspect_backup(archive)
    started, release = asyncio.Event(), asyncio.Event()

    async def verify(*args: object, **kwargs: object) -> object:
        started.set()
        await release.wait()
        return metadata

    monkeypatch.setattr(asyncio, "to_thread", verify)
    task = asyncio.create_task(dialog.restore())
    await started.wait()
    dialog.reject()
    release.set()
    await task
    assert dialog._confirmation is None and data_window.pending_restore is None
    assert not dialog.isVisible()


async def test_cleanup_refresh_does_not_reopen_closed_dialog(data_window: MainWindow) -> None:
    dialog = data_window.data_dialog
    assert dialog is not None
    dialog.reject()
    await data_window._refresh_data()
    assert not dialog.isVisible()


async def test_recovery_help_does_not_create_unavailable_database(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing.sqlite3"
    window = MainWindow(FakeBackend(), database_path=path)
    qtbot.addWidget(window)
    try:
        await window._open_data()
        assert not path.exists()
        assert window.data_dialog is not None and window.data_dialog.isVisible()
    finally:
        window.close()
        await window.shutdown()


async def test_saving_another_draft_does_not_clear_failed_writing(
    data_window: MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.ui.test_draft_editor import make_draft

    first = make_draft(public_id="20000000-0000-4000-8000-000000000001")
    second = make_draft(public_id="20000000-0000-4000-8000-000000000002")
    save = AsyncMock(side_effect=[OSError("disk full"), second.model_copy(update={"revision": 2})])
    monkeypatch.setattr(data_window.backend, "autosave_draft", save)
    data_window.draft_editor.load(first, data_window.zone)
    assert not await data_window._autosave_draft(first.public_id, first.content())
    data_window.draft_editor.load(second, data_window.zone)
    assert await data_window._autosave_draft(second.public_id, second.content())
    assert data_window._unsaved_drafts == {first.public_id}
    data_window.draft_editor.load(first, data_window.zone)
    monkeypatch.setattr(data_window.backend, "save_draft_as_new", AsyncMock(return_value=second))
    await data_window._save_draft_as_new(first.content())
    assert not data_window._unsaved_drafts
