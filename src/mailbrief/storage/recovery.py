"""Validate and stage recovery before publishing a database; retain the old copy."""

import hashlib
import os
import sqlite3
import tempfile
import zipfile
import zlib
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from alembic.script import ScriptDirectory
from pydantic import ValidationError

from mailbrief.domain.backup import MAX_DATABASE_BYTES, MAX_METADATA_BYTES, BackupMetadata
from mailbrief.infra.data_lock import data_directory_lock
from mailbrief.storage.migrate import migration_config, upgrade_database
from mailbrief.storage.tables import Base

_INVALID = "The backup is damaged, incompatible or not a MailBrief database."


class BackupValidationError(ValueError):
    """An archive cannot be safely restored; its content is never included in errors."""


def _database_revisions(path: Path, *, current_schema: bool = False) -> tuple[str, ...]:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA trusted_schema=OFF")
        objects = connection.execute("SELECT name, type FROM sqlite_master").fetchall()
        if any(kind in {"trigger", "view"} for _, kind in objects):
            raise BackupValidationError(_INVALID)
        tables = {name for name, kind in objects if kind == "table"}
        expected = set(Base.metadata.tables) | {"alembic_version"}
        if tables - expected - {"sqlite_sequence"}:
            raise BackupValidationError(_INVALID)
        if current_schema:
            if not expected <= tables:
                raise BackupValidationError(_INVALID)
            for name, table in Base.metadata.tables.items():
                # Names come only from checked-in metadata, never archive content.
                columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')}
                if columns != set(table.columns.keys()):
                    raise BackupValidationError(_INVALID)
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise BackupValidationError(_INVALID)
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupValidationError(_INVALID)
        rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
        if len(rows) != 1 or not isinstance(rows[0][0], str):
            raise BackupValidationError(_INVALID)
        revisions = (rows[0][0],)
    scripts = ScriptDirectory.from_config(migration_config(path))
    if revisions[0] not in {script.revision for script in scripts.walk_revisions()}:
        raise BackupValidationError(_INVALID)
    if current_schema and set(revisions) != set(scripts.get_heads()):
        raise BackupValidationError(_INVALID)
    return revisions


def validate_snapshot(database: Path) -> tuple[str, ...]:
    """Refuse unknown objects/revisions and enforce current columns on current snapshots."""
    revisions = _database_revisions(database)
    scripts = ScriptDirectory.from_config(migration_config(database))
    if set(revisions) == set(scripts.get_heads()):
        _database_revisions(database, current_schema=True)
    return revisions


def _stage_archive(archive: Path, database: Path) -> BackupMetadata:
    """Read only the two fixed members; no archive path is ever extracted."""
    try:
        with zipfile.ZipFile(archive) as source:
            members = source.infolist()
            if len(members) != 2 or {member.filename for member in members} != {
                "metadata.json",
                "database.sqlite3",
            }:
                raise BackupValidationError(_INVALID)
            if source.getinfo("metadata.json").file_size > MAX_METADATA_BYTES:
                raise BackupValidationError(_INVALID)
            size = source.getinfo("database.sqlite3").file_size
            if not 0 < size <= MAX_DATABASE_BYTES:
                raise BackupValidationError(_INVALID)
            metadata = BackupMetadata.model_validate_json(source.read("metadata.json"))
            digest = hashlib.sha256()
            count = 0
            with source.open("database.sqlite3") as incoming, database.open("xb") as output:
                while chunk := incoming.read(1024 * 1024):
                    count += len(chunk)
                    if count > MAX_DATABASE_BYTES:
                        raise BackupValidationError(_INVALID)
                    digest.update(chunk)
                    output.write(chunk)
            if count != size or digest.hexdigest() != metadata.database_sha256:
                raise BackupValidationError(_INVALID)
        if validate_snapshot(database) != metadata.schema_revisions:
            raise BackupValidationError(_INVALID)
        return metadata
    except (
        zipfile.BadZipFile,
        ValidationError,
        sqlite3.Error,
        RuntimeError,
        NotImplementedError,
        EOFError,
        zlib.error,
    ):
        raise BackupValidationError(_INVALID) from None


def inspect_backup(archive: Path) -> BackupMetadata:
    """Check structure, bounds, checksum, SQLite integrity and supported revision."""
    with tempfile.TemporaryDirectory(prefix="mailbrief-verify-") as work:
        return _stage_archive(archive, Path(work) / "database.sqlite3")


def _prepare_restored_database(database: Path) -> None:
    upgrade_database(database)
    _database_revisions(database, current_schema=True)
    with closing(sqlite3.connect(database)) as connection:
        # Restoring history must not restore permission to send automatically.
        connection.execute(
            "UPDATE ai_consents SET auto_send_limit=0, auto_send_granted_at_utc=NULL"
        )
        connection.commit()
        # Publish a self-contained file; no staging WAL can be left behind.
        connection.execute("PRAGMA journal_mode=DELETE")
    _database_revisions(database, current_schema=True)
    with database.open("r+b") as file:
        os.chmod(database, 0o600)
        os.fsync(file.fileno())


def _preserve_database(database: Path, snapshot: Path) -> None:
    with (
        closing(sqlite3.connect(database.resolve().as_uri() + "?mode=rw", uri=True)) as source,
        closing(sqlite3.connect(snapshot)) as target,
    ):
        # Refuse other SQLite clients rather than replaying old WAL into the replacement.
        source.execute("PRAGMA busy_timeout=0")
        source.execute("PRAGMA journal_mode=DELETE")
        source.backup(target)
        target.execute("PRAGMA journal_mode=DELETE")
    with snapshot.open("r+b") as file:
        os.chmod(snapshot, 0o600)
        os.fsync(file.fileno())


def restore_backup(archive: Path, database: Path, *, replace: bool = False) -> Path | None:
    """Restore offline, retaining a unique pre-restore snapshot on replacement.

    The desktop and diagnostic commands must be closed. Other SQLite clients must
    also be closed. A failed validation or migration never replaces the destination.
    """
    with data_directory_lock(database):
        return _restore_backup_locked(archive, database, replace=replace)


def _restore_backup_locked(
    archive: Path, database: Path, *, replace: bool, expected: BackupMetadata | None = None
) -> Path | None:
    """Desktop shutdown only: the caller must hold its data-folder lock and close SQLite."""
    if database.is_symlink():
        raise BackupValidationError("Restore requires a regular database path.")
    database = database.resolve()
    if archive.resolve() == database:
        raise BackupValidationError("The backup and database must be different files.")
    if database.exists() and not replace:
        raise FileExistsError("That database already exists; replacement must be explicit.")
    with tempfile.TemporaryDirectory(dir=database.parent, prefix=".mailbrief-restore-") as work:
        staged = Path(work) / "database.sqlite3"
        metadata = _stage_archive(archive, staged)
        if expected is not None and metadata != expected:
            raise BackupValidationError("The selected backup changed. Verify and review it again.")
        _prepare_restored_database(staged)
        previous = None
        if database.exists():
            if not replace:
                raise FileExistsError("That database already exists; replacement must be explicit.")
            snapshot = Path(work) / "previous.sqlite3"
            _preserve_database(database, snapshot)
            previous = database.with_name(f"{database.name}.pre-restore-{uuid4().hex}.sqlite3")
            os.link(snapshot, previous)
        if any(Path(f"{database}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal")):
            raise BackupValidationError(
                "Database sidecar files remain. Close all database clients before restoring."
            )
        if previous is None:
            os.link(staged, database)
        else:
            os.replace(staged, database)
        return previous
