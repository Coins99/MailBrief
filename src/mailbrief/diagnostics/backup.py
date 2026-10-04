"""Explicit local backup command, independent of Gmail and AI setup."""

import argparse
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from alembic.util import CommandError
from sqlalchemy.exc import SQLAlchemyError

from mailbrief.errors import ConfigurationError
from mailbrief.storage.backup import create_backup
from mailbrief.storage.recovery import inspect_backup, restore_backup


def main(arguments: Sequence[str] | None = None) -> int:
    """Create, verify or restore a backup without signing in or reading credentials."""
    parser = argparse.ArgumentParser(description="Back up or restore MailBrief's local database.")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path, nargs="?")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--verify", action="store_true", help="Validate an archive without restoring."
    )
    mode.add_argument("--restore", action="store_true", help="Restore an archive into a database.")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="With --restore, preserve then replace an existing DB.",
    )
    args = parser.parse_args(arguments)
    if args.verify and args.destination is not None:
        parser.error("--verify takes only the archive path")
    if not args.verify and args.destination is None:
        parser.error("a destination path is required")
    if args.replace and not args.restore:
        parser.error("--replace requires --restore")
    try:
        if args.verify:
            metadata = inspect_backup(args.source)
            print(f"Backup validated: format {metadata.format_version}, {metadata.created_at_utc}.")
            return 0
        if args.restore:
            previous = restore_backup(args.source, args.destination, replace=args.replace)
            print("Database restored. Automatic AI analysis is off; re-enable it explicitly.")
            if previous is not None:
                print(f"Previous database retained at: {previous}")
        else:
            create_backup(args.source, args.destination)
            print(
                "Backup saved. It contains local mail metadata and your writing; keep it private."
            )
    except ConfigurationError as exc:
        print(str(exc))  # Static setup/lock messages; no archive data.
        return 1
    except (OSError, sqlite3.Error, ValueError, SQLAlchemyError, CommandError):
        print(
            "Backup or restore failed. Check the archive, database, permissions and disk space. "
            "Any existing database and backup have been retained."
        )
        return 1
    print("Credentials are excluded. Reconnect accounts and configure the AI key separately.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
