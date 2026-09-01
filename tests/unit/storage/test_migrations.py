"""Tests for the initial Alembic schema revision."""

import sqlite3
from contextlib import closing
from pathlib import Path

from alembic import command
from alembic.config import Config

from mailbrief.storage.database import sqlite_url


def table_names(database_path: Path) -> set[str]:
    """Read all SQLite table names from a migrated database."""
    with closing(sqlite3.connect(database_path)) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row[0]) for row in rows}


def test_initial_migration_upgrades_and_downgrades(tmp_path: Path) -> None:
    repository_root = Path(__file__).resolve().parents[3]
    database_path = tmp_path / "migrated.sqlite3"
    config = Config(str(repository_root / "alembic.ini"))
    config.set_main_option("script_location", str(repository_root / "migrations"))
    config.set_main_option("sqlalchemy.url", sqlite_url(database_path))

    command.upgrade(config, "head")

    assert {
        "accounts",
        "messages",
        "analyses",
        "digests",
        "digest_items",
        "sync_runs",
        "alembic_version",
    }.issubset(table_names(database_path))

    command.check(config)

    command.downgrade(config, "base")

    assert table_names(database_path) <= {"alembic_version"}
