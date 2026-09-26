"""Tests for async database lifecycle and schema behavior."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import StatementError

from mailbrief.storage.database import Database, sqlite_url
from mailbrief.storage.tables import AccountTable, Base, MessageTable


def test_sqlite_url_uses_absolute_posix_path(tmp_path: Path) -> None:
    database_path = tmp_path / "nested" / "mailbrief.sqlite3"

    url = sqlite_url(database_path)

    assert url.startswith("sqlite+aiosqlite:///")
    assert database_path.resolve().as_posix() in url


def test_metadata_contains_all_initial_tables() -> None:
    assert set(Base.metadata.tables) == {
        "accounts",
        "messages",
        "analyses",
        "digests",
        "digest_items",
        "sync_runs",
        "ai_consents",
    }
    assert "body" not in Base.metadata.tables["messages"].columns


@pytest.mark.asyncio
async def test_transaction_commits_and_restores_utc(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "data" / "mailbrief.sqlite3")
    await database.create_schema_for_tests()

    try:
        async with database.transaction() as session:
            session.add(
                AccountTable(
                    provider="microsoft",
                    provider_account_id="account-1",
                    email_address="taylor@example.com",
                )
            )

        async with database.session() as session:
            account = (await session.execute(select(AccountTable))).scalar_one()

        assert account.provider_account_id == "account-1"
        assert account.created_at_utc.tzinfo is UTC
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "mailbrief.sqlite3")
    await database.create_schema_for_tests()

    try:
        with pytest.raises(RuntimeError, match="stop"):
            async with database.transaction() as session:
                session.add(
                    AccountTable(
                        provider="microsoft",
                        provider_account_id="account-1",
                        email_address="taylor@example.com",
                    )
                )
                raise RuntimeError("stop")

        async with database.session() as session:
            count = await session.scalar(select(func.count()).select_from(AccountTable))

        assert count == 0
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_sqlite_foreign_keys_cascade_messages(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "mailbrief.sqlite3")
    await database.create_schema_for_tests()

    try:
        async with database.transaction() as session:
            account = AccountTable(
                provider="microsoft",
                provider_account_id="account-1",
                email_address="taylor@example.com",
            )
            session.add(account)
            await session.flush()
            session.add(
                MessageTable(
                    account_id=account.id,
                    provider_message_id="message-1",
                    subject="Subject",
                    sender_address="alex@example.com",
                    to_recipients_json=[],
                    received_at_utc=datetime(2026, 8, 31, 14, 30, tzinfo=UTC),
                    is_read=False,
                    importance="normal",
                    has_attachments=False,
                    body_preview="Preview",
                    web_link="https://outlook.office.com/mail/id/message-1",
                    rank_reasons_json=[],
                )
            )

        async with database.transaction() as session:
            await session.execute(delete(AccountTable))

        async with database.session() as session:
            count = await session.scalar(select(func.count()).select_from(MessageTable))

        assert count == 0
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_naive_database_timestamp_is_rejected(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "mailbrief.sqlite3")
    await database.create_schema_for_tests()

    try:
        with pytest.raises(StatementError, match="time zone"):
            async with database.transaction() as session:
                session.add(
                    AccountTable(
                        provider="microsoft",
                        provider_account_id="account-1",
                        email_address="taylor@example.com",
                        created_at_utc=datetime(2026, 8, 31, 14, 30),
                    )
                )
    finally:
        await database.dispose()
