"""Recovery preserves owner artifacts and refuses unsafe archives before replacement."""

import errno
import hashlib
import json
import os
import sqlite3
import struct
import subprocess
import sys
import textwrap
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from PySide6.QtCore import QLockFile
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Integer, MetaData, UniqueConstraint
from sqlalchemy.dialects.sqlite import dialect
from sqlalchemy.schema import CreateTable

from mailbrief.infra import files
from mailbrief.infra.data_lock import DataInUseError, data_directory_lock
from mailbrief.storage import recovery
from mailbrief.storage.backup import create_backup
from mailbrief.storage.migrate import migration_config
from mailbrief.storage.recovery import (
    BackupValidationError,
    inspect_backup,
    restore_backup,
    restore_backup_holding_lock,
)
from mailbrief.storage.tables import Base

STAMP = "2026-10-04 12:00:00.000000"


@pytest.mark.parametrize(
    "fields",
    [
        (0, 0, 2, 2, 1024 * 1024, 0, 0),
        (0, 0, 65535, 65535, 100, 0, 0),
        (0, 0, 3, 3, 100, 0, 0),
        (1, 0, 2, 2, 100, 0, 0),
        (0, 1, 2, 2, 100, 0, 0),
        (0, 0, 1, 2, 100, 0, 0),
        (0, 0, 2, 2, 100, 0, 1),
    ],
)
def test_archive_directory_is_bounded_before_zip_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fields: tuple[int, ...]
) -> None:
    archive = tmp_path / "hostile.zip"
    archive.write_bytes(struct.pack("<4s4H2IH", b"PK\x05\x06", *fields))

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("Untrusted directory reached the ZIP parser")

    monkeypatch.setattr(zipfile, "ZipFile", fail)
    with pytest.raises(BackupValidationError):
        inspect_backup(archive)


@pytest.mark.parametrize("payload", [b"", b"PK\x05\x06", b"not an archive"])
def test_missing_or_truncated_zip_directory_is_refused(tmp_path: Path, payload: bytes) -> None:
    archive = tmp_path / "hostile.zip"
    archive.write_bytes(payload)
    with pytest.raises(BackupValidationError):
        inspect_backup(archive)


@pytest.mark.parametrize("comment_size", [0, 65535])
def test_zip64_directory_cannot_override_bounds(tmp_path: Path, comment_size: int) -> None:
    archive = tmp_path / "hostile.zip"
    archive.write_bytes(
        b"PK\x06\x07"
        + bytes(16)
        + struct.pack("<4s4H2IH", b"PK\x05\x06", 0, 0, 2, 2, 100, 0, comment_size)
        + bytes(comment_size)
    )
    with pytest.raises(BackupValidationError):
        inspect_backup(archive)


def test_oversized_outer_archive_is_refused_before_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "oversized.zip"
    with archive.open("wb") as file:
        file.seek(1024 * 1024 + 2)
        file.write(b"x")
    monkeypatch.setattr(recovery, "MAX_DATABASE_BYTES", 1)
    monkeypatch.setattr(recovery, "MAX_METADATA_BYTES", 1)
    with pytest.raises(BackupValidationError):
        inspect_backup(archive)


def test_archive_with_comment_remains_supported(archive: Path) -> None:
    with zipfile.ZipFile(archive, "a") as file:
        file.comment = b"owner backup"
    assert inspect_backup(archive).format_version == 1


@pytest.mark.parametrize("method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_unbounded_compression_methods_are_refused_before_member_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: int
) -> None:
    archive = tmp_path / "hostile.zip"
    with zipfile.ZipFile(archive, "w", compression=method) as file:
        file.writestr("metadata.json", "{}")
        file.writestr("database.sqlite3", b"untrusted")

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("Untrusted compression reached a decompressor")

    monkeypatch.setattr(zipfile.ZipFile, "open", fail)
    with pytest.raises(BackupValidationError):
        inspect_backup(archive)


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE VIEW unexpected AS SELECT body FROM drafts",
        "CREATE TRIGGER unexpected AFTER UPDATE ON drafts BEGIN SELECT 1; END",
    ],
)
def test_archive_cannot_introduce_executable_schema(
    archive: Path, tmp_path: Path, sql: str
) -> None:
    hostile = tmp_path / "hostile.sqlite3"
    sample_database(hostile)
    with closing(sqlite3.connect(hostile)) as connection:
        connection.execute(sql)
        connection.commit()
    payload = hostile.read_bytes()
    rewrite_archive(
        archive, payload=payload, metadata={"database_sha256": hashlib.sha256(payload).hexdigest()}
    )
    current = tmp_path / "current.sqlite3"
    sample_database(current, "keep me")
    with pytest.raises(BackupValidationError):
        restore_backup(archive, current, replace=True)
    assert query(current, "SELECT body FROM drafts") == [("keep me",)]


def test_process_exit_before_publication_keeps_both_copies(archive: Path, tmp_path: Path) -> None:
    current = tmp_path / "current.sqlite3"
    sample_database(current, "current writing")
    script = textwrap.dedent("""
        import os, sys
        from pathlib import Path
        from PySide6.QtCore import QCoreApplication
        from mailbrief.paths import configure_qt_identity
        from mailbrief.storage import recovery
        application = QCoreApplication([])
        configure_qt_identity()
        def stop_before_publication(source, destination):
            os._exit(91)
        recovery.os.replace = stop_before_publication
        recovery.restore_backup(Path(sys.argv[1]), Path(sys.argv[2]), replace=True)
    """)
    result = subprocess.run(
        [sys.executable, "-c", script, str(archive), str(current)],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 91
    assert query(current, "SELECT body FROM drafts") == [("current writing",)]
    previous = list(tmp_path.glob("current.sqlite3.pre-restore-*.sqlite3"))
    assert len(previous) == 1
    assert query(previous[0], "SELECT body FROM drafts") == [("current writing",)]
    assert list(tmp_path.glob(".mailbrief-restore-*"))  # Never reused automatically.
    # A dead process lock is recoverable; a fresh attempt uses a new staging folder.
    restore_backup(archive, current, replace=True)
    assert query(current, "SELECT body FROM drafts") == [("owner writing",)]


def sample_database(path: Path, text: str = "owner writing", *, revision: str = "head") -> None:
    """Use the released schema and synthetic owner records, never a real profile."""
    command.upgrade(migration_config(path), revision)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO actions (id,public_id,title,ownership,status,notes,target_date,"
            "created_at_utc,updated_at_utc) VALUES (1,?,'Follow up','mine','open',?,"
            "'2026-10-10',?,?)",
            ("0c5e2c1d-6b8e-4f55-9d0e-2a7f3b9c1e44", text, STAMP, STAMP),
        )
        connection.execute(
            "INSERT INTO action_steps (action_id,position,text) VALUES (1,0,'First step')"
        )
        connection.execute(
            "INSERT INTO drafts (id,public_id,kind,title,body,action_id,action_title,"
            "created_at_utc,updated_at_utc) VALUES (1,?,'note','Note',?,1,'Follow up',?,?)",
            ("20000000-0000-4000-8000-000000000001", text, STAMP, STAMP),
        )
        connection.execute(
            "INSERT INTO draft_versions (draft_id,number,origin,title,body,created_at_utc) "
            "VALUES (1,1,'created','Note',?,?)",
            (text, STAMP),
        )
        connection.execute(
            "INSERT INTO draft_sources (draft_id,provider_message_id,subject,sender_address,"
            "web_link,received_at_utc) VALUES (1,'synthetic','Source','sender@example.com',"
            "'https://mail.google.com/mail/u/0/#inbox/synthetic',?)",
            (STAMP,),
        )
        connection.commit()


def query(path: Path, sql: str) -> list[Any]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(sql).fetchall()


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    database = tmp_path / "source.sqlite3"
    sample_database(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "INSERT INTO owner_preferences (id,time_zone,excluded_senders_json,"
            "updated_at_utc,refresh_on_launch) VALUES (1,'America/Toronto','[]',?,1)",
            (STAMP,),
        )
        connection.execute(
            "INSERT INTO accounts (id,provider,provider_account_id,email_address,created_at_utc) "
            "VALUES (1,'gmail','synthetic','owner@example.com',?)",
            (STAMP,),
        )
        connection.execute(
            "INSERT INTO ai_consents (account_id,provider,disclosure_version,granted_at_utc,"
            "auto_send_limit,auto_send_granted_at_utc) VALUES (1,'groq','1',?,5,?)",
            (STAMP, STAMP),
        )
        connection.commit()
    backup = tmp_path / "backup.zip"
    create_backup(database, backup)
    return backup


def rewrite_archive(
    archive: Path,
    *,
    metadata: dict[str, object] | None = None,
    payload: bytes | None = None,
    extra: str | None = None,
) -> None:
    with zipfile.ZipFile(archive) as source:
        saved_metadata = json.loads(source.read("metadata.json"))
        saved_database = source.read("database.sqlite3")
    if metadata:
        saved_metadata.update(metadata)
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("metadata.json", json.dumps(saved_metadata))
        output.writestr("database.sqlite3", payload if payload is not None else saved_database)
        if extra:
            output.writestr(extra, "untrusted archive text")


def test_inspect_checks_supported_archive(archive: Path) -> None:
    metadata = inspect_backup(archive)
    assert metadata.schema_revisions == ("20260930_0012",)
    assert metadata.credentials_included is False


@pytest.mark.parametrize(
    "defect", ["foreign-key", "unique", "type", "nullable", "check", "weak-check"]
)
def test_matching_column_names_do_not_make_a_valid_schema(
    archive: Path,
    tmp_path: Path,
    defect: str,
) -> None:
    database = tmp_path / "tampered.sqlite3"
    sample_database(database)
    metadata = MetaData()
    for source in Base.metadata.tables.values():
        source.to_metadata(metadata)
    table = metadata.tables["draft_versions"]
    if defect in ("check", "weak-check"):
        for constraint in tuple(table.constraints):
            if isinstance(constraint, CheckConstraint):
                table.constraints.remove(constraint)
        if defect == "weak-check":
            table.append_constraint(CheckConstraint("1=1", name="origin_known"))
    elif defect in ("foreign-key", "unique"):
        cls = ForeignKeyConstraint if defect == "foreign-key" else UniqueConstraint
        for constraint in tuple(table.constraints):
            if isinstance(constraint, cls):
                table.constraints.remove(constraint)
    elif defect == "type":
        table.c.body.type = Integer()
    else:
        table.c.body.nullable = True
    with closing(sqlite3.connect(database)) as connection:
        rows = connection.execute("SELECT * FROM draft_versions").fetchall()
        connection.execute("DROP TABLE draft_versions")
        connection.execute(str(CreateTable(table).compile(dialect=dialect())))
        placeholders = ",".join("?" for _ in table.columns)
        connection.executemany(f"INSERT INTO draft_versions VALUES ({placeholders})", rows)
        if defect in ("check", "weak-check"):
            connection.execute("UPDATE draft_versions SET origin='unknown'")
        connection.commit()
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    payload = database.read_bytes()
    rewrite_archive(
        archive,
        payload=payload,
        metadata={
            "database_sha256": hashlib.sha256(payload).hexdigest(),
        },
    )
    with pytest.raises(BackupValidationError):
        inspect_backup(archive)
    current = tmp_path / "current.sqlite3"
    sample_database(current, "keep me")
    with pytest.raises(BackupValidationError):
        restore_backup(archive, current, replace=True)
    assert query(current, "SELECT body FROM drafts") == [("keep me",)]


def test_reviewed_archive_cannot_be_swapped_before_restore(archive: Path, tmp_path: Path) -> None:
    reviewed = inspect_backup(archive)
    replacement = tmp_path / "replacement.sqlite3"
    sample_database(replacement, "other snapshot")
    archive.unlink()
    create_backup(replacement, archive)
    current = tmp_path / "current.sqlite3"
    sample_database(current, "current writing")
    with data_directory_lock(current), pytest.raises(BackupValidationError, match="changed"):
        restore_backup_holding_lock(archive, current, replace=True, expected=reviewed)
    assert query(current, "SELECT body FROM drafts") == [("current writing",)]
    assert not list(tmp_path.glob("*.pre-restore-*.sqlite3"))


def test_restore_owner_records_and_disable_automatic_analysis(
    archive: Path, tmp_path: Path
) -> None:
    restored = tmp_path / "restored.sqlite3"
    assert restore_backup(archive, restored) is None
    assert query(restored, "SELECT body FROM drafts") == [("owner writing",)]
    assert query(restored, "SELECT body FROM draft_versions") == [("owner writing",)]
    assert query(restored, "SELECT notes,target_date FROM actions") == [
        ("owner writing", "2026-10-10")
    ]
    assert query(restored, "SELECT text FROM action_steps") == [("First step",)]
    assert query(restored, "SELECT provider_message_id FROM draft_sources") == [("synthetic",)]
    assert query(restored, "SELECT time_zone,refresh_on_launch FROM owner_preferences") == [
        ("America/Toronto", 1)
    ]
    assert query(restored, "SELECT auto_send_limit,auto_send_granted_at_utc FROM ai_consents") == [
        (0, None)
    ]
    assert query(tmp_path / "source.sqlite3", "SELECT auto_send_limit FROM ai_consents") == [(5,)]
    assert not list(tmp_path.glob(".mailbrief-restore-*"))
    assert not Path(f"{restored}-wal").exists()


def test_replacement_keeps_old_database(archive: Path, tmp_path: Path) -> None:
    current = tmp_path / "current.sqlite3"
    sample_database(current, "newer owner writing")
    with pytest.raises(FileExistsError):
        restore_backup(archive, current)
    assert query(current, "SELECT body FROM drafts") == [("newer owner writing",)]
    previous = restore_backup(archive, current, replace=True)
    assert previous is not None and previous.is_file()
    assert query(previous, "SELECT body FROM drafts") == [("newer owner writing",)]
    assert query(current, "SELECT body FROM drafts") == [("owner writing",)]


@pytest.mark.parametrize("replace", [False, True])
def test_restore_syncs_published_entries_before_success(
    archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replace: bool
) -> None:
    current = tmp_path / "current.sqlite3"
    if replace:
        sample_database(current, "keep me")
    published: list[str] = []

    def sync(directory: Path) -> None:
        assert directory == tmp_path
        previous = list(directory.glob("current.sqlite3.pre-restore-*.sqlite3"))
        if replace:
            assert len(previous) == 1
            assert query(previous[0], "SELECT body FROM drafts") == [("keep me",)]
        if replace and not published:
            assert query(current, "SELECT body FROM drafts") == [("keep me",)]
            published.append("snapshot")
        else:
            assert query(current, "SELECT body FROM drafts") == [("owner writing",)]
            published.append("restored")

    monkeypatch.setattr(recovery, "sync_directory", sync)
    restore_backup(archive, current, replace=replace)
    assert published == (["snapshot", "restored"] if replace else ["restored"])


def test_snapshot_directory_sync_failure_prevents_replacement(
    archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = tmp_path / "current.sqlite3"
    sample_database(current, "keep me")

    def fail(directory: Path) -> None:
        raise OSError("directory sync failed")

    monkeypatch.setattr(recovery, "sync_directory", fail)
    with pytest.raises(OSError, match="directory sync failed"):
        restore_backup(archive, current, replace=True)
    assert query(current, "SELECT body FROM drafts") == [("keep me",)]
    assert len(list(tmp_path.glob("current.sqlite3.pre-restore-*.sqlite3"))) == 1


@pytest.mark.parametrize("replace", [False, True])
def test_restore_does_not_require_hard_links(
    archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replace: bool
) -> None:
    """exFAT and FAT32 drives have neither hard links nor an exclusive rename."""
    current = tmp_path / "current.sqlite3"
    if replace:
        sample_database(current, "keep me")

    def unsupported(*args: object) -> None:
        raise OSError(errno.ENOTSUP, "Operation not supported")

    def no_links(*args: object) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(files, "_rename_exclusively", unsupported)
    monkeypatch.setattr(os, "link", no_links)
    previous = restore_backup(archive, current, replace=replace)
    monkeypatch.undo()

    assert query(current, "SELECT body FROM drafts") == [("owner writing",)]
    if replace:
        assert previous is not None
        assert query(previous, "SELECT body FROM drafts") == [("keep me",)]
    else:
        assert previous is None
    assert not list(tmp_path.glob(".mailbrief-restore-*"))


@pytest.mark.parametrize("revision", ["20260928_0007", "20260929_0009"])
def test_older_backup_is_upgraded_in_staging(tmp_path: Path, revision: str) -> None:
    original = tmp_path / "older.sqlite3"
    sample_database(original, revision=revision)
    backup = tmp_path / "older.zip"
    create_backup(original, backup)
    restored = tmp_path / "restored.sqlite3"
    restore_backup(backup, restored)
    assert query(restored, "SELECT body FROM drafts") == [("owner writing",)]
    assert query(restored, "SELECT version_num FROM alembic_version") == [("20260930_0012",)]
    assert query(original, "SELECT version_num FROM alembic_version") == [(revision,)]


@pytest.mark.parametrize(
    "metadata",
    [
        {"format_version": 2},
        {"format_version": True},
        {"credentials_included": True},
        {"created_at_utc": "2026-10-04T12:00:00"},
        {"schema_revisions": ["unknown"]},
        {"schema_revisions": []},
        {"database_sha256": "0" * 64},
        {"extra": "untrusted"},
    ],
)
def test_bad_metadata_does_not_replace_database(
    archive: Path, tmp_path: Path, metadata: dict[str, object]
) -> None:
    current = tmp_path / "current.sqlite3"
    sample_database(current, "keep me")
    rewrite_archive(archive, metadata=metadata)
    with pytest.raises(BackupValidationError, match="damaged"):
        restore_backup(archive, current, replace=True)
    assert query(current, "SELECT body FROM drafts") == [("keep me",)]
    assert not list(tmp_path.glob(".mailbrief-restore-*"))
    assert not list(tmp_path.glob("*.pre-restore-*"))


@pytest.mark.parametrize("extra", ["../outside.txt", "credentials.json", "metadata.json"])
def test_unexpected_or_duplicate_archive_members_are_refused(archive: Path, extra: str) -> None:
    if extra == "metadata.json":
        with pytest.warns(UserWarning, match="Duplicate name"):
            rewrite_archive(archive, extra=extra)
    else:
        rewrite_archive(archive, extra=extra)
    with pytest.raises(BackupValidationError):
        inspect_backup(archive)
    assert not (archive.parent.parent / "outside.txt").exists()


def test_corrupt_database_with_matching_checksum_is_refused(archive: Path) -> None:
    payload = b"not sqlite"
    rewrite_archive(
        archive, payload=payload, metadata={"database_sha256": hashlib.sha256(payload).hexdigest()}
    )
    with pytest.raises(BackupValidationError):
        inspect_backup(archive)


@pytest.mark.parametrize("version", ["future_schema", "head", "base", ""])
def test_unknown_schema_is_refused(tmp_path: Path, version: str) -> None:
    database = tmp_path / "source.sqlite3"
    sample_database(database)
    backup = tmp_path / "backup.zip"
    create_backup(database, backup)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE alembic_version SET version_num=?", (version,))
        connection.commit()
    payload = database.read_bytes()
    rewrite_archive(
        backup,
        payload=payload,
        metadata={
            "schema_revisions": [version],
            "database_sha256": hashlib.sha256(payload).hexdigest(),
        },
    )
    with pytest.raises(BackupValidationError):
        restore_backup(backup, tmp_path / "restored.sqlite3")
    assert not (tmp_path / "restored.sqlite3").exists()


@pytest.mark.parametrize("limit", ["MAX_DATABASE_BYTES", "MAX_METADATA_BYTES"])
def test_archive_size_bounds(archive: Path, monkeypatch: pytest.MonkeyPatch, limit: str) -> None:
    monkeypatch.setattr(recovery, limit, 1)
    with pytest.raises(BackupValidationError):
        inspect_backup(archive)


def test_restore_refuses_leftover_sidecars(archive: Path, tmp_path: Path) -> None:
    database = tmp_path / "restored.sqlite3"
    sidecar = Path(f"{database}-wal")
    sidecar.write_bytes(b"old committed pages")
    with pytest.raises(BackupValidationError, match="sidecar"):
        restore_backup(archive, database)
    assert not database.exists()
    assert sidecar.read_bytes() == b"old committed pages"


def test_a_sidecar_refusal_leaves_no_pre_restore_copy(
    archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another client can recreate a sidecar after the current database is preserved."""
    database = tmp_path / "current.sqlite3"
    sample_database(database, "keep me")
    preserve = recovery._preserve_database

    def preserve_then_reopen(current: Path, snapshot: Path) -> None:
        preserve(current, snapshot)
        Path(f"{current}-wal").write_bytes(b"another client")

    monkeypatch.setattr(recovery, "_preserve_database", preserve_then_reopen)
    with pytest.raises(BackupValidationError, match="sidecar"):
        restore_backup(archive, database, replace=True)
    monkeypatch.undo()

    assert not list(tmp_path.glob("current.sqlite3.pre-restore-*.sqlite3"))
    assert not list(tmp_path.glob(".mailbrief-restore-*"))
    Path(f"{database}-wal").unlink()  # The simulated client's file, not SQLite's.
    assert query(database, "SELECT body FROM drafts") == [("keep me",)]


def test_flush_failure_preserves_database(
    archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "current.sqlite3"
    sample_database(database, "keep me")

    def fail(descriptor: int) -> None:
        raise OSError("simulated disk full")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError):
        restore_backup(archive, database, replace=True)
    assert query(database, "SELECT body FROM drafts") == [("keep me",)]
    assert not list(tmp_path.glob(".mailbrief-restore-*"))


def test_fake_current_schema_is_not_restored(archive: Path, tmp_path: Path) -> None:
    database = tmp_path / "fake.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE alembic_version(version_num TEXT)")
        connection.execute("INSERT INTO alembic_version VALUES ('20260930_0012')")
        connection.commit()
    backup = archive
    payload = database.read_bytes()
    rewrite_archive(
        backup,
        payload=payload,
        metadata={
            "database_sha256": hashlib.sha256(payload).hexdigest(),
        },
    )
    with pytest.raises(BackupValidationError):
        restore_backup(backup, tmp_path / "restored.sqlite3")


def test_migration_failure_preserves_destination(
    archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = tmp_path / "current.sqlite3"
    sample_database(current, "keep me")

    def fail(path: Path) -> None:
        raise OSError("simulated interrupted upgrade")

    monkeypatch.setattr(recovery, "upgrade_database", fail)
    with pytest.raises(OSError):
        restore_backup(archive, current, replace=True)
    assert query(current, "SELECT body FROM drafts") == [("keep me",)]
    assert not list(tmp_path.glob(".mailbrief-restore-*"))


def test_replacement_failure_keeps_both_old_copies(
    archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = tmp_path / "current.sqlite3"
    sample_database(current, "keep me")

    def fail(source: object, destination: object) -> None:
        raise OSError("simulated read-only destination")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        restore_backup(archive, current, replace=True)
    assert query(current, "SELECT body FROM drafts") == [("keep me",)]
    [previous] = tmp_path.glob("*.pre-restore-*")
    assert query(previous, "SELECT body FROM drafts") == [("keep me",)]


def test_restore_refuses_running_desktop(archive: Path, tmp_path: Path) -> None:
    lock = QLockFile(str(tmp_path / "desktop.lock"))
    lock.setStaleLockTime(0)
    assert lock.tryLock(0)
    try:
        with pytest.raises(DataInUseError):
            restore_backup(archive, tmp_path / "restored.sqlite3")
    finally:
        lock.unlock()
    restore_backup(archive, tmp_path / "restored.sqlite3")


def test_restore_refuses_another_sqlite_client(archive: Path, tmp_path: Path) -> None:
    current = tmp_path / "current.sqlite3"
    sample_database(current, "keep me")
    with closing(sqlite3.connect(current)) as client:
        client.execute("PRAGMA journal_mode=WAL")
        client.execute("BEGIN")
        client.execute("SELECT * FROM drafts").fetchall()
        with pytest.raises(sqlite3.OperationalError):
            restore_backup(archive, current, replace=True)
    assert query(current, "SELECT body FROM drafts") == [("keep me",)]


def test_lock_released_on_failure(tmp_path: Path) -> None:
    path = tmp_path / "database.sqlite3"
    with pytest.raises(RuntimeError), data_directory_lock(path):
        raise RuntimeError("failed")
    with data_directory_lock(path):
        assert not path.exists()


def test_no_replace_permission_survives_destination_race(
    archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "restored.sqlite3"
    prepare = recovery._prepare_restored_database

    def create_destination(staged: Path) -> None:
        prepare(staged)
        database.write_bytes(b"only copy, created during validation")

    monkeypatch.setattr(recovery, "_prepare_restored_database", create_destination)
    with pytest.raises(FileExistsError):
        restore_backup(archive, database)
    assert database.read_bytes() == b"only copy, created during validation"


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE UNIQUE INDEX extra ON drafts(title)",
        "CREATE INDEX extra ON drafts(body)",
        "CREATE INDEX extra ON drafts(lower(title))",
        "CREATE INDEX extra ON drafts(title) WHERE deleted_at_utc IS NULL",
        "DROP INDEX ix_actions_status",
        "CREATE INDEX extra ON actions(status)",  # Same shape as ix_actions_status.
    ],
)
def test_archive_indexes_must_match_the_migrated_schema(
    archive: Path, tmp_path: Path, sql: str
) -> None:
    """An extra unique index would make ordinary saves fail after restore."""
    hostile = tmp_path / "hostile.sqlite3"
    sample_database(hostile)
    with closing(sqlite3.connect(hostile)) as connection:
        connection.execute(sql)
        connection.commit()
    payload = hostile.read_bytes()
    rewrite_archive(
        archive, payload=payload, metadata={"database_sha256": hashlib.sha256(payload).hexdigest()}
    )
    current = tmp_path / "current.sqlite3"
    sample_database(current, "keep me")
    with pytest.raises(BackupValidationError):
        restore_backup(archive, current, replace=True)
    assert query(current, "SELECT body FROM drafts") == [("keep me",)]


def test_untrusted_files_open_read_only_and_defensive(archive: Path, tmp_path: Path) -> None:
    database = tmp_path / "source.sqlite3"
    with closing(recovery._untrusted(database)) as connection:
        assert connection.getconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE)
        assert connection.execute("PRAGMA trusted_schema").fetchone() == (0,)
        assert connection.execute("PRAGMA cell_size_check").fetchone() == (1,)
        assert connection.execute("PRAGMA mmap_size").fetchone() == (0,)
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM drafts")


def test_restore_refuses_a_symlinked_database(archive: Path, tmp_path: Path) -> None:
    real = tmp_path / "real.sqlite3"
    sample_database(real, "keep me")
    link = tmp_path / "database.sqlite3"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("Creating symlinks needs Developer Mode on Windows")
    before = hashlib.sha256(real.read_bytes()).hexdigest()
    with pytest.raises(BackupValidationError, match="regular database path"):
        restore_backup(archive, link, replace=True)
    assert hashlib.sha256(real.read_bytes()).hexdigest() == before
    assert link.is_symlink()
    assert not list(tmp_path.glob("*.pre-restore-*"))
