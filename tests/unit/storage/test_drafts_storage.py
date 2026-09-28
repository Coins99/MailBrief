"""Draft storage: chunked loading, ORM-only pruning, and summaries that never read versions."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.drafts import DraftEdit, DraftKind, DraftVersionOrigin
from mailbrief.storage.database import MAX_SQLITE_BATCH_SIZE, Database
from mailbrief.storage.drafts import DraftRepository
from mailbrief.storage.tables import DraftSourceTable, DraftTable, DraftVersionTable

AT = datetime(2026, 9, 28, 13, tzinfo=UTC)


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.from_path(tmp_path / "drafts.sqlite3")
    await database.create_schema_for_tests()
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    async with database.session() as session:
        yield session


async def add_drafts(session: AsyncSession, count: int) -> list[DraftTable]:
    repository = DraftRepository(session)
    rows: list[DraftTable] = []
    for index in range(count):
        row = await repository.add_draft(
            DraftTable(
                public_id=f"00000000-0000-4000-8000-{index:012d}",
                kind=DraftKind.NOTE.value,
                body=f"Note {index}",
                created_at_utc=AT,
                updated_at_utc=AT,
                revision=1,
            )
        )
        session.add(
            DraftSourceTable(
                draft_id=row.id,
                message_id=None,
                provider_message_id=f"m-{index}",
                subject=f"Subject {index}",
                sender_address="alex@example.com",
                web_link="https://mail.google.com/mail/u/0/#all/m",
                received_at_utc=AT,
            )
        )
        rows.append(row)
    await session.commit()
    return rows


async def test_loading_more_drafts_than_one_batch_keeps_every_source(
    session: AsyncSession,
) -> None:
    rows = await add_drafts(session, MAX_SQLITE_BATCH_SIZE + 50)

    drafts = await DraftRepository(session).load(rows)

    assert len(drafts) == 150
    assert [draft.sources[0].provider_message_id for draft in drafts] == [
        f"m-{index}" for index in range(150)
    ]
    assert all(not draft.sources[0].available for draft in drafts)


async def test_pruning_deletes_version_rows_through_the_session(session: AsyncSession) -> None:
    (row,) = await add_drafts(session, 1)
    repository = DraftRepository(session)
    for index in range(5):
        await repository.add_version(
            row.id, DraftVersionOrigin.EDITED, DraftEdit(body=f"v{index}"), AT
        )
    oldest = await repository.get_version(row.id, 1)
    assert oldest is not None

    assert await repository.prune_versions(row.id, 3) == 2
    await session.flush()

    assert inspect(oldest).was_deleted
    kept = await session.scalars(
        select(DraftVersionTable.number).order_by(DraftVersionTable.number)
    )
    assert list(kept) == [3, 4, 5]
    assert await repository.prune_versions(row.id, 3) == 0


async def test_summaries_never_read_versions(database: Database, session: AsyncSession) -> None:
    rows = await add_drafts(session, MAX_SQLITE_BATCH_SIZE + 1)
    repository = DraftRepository(session)
    await repository.add_version(rows[0].id, DraftVersionOrigin.CREATED, DraftEdit(), AT)
    await session.commit()
    statements: list[str] = []

    def record(*args: Any) -> None:
        statements.append(str(args[2]))

    event.listen(database.engine.sync_engine, "before_cursor_execute", record)
    try:
        summaries = await repository.list_summaries()
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", record)

    assert len(summaries) == 101
    assert {summary.source_subject for summary in summaries} == {
        f"Subject {index}" for index in range(101)
    }
    assert statements
    assert not any("draft_versions" in statement for statement in statements)
