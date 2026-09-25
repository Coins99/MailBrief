"""Tests for the initial Alembic schema revision."""

import sqlite3
from contextlib import closing
from pathlib import Path

from alembic import command
from alembic.config import Config

from mailbrief.storage.database import sqlite_url
from mailbrief.storage.migrate import upgrade_database


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


def test_m2_upgrade_preserves_existing_provider_rows(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    path = tmp_path / "upgrade.sqlite3"
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    command.upgrade(config, "20260831_0001")
    with closing(sqlite3.connect(path)) as connection:
        for provider in ("gmail", "microsoft"):
            cursor = connection.execute(
                "INSERT INTO accounts(provider,provider_account_id,email_address,created_at_utc) "
                "VALUES(?,?,?,?)",
                (provider, provider, "me@example.com", "2026-09-25 00:00:00"),
            )
            connection.execute(
                "INSERT INTO messages(account_id,provider_message_id,subject,sender_address,"
                "to_recipients_json,received_at_utc,is_read,importance,has_attachments,body_preview,"
                "web_link,rank_reasons_json,synced_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cursor.lastrowid,
                    "kept",
                    "Subject",
                    "sender@example.com",
                    "[]",
                    "2026-09-25 00:00:00",
                    0,
                    "normal",
                    0,
                    "",
                    "https://example.com",
                    "[]",
                    "2026-09-25 00:00:00",
                ),
            )
        connection.commit()
    upgrade_database(path)
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute(
            "SELECT accounts.provider,messages.provider_message_id,messages.is_in_inbox "
            "FROM accounts JOIN messages ON accounts.id=messages.account_id "
            "ORDER BY accounts.provider"
        ).fetchall()
    assert rows == [("gmail", "kept", 1), ("microsoft", "kept", 1)]


def _alembic_config(path: Path) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    return config


def _account_columns(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        return {str(row[1]) for row in connection.execute("PRAGMA table_info(accounts)")}


def test_account_addresses_added_to_m2_databases(tmp_path: Path) -> None:
    path = tmp_path / "m2.sqlite3"
    command.upgrade(_alembic_config(path), "20260925_0002")
    assert "account_addresses" not in _account_columns(path)

    upgrade_database(path)

    assert "account_addresses" in _account_columns(path)


def test_account_addresses_migration_tolerates_existing_column(tmp_path: Path) -> None:
    path = tmp_path / "prefilled.sqlite3"
    command.upgrade(_alembic_config(path), "20260925_0002")
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("ALTER TABLE accounts ADD COLUMN account_addresses JSON")
        connection.commit()

    upgrade_database(path)

    assert "account_addresses" in _account_columns(path)
    with closing(sqlite3.connect(path)) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert version == ("20260925_0003",)
