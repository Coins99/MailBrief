"""Retention and portable exports use only synthetic owner data."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from mailbrief.services import data
from mailbrief.services.data import (
    CleanupChangedError,
    CleanupKind,
    CleanupRequest,
    apply_cleanup,
    export_writing,
    preview_cleanup,
)
from mailbrief.storage.database import Database
from mailbrief.storage.tables import (
    AccountTable,
    ActionTable,
    DraftTable,
    DraftVersionTable,
    MessageTable,
    SuggestionDecisionTable,
)
from tests.unit.storage.test_recovery import sample_database

NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)


@pytest.fixture
async def owner_database(tmp_path: Path) -> AsyncIterator[Database]:
    path = tmp_path / "owner.sqlite3"
    await asyncio.to_thread(sample_database, path)
    database = Database.from_path(path)
    async with database.transaction() as session:
        session.add(
            AccountTable(
                id=1,
                provider="gmail",
                provider_account_id="test",
                email_address="owner@example.invalid",
                created_at_utc=NOW,
            )
        )
        await session.flush()
        for identifier, received in ((1, NOW - timedelta(days=100)), (2, NOW)):
            session.add(
                MessageTable(
                    id=identifier,
                    account_id=1,
                    provider_message_id=str(identifier),
                    subject="PRIVATE CACHE",
                    sender_address="sender@example.invalid",
                    received_at_utc=received,
                    importance="normal",
                    web_link="https://mail.google.com/",
                )
            )
        session.add(
            SuggestionDecisionTable(
                provider="gmail",
                provider_account_id="test",
                provider_message_id="1",
                fingerprint="f" * 64,
                decision="dismissed",
                decided_at_utc=NOW,
            )
        )
    try:
        yield database
    finally:
        await database.dispose()


async def test_export_all_writing_and_versions_without_mail_cache(
    owner_database: Database,
    tmp_path: Path,
) -> None:
    async with owner_database.transaction() as session:
        draft = await session.get(DraftTable, 1)
        assert draft is not None
        draft.deleted_at_utc = NOW
    target = tmp_path / "writing.json"
    await export_writing(owner_database, target)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["format"] == "mailbrief-writing" and payload["version"] == 1
    records = payload["records"]
    assert set(records) == set(data.EXPORT_TABLES)
    assert records["actions"][0]["notes"] == "owner writing"
    assert records["drafts"][0]["deleted_at_utc"] is not None
    assert records["draft_versions"][0]["body"] == "owner writing"
    assert records["draft_sources"][0]["subject"] == "Source"
    assert "PRIVATE CACHE" not in target.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        await export_writing(owner_database, target)


@pytest.mark.parametrize("kind", [CleanupKind.CACHE, CleanupKind.ACCOUNT])
async def test_cache_and_account_deletion_preserve_owner_records(
    owner_database: Database,
    kind: CleanupKind,
) -> None:
    preview = await preview_cleanup(owner_database, CleanupRequest(kind, 1))
    assert preview.messages == 2 and preview.drafts == 0 and preview.actions == 0
    await apply_cleanup(owner_database, preview)
    async with owner_database.session() as session:
        assert list(await session.scalars(select(MessageTable))) == []
        assert await session.get(ActionTable, 1) is not None
        assert await session.get(DraftTable, 1) is not None
        assert len(list(await session.scalars(select(SuggestionDecisionTable)))) == 1
        assert (await session.get(AccountTable, 1) is None) == (kind == CleanupKind.ACCOUNT)


async def test_age_cleanup_keeps_recent_mail(owner_database: Database) -> None:
    request = CleanupRequest(CleanupKind.CACHE, 1, NOW - timedelta(days=90))
    preview = await preview_cleanup(owner_database, request)
    assert preview.messages == 1
    await apply_cleanup(owner_database, preview)
    async with owner_database.session() as session:
        assert [row.id for row in await session.scalars(select(MessageTable))] == [2]


async def test_permanent_delete_only_old_soft_deleted_writing(owner_database: Database) -> None:
    async with owner_database.transaction() as session:
        draft = await session.get(DraftTable, 1)
        assert draft is not None
        draft.deleted_at_utc = NOW - timedelta(days=100)
    preview = await preview_cleanup(
        owner_database, CleanupRequest(CleanupKind.DELETED, before=NOW - timedelta(days=90))
    )
    assert preview.drafts == 1 and preview.versions == 1 and preview.actions == 0
    await apply_cleanup(owner_database, preview)
    async with owner_database.session() as session:
        assert await session.get(DraftTable, 1) is None
        assert list(await session.scalars(select(DraftVersionTable))) == []
        assert await session.get(ActionTable, 1) is not None


async def test_preview_refuses_changes_and_rolls_back(owner_database: Database) -> None:
    preview = await preview_cleanup(owner_database, CleanupRequest(CleanupKind.ACCOUNT, 1))
    async with owner_database.transaction() as session:
        action = await session.get(ActionTable, 1)
        assert action is not None
        action.notes = "Changed since review"
        action.revision += 1
    with pytest.raises(CleanupChangedError):
        await apply_cleanup(owner_database, preview)
    async with owner_database.session() as session:
        assert await session.get(AccountTable, 1) is not None
        assert len(list(await session.scalars(select(MessageTable)))) == 2


async def test_failed_deletion_rolls_back(
    owner_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    preview = await preview_cleanup(owner_database, CleanupRequest(CleanupKind.CACHE, 1))

    async def fail_after_deletes(session: AsyncSession) -> None:
        await session.flush()
        raise OSError("disk full")

    monkeypatch.setattr(AsyncSession, "commit", fail_after_deletes)
    with pytest.raises(OSError):
        await apply_cleanup(owner_database, preview)
    async with owner_database.session() as session:
        assert len(list(await session.scalars(select(MessageTable)))) == 2


async def test_orphaned_decisions_only_explicitly_forgotten(owner_database: Database) -> None:
    async with owner_database.transaction() as session:
        session.add(
            SuggestionDecisionTable(
                provider="gmail",
                provider_account_id="gone",
                provider_message_id="2",
                fingerprint="a" * 64,
                decision="dismissed",
                decided_at_utc=NOW - timedelta(days=100),
            )
        )
    preview = await preview_cleanup(
        owner_database,
        CleanupRequest(CleanupKind.ORPHAN_DECISIONS, before=NOW - timedelta(days=90)),
    )
    assert preview.decisions == 1
    await apply_cleanup(owner_database, preview)
    async with owner_database.session() as session:
        rows = list(await session.scalars(select(SuggestionDecisionTable)))
        assert len(rows) == 1 and rows[0].provider_account_id == "test"


@pytest.mark.parametrize(
    "kind,account,before",
    [
        (CleanupKind.CACHE, None, None),
        (CleanupKind.ACCOUNT, 1, NOW),
        (CleanupKind.DELETED, None, None),
        (CleanupKind.DELETED, 1, NOW),
        (CleanupKind.CACHE, 1, datetime(2026, 1, 1)),
    ],
)
def test_invalid_requests_refused(
    kind: CleanupKind, account: int | None, before: datetime | None
) -> None:
    with pytest.raises(ValueError):
        CleanupRequest(kind, account, before)


async def test_missing_or_dormant_account_refused(owner_database: Database) -> None:
    async with owner_database.transaction() as session:
        account = await session.get(AccountTable, 1)
        assert account is not None
        account.provider = "microsoft"
    for identifier in (1, 99):
        with pytest.raises(ValueError):
            await preview_cleanup(owner_database, CleanupRequest(CleanupKind.ACCOUNT, identifier))


async def test_large_cleanup_streams_preview_and_deletes_in_batches(
    owner_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with owner_database.transaction() as session:
        for identifier in range(3, 304):
            session.add(
                MessageTable(
                    account_id=1,
                    provider_message_id=str(identifier),
                    subject="Cached",
                    sender_address="sender@example.invalid",
                    received_at_utc=NOW,
                    importance="normal",
                    web_link="https://mail.google.com/",
                )
            )

    async def forbid_materializing(*args: object) -> None:
        raise AssertionError("Preview must not materialize all database records.")

    monkeypatch.setattr(data, "_records", forbid_materializing)
    preview = await preview_cleanup(owner_database, CleanupRequest(CleanupKind.CACHE, 1))
    assert preview.messages == 303
    await apply_cleanup(owner_database, preview)
    async with owner_database.session() as session:
        assert list(await session.scalars(select(MessageTable))) == []


async def test_nonrevisioned_cache_changes_invalidate_preview(owner_database: Database) -> None:
    preview = await preview_cleanup(owner_database, CleanupRequest(CleanupKind.CACHE, 1))
    async with owner_database.transaction() as session:
        message = await session.get(MessageTable, 1)
        assert message is not None
        message.body_preview = "Changed after review"
    with pytest.raises(CleanupChangedError):
        await apply_cleanup(owner_database, preview)
