"""The desktop restores only after shutdown, while retaining its process lock."""

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
from pytestqt.qtbot import QtBot

from mailbrief import app
from mailbrief.domain.backup import BackupMetadata
from mailbrief.paths import AppPaths
from mailbrief.storage.backup import create_backup
from mailbrief.storage.recovery import inspect_backup, restore_backup_holding_lock
from mailbrief.ui.main_window import MainWindow
from tests.ui.test_workflow import FakeBackend
from tests.unit.storage.test_recovery import query, sample_database


@pytest.mark.parametrize("failure", [None, "no-previous", "archive", "swapped", "shutdown"])
def test_restore_follows_shutdown_and_holds_lock(
    qtbot: QtBot,
    themed: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    source = tmp_path / "source.sqlite3"
    sample_database(source, "backup writing")
    archive = tmp_path / "backup.zip"
    create_backup(source, archive)
    metadata = inspect_backup(archive)
    if failure == "archive":
        archive.write_bytes(b"invalid archive")
    elif failure == "swapped":
        replacement = tmp_path / "replacement.sqlite3"
        sample_database(replacement, "other writing")
        archive.unlink()
        create_backup(replacement, archive)
    current = tmp_path / "current.sqlite3"
    sample_database(current, "current writing")

    class Backend(FakeBackend):
        async def close(self) -> None:
            with closing(sqlite3.connect(current)) as connection:
                connection.execute("UPDATE actions SET notes='shutdown finished'")
                connection.commit()
            await super().close()
            if failure == "shutdown":
                raise OSError("PRIVATE FAILURE")

    backend = Backend()
    monkeypatch.setattr(app, "DesktopRuntime", lambda path: backend)
    monkeypatch.setattr(
        AppPaths,
        "from_qt",
        lambda: AppPaths(
            data_dir=tmp_path,
            database_path=current,
            microsoft_token_cache_path=tmp_path / "unused.bin",
        ),
    )
    information = Mock()
    warning = Mock()
    monkeypatch.setattr(QMessageBox, "information", information)
    monkeypatch.setattr(QMessageBox, "warning", warning)
    real_restore = restore_backup_holding_lock
    restores: list[Path | None] = []

    def restore(
        path: Path, database: Path, *, replace: bool, expected: BackupMetadata | None = None
    ) -> Path | None:
        assert backend.closed
        contender = QLockFile(str(tmp_path / "desktop.lock"))
        assert not contender.tryLock(0)
        assert query(current, "SELECT notes FROM actions") == [("shutdown finished",)]
        if failure == "no-previous":
            current.unlink()  # Removed while the app ran: nothing to retain.
        result = real_restore(path, database, replace=replace, expected=expected)
        restores.append(result)
        return result

    monkeypatch.setattr(app, "restore_backup_holding_lock", restore)

    def request_restore() -> None:
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, MainWindow) and widget.isVisible():
                widget._request_restore(archive, metadata)
                return
        # Applying the theme can delay the first show; keep looking until it appears.
        QTimer.singleShot(10, request_restore)

    QTimer.singleShot(100, request_restore)
    try:
        assert app.main([]) == 0
        assert backend.closed
        if failure is None:
            assert len(restores) == 1 and restores[0] is not None
            assert query(current, "SELECT notes FROM actions") == [("backup writing",)]
            assert query(restores[0], "SELECT notes FROM actions") == [("shutdown finished",)]
            information.assert_called_once()
            assert f"Previous database: {restores[0]}" in information.call_args.args[2]
        elif failure == "no-previous":
            assert restores == [None]
            assert query(current, "SELECT notes FROM actions") == [("backup writing",)]
            information.assert_called_once()
            assert "Previous database" not in information.call_args.args[2]
        else:
            assert restores == []
            assert query(current, "SELECT notes FROM actions") == [("shutdown finished",)]
            warning.assert_called_once()
            text = warning.call_args.args[2]
            assert "PRIVATE" not in text
            if failure == "archive":
                assert text.startswith("The backup is damaged, incompatible")
            elif failure == "swapped":
                assert text.startswith("The selected backup changed. Verify and review it again.")
            if failure != "shutdown":
                assert text.endswith("The current database was kept.")
        contender = QLockFile(str(tmp_path / "desktop.lock"))
        assert contender.tryLock(0)
        contender.unlock()
    finally:
        application = QApplication.instance()
        assert isinstance(application, QApplication)
        application.setQuitOnLastWindowClosed(True)
        asyncio.set_event_loop(None)
