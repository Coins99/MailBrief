"""Explicit CLI modes, redacted failures and recovery locking."""

from pathlib import Path

import pytest

from mailbrief.diagnostics import backup, gmail
from mailbrief.infra.data_lock import data_directory_lock
from tests.unit.storage.test_recovery import sample_database


def test_create_verify_and_restore(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    database = tmp_path / "source.sqlite3"
    archive = tmp_path / "backup.zip"
    restored = tmp_path / "restored.sqlite3"
    sample_database(database)
    assert backup.main([str(database), str(archive)]) == 0
    assert backup.main(["--verify", str(archive)]) == 0
    assert backup.main(["--restore", str(archive), str(restored)]) == 0
    assert restored.is_file()
    output = capsys.readouterr().out
    assert "owner writing" not in output
    assert "Automatic AI analysis is off" in output


def test_failure_does_not_print_archive_or_exception_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(source: Path, destination: Path) -> None:
        raise OSError("private owner writing")

    monkeypatch.setattr(backup, "create_backup", fail)
    assert backup.main([str(tmp_path / "source"), str(tmp_path / "archive")]) == 1
    assert "private owner writing" not in capsys.readouterr().out


@pytest.mark.parametrize("arguments", [[], ["--verify", "a", "b"], ["a"], ["a", "b", "--replace"]])
def test_invalid_options_are_refused(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        backup.main(arguments)
    assert error.value.code == 2


def test_gmail_diagnostic_refuses_locked_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "mailbrief.sqlite3"
    with data_directory_lock(database):
        assert gmail.main(["preferences", "show", "--database", str(database)]) == 5
    assert "busy or unavailable" in capsys.readouterr().out
    assert not database.exists()


def test_gmail_diagnostic_releases_lock_after_failure(tmp_path: Path) -> None:
    database = tmp_path / "missing.sqlite3"
    assert gmail.main(["briefs", "list", "--database", str(database)]) == 0
    with data_directory_lock(database):
        assert not database.exists()
