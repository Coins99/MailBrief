"""Portable, consistent database snapshots; never open the credential vault."""

import hashlib
import json
import os
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from mailbrief.domain.backup import MAX_DATABASE_BYTES
from mailbrief.storage.recovery import validate_snapshot

FORMAT_VERSION = 1


def create_backup(database: Path, destination: Path) -> None:
    """Publish a new archive atomically, including committed WAL pages.

    Existing files are never replaced. All intermediate files live next to the
    destination and are removed on failure. The source is opened read-only.
    """
    if destination.exists():
        raise FileExistsError("That file already exists.")
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".mailbrief-backup-") as work:
        snapshot = Path(work) / "database.sqlite3"
        with (
            closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(snapshot)) as target,
        ):
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("The database failed its integrity check.")
            if target.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("The database has invalid relationships.")
            revisions = sorted(
                row[0] for row in target.execute("SELECT version_num FROM alembic_version")
            )
            if not revisions:
                raise ValueError("The database has no schema revision.")
        if snapshot.stat().st_size > MAX_DATABASE_BYTES:
            raise ValueError("The database exceeds the supported backup size.")
        revisions = list(validate_snapshot(snapshot))
        with snapshot.open("rb") as file:
            checksum = hashlib.file_digest(file, "sha256").hexdigest()
        metadata = {
            "format_version": FORMAT_VERSION,
            "created_at_utc": datetime.now(UTC).isoformat(),
            "schema_revisions": revisions,
            "database_sha256": checksum,
            "credentials_included": False,
        }
        archive = Path(work) / "backup.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            output.writestr("metadata.json", json.dumps(metadata, indent=2) + "\n")
            output.write(snapshot, "database.sqlite3")
        with archive.open("r+b") as file:
            os.chmod(archive, 0o600)
            os.fsync(file.fileno())
        # Exclusive creation closes the destination race without replacing anyone's backup.
        # A hard link publishes the finished archive in one operation, on the same volume.
        os.link(archive, destination)
