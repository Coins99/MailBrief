"""Validate and stage recovery before publishing a database; retain the old copy."""

import hashlib
import os
import sqlite3
import struct
import tempfile
import zipfile
import zlib
from contextlib import closing
from functools import cache
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from alembic import command
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, UniqueConstraint, create_engine, inspect
from sqlalchemy.dialects.sqlite import dialect

from mailbrief.domain.backup import MAX_DATABASE_BYTES, MAX_METADATA_BYTES, BackupMetadata
from mailbrief.infra.data_lock import data_directory_lock
from mailbrief.infra.files import sync_directory
from mailbrief.storage.migrate import migration_config, upgrade_database
from mailbrief.storage.tables import Base

_INVALID = "The backup is damaged, incompatible or not a MailBrief database."


class BackupValidationError(ValueError):
    """An archive cannot be safely restored; its content is never included in errors."""


def _untrusted(path: Path) -> sqlite3.Connection:
    """A read-only connection configured for a file that may be hostile."""
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        # Defensive mode blocks features that can corrupt a file, such as writable_schema.
        connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
        for pragma in ("trusted_schema=OFF", "cell_size_check=ON", "mmap_size=0"):
            connection.execute(f"PRAGMA {pragma}")
    except BaseException:
        connection.close()
        raise
    return connection


def _indexes(connection: sqlite3.Connection) -> frozenset[tuple[object, ...]]:
    """Every index on MailBrief's tables by shape; names differ between equal databases."""
    found: set[tuple[object, ...]] = set()
    for table in (*Base.metadata.tables, "alembic_version"):
        for name, unique, origin, partial in connection.execute(
            'SELECT name, "unique", origin, partial FROM pragma_index_list(?)', (table,)
        ):
            # Key columns with their order and collation; an expression has no name.
            columns = tuple(
                connection.execute(
                    'SELECT name, "desc", coll FROM pragma_index_xinfo(?) WHERE key ORDER BY seqno',
                    (name,),
                )
            )
            found.add((table, unique, origin, partial, columns))
    return frozenset(found)


@cache
def _reference_indexes() -> frozenset[tuple[object, ...]]:
    """The indexes of a freshly migrated database, built once per process."""
    with tempfile.TemporaryDirectory(prefix="mailbrief-reference-") as work:
        reference = Path(work) / "reference.sqlite3"
        command.upgrade(migration_config(reference), "head")
        with closing(sqlite3.connect(reference)) as connection:
            return _indexes(connection)


def _check_schema(connection: sqlite3.Connection, path: Path) -> None:
    engine = create_engine("sqlite://", creator=lambda: _untrusted(path))
    try:
        with engine.connect() as reflected:
            inspector = inspect(reflected)
            checks = {
                name: {item["sqltext"].strip() for item in inspector.get_check_constraints(name)}
                for name in Base.metadata.tables
            }
    finally:
        engine.dispose()
    for name, table in Base.metadata.tables.items():
        columns = {
            row[1]: (row[2].upper(), bool(row[3]), bool(row[5]))
            for row in connection.execute("SELECT * FROM pragma_table_info(?)", (name,))
        }
        expected = {
            column.name: (
                column.type.compile(dialect=dialect()).upper(),
                not column.nullable,
                column.primary_key,
            )
            for column in table.columns
        }
        foreign_keys = {
            (row[3], row[2], row[4], row[6])
            for row in connection.execute("SELECT * FROM pragma_foreign_key_list(?)", (name,))
        }
        expected_keys = {
            (column.name, key.column.table.name, key.column.name, key.ondelete)
            for column in table.columns
            for key in column.foreign_keys
        }
        unique = {
            tuple(
                row[2]
                for row in connection.execute("SELECT * FROM pragma_index_info(?)", (index[1],))
            )
            for index in connection.execute("SELECT * FROM pragma_index_list(?)", (name,))
            if index[2] and not index[4]
        }
        expected_unique = {
            tuple(column.name for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        expected_checks = {
            str(constraint.sqltext.compile(dialect=dialect())).strip()
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        }
        if (
            columns != expected
            or foreign_keys != expected_keys
            or not expected_unique <= unique
            or checks[name] != expected_checks
        ):
            raise BackupValidationError(_INVALID)
    # An extra unique index would make ordinary saves fail after a restore.
    if _indexes(connection) != _reference_indexes():
        raise BackupValidationError(_INVALID)


def validate_snapshot(path: Path, *, current_schema: bool = False) -> tuple[str, ...]:
    scripts = ScriptDirectory.from_config(migration_config(path))
    with closing(_untrusted(path)) as connection:
        objects = connection.execute("SELECT name, type FROM sqlite_master").fetchall()
        tables = {name for name, kind in objects if kind == "table"}
        expected = set(Base.metadata.tables) | {"alembic_version"}
        if any(kind in {"trigger", "view"} for _, kind in objects) or (
            tables - expected - {"sqlite_sequence"}
        ):
            raise BackupValidationError(_INVALID)
        rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
        if len(rows) != 1 or rows[0][0] not in {
            script.revision for script in scripts.walk_revisions()
        }:
            raise BackupValidationError(_INVALID)
        revisions = (rows[0][0],)
        is_current = set(revisions) == set(scripts.get_heads())
        if current_schema and not is_current:
            raise BackupValidationError(_INVALID)
        if is_current:
            if not expected <= tables:
                raise BackupValidationError(_INVALID)
            _check_schema(connection, path)
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise BackupValidationError(_INVALID)
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupValidationError(_INVALID)
    return revisions


def _check_zip_directory(source: BinaryIO) -> None:
    source.seek(0, os.SEEK_END)
    size = source.tell()
    if size > MAX_DATABASE_BYTES + MAX_METADATA_BYTES + 1024 * 1024:
        raise BackupValidationError(_INVALID)
    source.seek(max(0, size - 65557))
    tail = source.read(65557)
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or len(tail) - offset < 22:
        raise BackupValidationError(_INVALID)
    _, disk, directory_disk, disk_entries, entries, directory_size, _, comment_size = (
        struct.unpack_from("<4s4H2IH", tail, offset)
    )
    source.seek(max(0, size - len(tail) + offset - 20))
    zip64 = source.read(4) == b"PK\x06\x07"
    if (
        disk != 0
        or directory_disk != 0
        or disk_entries != 2
        or entries != 2
        or directory_size > MAX_METADATA_BYTES
        or len(tail) - offset != 22 + comment_size
        or zip64
    ):
        raise BackupValidationError(_INVALID)
    source.seek(0)


def _stage_archive(archive: Path, database: Path) -> BackupMetadata:
    """Read only the two fixed members; no archive path is ever extracted."""
    try:
        with archive.open("rb") as handle:
            _check_zip_directory(handle)
            return _read_archive(handle, database)
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


def _read_archive(handle: BinaryIO, database: Path) -> BackupMetadata:
    with zipfile.ZipFile(handle) as source:
        members = source.infolist()
        if len(members) != 2 or {member.filename for member in members} != {
            "metadata.json",
            "database.sqlite3",
        }:
            raise BackupValidationError(_INVALID)
        if any(
            member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
            or member.flag_bits & 1
            for member in members
        ):
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


def inspect_backup(archive: Path) -> BackupMetadata:
    """Check structure, bounds, checksum, SQLite integrity and supported revision."""
    with tempfile.TemporaryDirectory(prefix="mailbrief-verify-") as work:
        return _stage_archive(archive, Path(work) / "database.sqlite3")


def _prepare_restored_database(database: Path) -> None:
    upgrade_database(database)
    with closing(sqlite3.connect(database)) as connection:
        # Restoring history must not restore permission to send automatically.
        connection.execute(
            "UPDATE ai_consents SET auto_send_limit=0, auto_send_granted_at_utc=NULL"
        )
        connection.commit()
        # Publish a self-contained file; no staging WAL can be left behind.
        connection.execute("PRAGMA journal_mode=DELETE")
    validate_snapshot(database, current_schema=True)
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
        return restore_backup_holding_lock(archive, database, replace=replace)


def restore_backup_holding_lock(
    archive: Path, database: Path, *, replace: bool, expected: BackupMetadata | None = None
) -> Path | None:
    """Restore for a caller that already holds the data-folder lock (desktop shutdown).

    Preconditions: the caller holds ``data_directory_lock`` for ``database`` and has
    disposed every SQLite connection to it. With ``expected``, the archive must still match
    the metadata the owner reviewed. Returns the pre-restore copy, or None when no database
    existed. Use ``restore_backup`` everywhere else; it takes the lock itself.
    """
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
            sync_directory(database.parent)
        if any(Path(f"{database}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal")):
            raise BackupValidationError(
                "Database sidecar files remain. Close all database clients before restoring."
            )
        if previous is None:
            os.link(staged, database)
        else:
            os.replace(staged, database)
        sync_directory(database.parent)
        return previous
