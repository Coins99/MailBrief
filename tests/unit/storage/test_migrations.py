"""Tests for the Alembic schema revisions."""

import asyncio
import re
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from mailbrief.storage.database import Database, sqlite_url
from mailbrief.storage.migrate import upgrade_database


def test_packaged_migrations_work_away_from_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "bundle"
    source = Path(__file__).resolve().parents[3] / "migrations"
    shutil.copytree(source, bundle / "migrations", ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    profile = tmp_path / "profile" / "mailbrief.sqlite3"
    upgrade_database(profile)
    upgrade_database(profile)
    assert "ai_consents" in table_names(profile)
    assert not list(bundle.glob("*.sqlite3"))


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


def test_pre_upgrade_backup_includes_wal_and_survives_failed_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "mailbrief.sqlite3"
    with closing(sqlite3.connect(path)) as source:
        source.execute("PRAGMA journal_mode=WAL")
        source.execute("CREATE TABLE saved (value TEXT)")
        source.execute("INSERT INTO saved VALUES ('synthetic metadata')")
        source.commit()

        def fail_upgrade(config: Config, target: str) -> None:
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS partial_upgrade (value TEXT)")
                connection.commit()
            raise RuntimeError("Migration failed")

        monkeypatch.setattr(command, "upgrade", fail_upgrade)
        with pytest.raises(RuntimeError, match="Migration failed"):
            upgrade_database(path)
        (backup,) = tmp_path.glob("*.pre-upgrade-*.sqlite3")
        with closing(sqlite3.connect(backup)) as restored:
            assert restored.execute("SELECT value FROM saved").fetchone() == ("synthetic metadata",)
            assert "partial_upgrade" not in table_names(backup)
        original = backup.read_bytes()
        with pytest.raises(RuntimeError):
            upgrade_database(path)
        assert backup.read_bytes() == original
        assert len(list(tmp_path.glob("*.pre-upgrade-*.sqlite3"))) == 2


def test_current_and_unknown_schema_do_not_create_backup(tmp_path: Path) -> None:
    path = tmp_path / "mailbrief.sqlite3"
    upgrade_database(path)
    upgrade_database(path)
    assert not list(tmp_path.glob("*.pre-upgrade-*.sqlite3"))
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("UPDATE alembic_version SET version_num='future_version'")
        connection.commit()
    before = path.read_bytes()
    from alembic.util import CommandError

    with pytest.raises(CommandError):
        upgrade_database(path)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.pre-upgrade-*.sqlite3"))


def test_backup_failure_prevents_upgrade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock

    path = tmp_path / "mailbrief.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE saved (value TEXT)")
    upgrade = Mock()
    monkeypatch.setattr(command, "upgrade", upgrade)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", Mock(side_effect=OSError("Disk full")))
    with pytest.raises(OSError):
        upgrade_database(path)
    upgrade.assert_not_called()


_ACTION_TABLES = {
    "actions",
    "action_suggestions",
    "action_steps",
    "action_sources",
    "suggestion_decisions",
}
_OLD_ROWS = ("accounts", "messages", "analyses", "digests", "digest_items")


def _seed_before_actions(path: Path) -> None:
    """One account, message, analysis, digest and digest item at revision 0004."""
    with closing(sqlite3.connect(path)) as connection:
        account_id = connection.execute(
            "INSERT INTO accounts(provider,provider_account_id,email_address,created_at_utc,"
            "account_addresses) VALUES('gmail','account-1','me@example.com',"
            "'2026-09-25 00:00:00','[\"me@example.com\"]')"
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
            f"INSERT INTO analyses({_ANALYSIS_COLUMNS},deadline_precision,deadline_date,"
            "deadline_timezone) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                7,
                message_id,
                "hash-1",
                "groq",
                "model-1",
                "prompt-1",
                "5",
                "action",
                "Approve the proposal.",
                1,
                "Approve it.",
                "Friday",
                None,
                0.75,
                "Please approve it by Friday.",
                "2026-09-25 00:00:00.000000",
                "date",
                "2026-09-04",
                "America/Toronto",
            ),
        )
        digest_id = connection.execute(
            "INSERT INTO digests(account_id,local_date,timezone_name,status,generated_at_utc,"
            "sync_complete,shortlisted_count,analyzed_count,reused_count,failed_count,"
            "skipped_count) VALUES(?,'2026-09-25','America/Toronto','complete',"
            "'2026-09-25 00:00:00',1,1,1,0,0,0)",
            (account_id,),
        ).lastrowid
        connection.execute(
            "INSERT INTO digest_items(digest_id,message_id,analysis_id,position,section) "
            "VALUES(?,?,7,0,'actions')",
            (digest_id, message_id),
        )
        connection.commit()


def _rows(path: Path, tables: Sequence[str]) -> dict[str, list[tuple[object, ...]]]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in tables
        }


def _foreign_keys(path: Path, table: str) -> set[tuple[str, str, str, str]]:
    """(target table, column, target column, on_delete) for each foreign key."""
    with closing(sqlite3.connect(path)) as connection:
        return {
            (str(row[2]), str(row[3]), str(row[4]), str(row[6]))
            for row in connection.execute(f"PRAGMA foreign_key_list('{table}')")
        }


def _use_action_tables(path: Path) -> None:
    """Insert one row into each action table, relying on the server defaults."""
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        action_id = connection.execute(
            "INSERT INTO actions(public_id,title,ownership,status,created_at_utc,updated_at_utc) "
            "VALUES('0c5e2c1d-6b8e-4f55-9d0e-2a7f3b9c1e44','Approve it','mine','open',"
            "'2026-09-27 00:00:00','2026-09-27 00:00:00')"
        ).lastrowid
        connection.execute(
            "INSERT INTO action_suggestions(analysis_id,position,title,ownership,steps_json,"
            "fingerprint) VALUES(7,0,'Approve it','mine','[]',?)",
            ("a" * 64,),
        )
        connection.execute(
            "INSERT INTO action_steps(action_id,position,text) VALUES(?,0,'Read it')",
            (action_id,),
        )
        connection.execute(
            "INSERT INTO action_sources(action_id,message_id,provider_message_id,subject,"
            "sender_address,web_link,received_at_utc) VALUES(?,1,'message-1','Subject',"
            "'sender@example.com','https://example.com','2026-09-25 00:00:00')",
            (action_id,),
        )
        connection.execute(
            "INSERT INTO suggestion_decisions(message_id,fingerprint,decision,action_id,"
            "decided_at_utc) VALUES(1,?,'accepted',?,'2026-09-27 00:00:00')",
            ("a" * 64, action_id),
        )
        defaults = connection.execute(
            "SELECT actions.deadline_precision,actions.notes,actions.revision,action_steps.done "
            "FROM actions JOIN action_steps ON action_steps.action_id=actions.id"
        ).fetchone()
        for rejected in (
            "UPDATE actions SET status='archived'",
            "UPDATE actions SET revision=0",
            "UPDATE action_suggestions SET ownership='theirs'",
            "UPDATE suggestion_decisions SET decision='maybe'",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                connection.execute(rejected)
        connection.commit()
    assert defaults == ("none", "", 1, 0)


def test_action_tables_upgrade_and_downgrade_keep_existing_rows(tmp_path: Path) -> None:
    path = tmp_path / "m4.sqlite3"
    config = _alembic_config(path)
    command.upgrade(config, "20260925_0004")
    _seed_before_actions(path)
    before = _rows(path, _OLD_ROWS)

    command.upgrade(config, "head")

    assert table_names(path) >= _ACTION_TABLES
    assert _rows(path, _OLD_ROWS) == before
    assert _foreign_keys(path, "actions") == set()
    assert _foreign_keys(path, "action_suggestions") == {
        ("analyses", "analysis_id", "id", "CASCADE")
    }
    assert _foreign_keys(path, "action_steps") == {("actions", "action_id", "id", "CASCADE")}
    assert _foreign_keys(path, "action_sources") == {
        ("actions", "action_id", "id", "CASCADE"),
        ("messages", "message_id", "id", "SET NULL"),
    }
    assert _foreign_keys(path, "suggestion_decisions") == {
        ("messages", "message_id", "id", "CASCADE"),
        ("actions", "action_id", "id", "SET NULL"),
    }
    _use_action_tables(path)

    command.downgrade(config, "20260925_0004")

    assert not _ACTION_TABLES & table_names(path)
    assert _rows(path, _OLD_ROWS) == before


def _schema(path: Path) -> dict[str, dict[str, object]]:
    """Each table's columns, foreign keys, unique column sets, indexes and CHECK names."""
    shape: dict[str, dict[str, object]] = {}
    with closing(sqlite3.connect(path)) as connection:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
            )
        ]
        for table in tables:
            unique: set[frozenset[str]] = set()
            indexes: set[tuple[str, tuple[str, ...]]] = set()
            for index in connection.execute(f"PRAGMA index_list('{table}')").fetchall():
                columns = tuple(
                    str(row[2]) for row in connection.execute(f"PRAGMA index_info('{index[1]}')")
                )
                if index[3] == "u":
                    unique.add(frozenset(columns))
                elif index[3] == "c":
                    indexes.add((str(index[1]), columns))
            (sql,) = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()
            shape[table] = {
                # Name, type, NOT NULL, server default and primary key position.
                "columns": {
                    tuple(row[1:]) for row in connection.execute(f"PRAGMA table_info('{table}')")
                },
                "foreign_keys": _foreign_keys(path, table),
                "unique": unique,
                "indexes": indexes,
                "checks": set(re.findall(r"CONSTRAINT (ck_\w+) CHECK", str(sql))),
            }
    return shape


async def _create_schema(path: Path) -> None:
    database = Database.from_path(path)
    try:
        await database.create_schema_for_tests()
    finally:
        await database.dispose()


def test_head_schema_matches_the_orm(tmp_path: Path) -> None:
    migrated = tmp_path / "migrated.sqlite3"
    created = tmp_path / "created.sqlite3"
    command.upgrade(_alembic_config(migrated), "head")
    asyncio.run(_create_schema(created))

    migrated_schema, created_schema = _schema(migrated), _schema(created)

    assert set(migrated_schema) >= _ACTION_TABLES
    assert set(migrated_schema) == set(created_schema)
    for table in sorted(created_schema):
        assert migrated_schema[table] == created_schema[table], table
    assert created_schema["actions"]["checks"] == {
        "ck_actions_status_known",
        "ck_actions_ownership_known",
        "ck_actions_deadline_precision_known",
        "ck_actions_revision_positive",
    }
    assert created_schema["action_suggestions"]["unique"] == {
        frozenset({"analysis_id", "position"}),
        frozenset({"analysis_id", "fingerprint"}),
    }
