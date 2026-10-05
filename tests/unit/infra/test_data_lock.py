"""The folder lock works across processes, not only within a Qt application."""

import subprocess
import sys
from pathlib import Path

import pytest

from mailbrief.infra.data_lock import data_directory_lock


def test_other_process_cannot_take_desktop_lock(tmp_path: Path) -> None:
    database = tmp_path / "database.sqlite3"
    script = """
import sys
from PySide6.QtCore import QCoreApplication, QLockFile
application = QCoreApplication([])
application.setApplicationName('MailBrief')
lock = QLockFile(sys.argv[1])
lock.setStaleLockTime(0)
acquired = lock.tryLock(0)
if acquired:
    lock.unlock()
sys.exit(1 if acquired else 0)
"""
    with data_directory_lock(database):
        subprocess.run(
            [sys.executable, "-c", script, str(tmp_path / "desktop.lock")],
            check=True,
            capture_output=True,
            timeout=10,
        )
    assert not (tmp_path / "desktop.lock").exists()


def test_unused_new_directories_are_removed_after_failure(tmp_path: Path) -> None:
    database = tmp_path / "new" / "nested" / "database.sqlite3"
    with pytest.raises(RuntimeError), data_directory_lock(database):
        raise RuntimeError("sign-in failed")
    assert not (tmp_path / "new").exists()
    assert tmp_path.exists()


def test_directory_with_owner_data_is_preserved(tmp_path: Path) -> None:
    database = tmp_path / "new" / "database.sqlite3"
    with data_directory_lock(database):
        database.write_bytes(b"owner data")
    assert database.read_bytes() == b"owner data"


def test_unavailable_volume_fails_without_looping_or_removing_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    removed: list[Path] = []

    def missing(path: Path) -> bool:
        return False

    def unavailable(path: Path, *, parents: bool, exist_ok: bool) -> None:
        raise OSError("unavailable volume")

    def remove(path: Path) -> None:
        removed.append(path)

    with monkeypatch.context() as patches:
        patches.setattr(Path, "exists", missing)
        patches.setattr(Path, "mkdir", unavailable)
        patches.setattr(Path, "rmdir", remove)
        with (
            pytest.raises(OSError, match="unavailable"),
            data_directory_lock(tmp_path / "missing" / "database.sqlite3"),
        ):
            pass
    assert Path(tmp_path.anchor) not in removed
