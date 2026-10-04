"""Snapshot consistency and failure preservation, using synthetic data only."""

import hashlib
import json
import os
import sqlite3
import zipfile
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from mailbrief.storage import backup
from mailbrief.storage.backup import create_backup
from mailbrief.storage.migrate import upgrade_database


def test_backup_includes_wal_and_preserves_source(tmp_path: Path) -> None:
    database = tmp_path / "source.sqlite3"
    destination = tmp_path / "backup.zip"
    upgrade_database(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "INSERT INTO drafts (public_id,kind,title,body,created_at_utc,updated_at_utc) "
            "VALUES ('20000000-0000-4000-8000-000000000001','note','Note',"
            "'synthetic owner writing','2026-10-04 12:00:00','2026-10-04 12:00:00')"
        )
        connection.commit()
        create_backup(database, destination)
        assert connection.execute("SELECT body FROM drafts").fetchone() == (
            "synthetic owner writing",
        )
    with zipfile.ZipFile(destination) as archive:
        assert set(archive.namelist()) == {"metadata.json", "database.sqlite3"}
        metadata = json.loads(archive.read("metadata.json"))
        payload = archive.read("database.sqlite3")
    assert metadata["format_version"] == 1
    assert metadata["credentials_included"] is False
    assert metadata["schema_revisions"] == ["20260930_0012"]
    assert metadata["database_sha256"] == hashlib.sha256(payload).hexdigest()
    restored = tmp_path / "snapshot.sqlite3"
    restored.write_bytes(payload)
    with closing(sqlite3.connect(restored)) as connection:
        assert connection.execute("SELECT body FROM drafts").fetchone() == (
            "synthetic owner writing",
        )


def test_existing_destination_is_preserved(tmp_path: Path) -> None:
    destination = tmp_path / "backup.zip"
    destination.write_bytes(b"only copy")
    with pytest.raises(FileExistsError):
        create_backup(tmp_path / "missing.sqlite3", destination)
    assert destination.read_bytes() == b"only copy"


@pytest.mark.parametrize("contents", [None, b"not a database"])
def test_invalid_source_leaves_no_backup(tmp_path: Path, contents: bytes | None) -> None:
    source = tmp_path / "source.sqlite3"
    if contents is not None:
        source.write_bytes(contents)
    with pytest.raises(sqlite3.Error):
        create_backup(source, tmp_path / "backup.zip")
    assert not (tmp_path / "backup.zip").exists()
    assert not list(tmp_path.glob(".mailbrief-backup-*"))
    assert source.exists() is (contents is not None)


def test_real_schema_backup(tmp_path: Path) -> None:
    database = tmp_path / "database.sqlite3"
    upgrade_database(database)
    create_backup(database, tmp_path / "backup.zip")
    with zipfile.ZipFile(tmp_path / "backup.zip") as archive:
        metadata = json.loads(archive.read("metadata.json"))
    assert metadata["schema_revisions"] == ["20260930_0012"]


def test_publication_failure_cleans_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "database.sqlite3"
    upgrade_database(database)

    def fail(source: object, destination: object) -> None:
        raise OSError("simulated disk or permission failure")

    monkeypatch.setattr(os, "link", fail)
    with pytest.raises(OSError):
        create_backup(database, tmp_path / "backup.zip")
    assert not (tmp_path / "backup.zip").exists()
    assert not list(tmp_path.glob(".mailbrief-backup-*"))
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_oversized_snapshot_refused_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "database.sqlite3"
    upgrade_database(database)
    monkeypatch.setattr(backup, "MAX_DATABASE_BYTES", 1)
    real_connect = sqlite3.connect

    class NoCopyConnection(sqlite3.Connection):
        def backup(
            self,
            target: sqlite3.Connection,
            *,
            pages: int = -1,
            progress: Callable[[int, int, int], object] | None = None,
            name: str = "main",
            sleep: float = 0.250,
        ) -> None:
            raise AssertionError("Oversized database must be refused before copying")

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return real_connect(*args, **kwargs, factory=NoCopyConnection)

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(ValueError, match="supported backup size"):
        create_backup(database, tmp_path / "backup.zip")
    assert not (tmp_path / "backup.zip").exists()
    assert not list(tmp_path.glob(".mailbrief-backup-*"))


def test_growth_during_backup_aborts_and_cleans_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "database.sqlite3"
    upgrade_database(database)
    monkeypatch.setattr(backup, "MAX_DATABASE_BYTES", database.stat().st_size)
    real_connect = sqlite3.connect

    class GrowingConnection(sqlite3.Connection):
        def backup(
            self,
            target: sqlite3.Connection,
            *,
            pages: int = -1,
            progress: Callable[[int, int, int], object] | None = None,
            name: str = "main",
            sleep: float = 0.250,
        ) -> None:
            assert pages == 1 and progress is not None
            progress(sqlite3.SQLITE_OK, 1000000, 1000001)
            raise AssertionError("Growth should abort the copy")

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return real_connect(*args, **kwargs, factory=GrowingConnection)

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(ValueError, match="supported backup size"):
        create_backup(database, tmp_path / "backup.zip")
    assert not (tmp_path / "backup.zip").exists()
    assert not list(tmp_path.glob(".mailbrief-backup-*"))


def test_unexpected_storage_objects_are_not_backed_up(tmp_path: Path) -> None:
    database = tmp_path / "database.sqlite3"
    upgrade_database(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE developer_credentials (value TEXT)")
        connection.execute("INSERT INTO developer_credentials VALUES ('SYNTHETIC SECRET')")
        connection.commit()
    with pytest.raises(ValueError) as caught:
        create_backup(database, tmp_path / "backup.zip")
    assert "SYNTHETIC SECRET" not in str(caught.value)
    assert not (tmp_path / "backup.zip").exists()
    assert not list(tmp_path.glob(".mailbrief-backup-*"))
