"""Tests for the initial Alembic schema revision."""

import sqlite3
from contextlib import closing
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

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
    assert version == (ScriptDirectory.from_config(_alembic_config(path)).get_current_head(),)


_ANALYSIS_COLUMNS = (
    "id,message_id,input_hash,provider,model,prompt_version,schema_version,category,summary,"
    "action_required,action_text,deadline_text,deadline_at_utc,confidence,evidence,analyzed_at_utc"
)


def _analyses_sql(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'analyses'"
        ).fetchone()
    return str(row[0])


def test_analysis_rebuild_keeps_rows_references_constraints_and_index(tmp_path: Path) -> None:
    path = tmp_path / "m3.sqlite3"
    config = _alembic_config(path)
    command.upgrade(config, "20260925_0003")
    with closing(sqlite3.connect(path)) as connection:
        account_id = connection.execute(
            "INSERT INTO accounts(provider,provider_account_id,email_address,created_at_utc) "
            "VALUES('gmail','account-1','me@example.com','2026-09-25 00:00:00')"
        ).lastrowid
        message_id = connection.execute(
            "INSERT INTO messages(account_id,provider_message_id,subject,sender_address,"
            "to_recipients_json,received_at_utc,is_read,importance,has_attachments,body_preview,"
            "web_link,rank_reasons_json,synced_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                account_id,
                "message-1",
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
        ).lastrowid
        connection.execute(
            f"INSERT INTO analyses({_ANALYSIS_COLUMNS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                7,
                message_id,
                "hash-1",
                "openai",
                "model-1",
                "prompt-1",
                "1",
                "action",
                "Approve the proposal.",
                1,
                "Approve it.",
                "Friday",
                "2026-09-04 21:00:00.000000",
                0.75,
                "Please approve it by Friday.",
                "2026-09-25 00:00:00.000000",
            ),
        )
        digest_id = connection.execute(
            "INSERT INTO digests(account_id,local_date,timezone_name,status,generated_at_utc) "
            "VALUES(?,'2026-09-25','America/Toronto','complete','2026-09-25 00:00:00')",
            (account_id,),
        ).lastrowid
        connection.execute(
            "INSERT INTO digest_items(digest_id,message_id,analysis_id,position,section) "
            "VALUES(?,?,7,0,'actions')",
            (digest_id, message_id),
        )
        connection.commit()
        before = connection.execute(f"SELECT {_ANALYSIS_COLUMNS} FROM analyses").fetchall()

    upgrade_database(path)

    with closing(sqlite3.connect(path)) as connection:
        after = connection.execute(f"SELECT {_ANALYSIS_COLUMNS} FROM analyses").fetchall()
        precision = connection.execute("SELECT deadline_precision FROM analyses").fetchall()
        references = connection.execute("SELECT analysis_id FROM digest_items").fetchall()
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'analyses'"
            )
        }
    table_sql = _analyses_sql(path)
    assert after == before
    assert precision == [("none",)]
    assert references == [(7,)]
    assert "ck_analyses_confidence_range CHECK (confidence >= 0 AND confidence <= 1)" in table_sql
    assert "ck_analyses_deadline_precision_known CHECK" in table_sql
    assert "uq_analyses_cache_identity UNIQUE" in table_sql
    assert "uq_analyses_cache_key" not in table_sql
    assert "REFERENCES messages (id) ON DELETE CASCADE" in table_sql
    assert "ix_analyses_message_id" in indexes

    command.downgrade(config, "20260925_0003")
    downgraded_sql = _analyses_sql(path)
    assert "uq_analyses_cache_key UNIQUE" in downgraded_sql
    assert "deadline_precision" not in downgraded_sql

    command.upgrade(config, "head")
    assert "uq_analyses_cache_identity UNIQUE" in _analyses_sql(path)
