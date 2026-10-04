"""Data controls never mutate before review and never expose arbitrary exception text."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog, QMessageBox
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from mailbrief.storage.database import Database
from mailbrief.storage.recovery import BackupValidationError
from mailbrief.storage.tables import DraftTable, MessageTable
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
    await dialog.verify()
    assert "Valid backup" in dialog.status.text()
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
    QTest.keyClick(box, Qt.Key.Key_Escape)
    assert await task is False
    assert dialog._confirmation is None


async def test_picker_refuses_data_folder_and_cancellation(
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
    data_window._draft_save_failed = True
    await data_window._open_data()
    assert "failed draft save" in data_window.status.text()
    data_window._draft_save_failed = False
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
