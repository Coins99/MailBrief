"""The preferences row: defaults when absent, fresh reads, and replacing unreadable rows."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.drafts import DraftLength, DraftTone
from mailbrief.domain.preferences import OwnerPreferences, PreferencesEdit
from mailbrief.storage.database import Database
from mailbrief.storage.preferences import (
    PreferencesRepository,
    apply_edit,
    preferences_from_row,
)
from mailbrief.storage.tables import OwnerPreferencesTable

AT = datetime(2026, 9, 29, 13, tzinfo=UTC)
EDIT = PreferencesEdit(
    time_zone="America/Toronto",
    shortlist_limit=4,
    excluded_senders=("@news.example.com", "boss@example.com"),
    draft_tone=DraftTone.WARM,
    draft_length=DraftLength.SHORT,
    ai_batch_size=2,
    ai_body_character_limit=3_000,
    ai_max_output_tokens=2_048,
    ai_max_requests_per_run=5,
    ai_timeout_seconds=90.5,
)


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.from_path(tmp_path / "preferences.sqlite3")
    await database.create_schema_for_tests()
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    async with database.session() as session:
        yield session


async def save(session: AsyncSession, edit: PreferencesEdit = EDIT) -> None:
    row = OwnerPreferencesTable(id=1, revision=1, updated_at_utc=AT)
    apply_edit(row, edit)
    PreferencesRepository(session).add(row)
    await session.commit()


async def test_no_row_means_defaults(session: AsyncSession) -> None:
    assert await PreferencesRepository(session).get_row() is None
    assert preferences_from_row(None) == OwnerPreferences.defaults()


async def test_a_saved_row_round_trips(database: Database, session: AsyncSession) -> None:
    await save(session)
    async with database.session() as other:
        loaded = preferences_from_row(await PreferencesRepository(other).get_row())
    assert loaded == OwnerPreferences(**EDIT.model_dump(), revision=1, updated_at_utc=AT)


async def test_get_row_reads_changes_made_elsewhere(
    database: Database, session: AsyncSession
) -> None:
    await save(session)
    repository = PreferencesRepository(session)
    assert (await repository.get_row()) is not None
    async with database.session() as other:
        await other.execute(text("UPDATE owner_preferences SET shortlist_limit = 7"))
        await other.commit()
    row = await repository.get_row()
    assert row is not None and row.shortlist_limit == 7


@pytest.mark.parametrize(
    "corruption",
    [
        "time_zone = 'Mars/Base'",
        "excluded_senders_json = '{\"a\": 1}'",
        "excluded_senders_json = '[1]'",
        "excluded_senders_json = '[\"not a rule\"]'",
    ],
)
async def test_an_invalid_row_raises_a_static_error(session: AsyncSession, corruption: str) -> None:
    await save(session)
    await session.execute(text(f"UPDATE owner_preferences SET {corruption}"))
    await session.commit()
    row = await PreferencesRepository(session).get_row()
    with pytest.raises(ValueError, match=r"^Saved preferences are invalid\.$"):
        preferences_from_row(row)


async def test_undecodable_json_can_still_be_replaced(session: AsyncSession) -> None:
    await save(session)
    await session.execute(text("UPDATE owner_preferences SET excluded_senders_json = 'not json'"))
    await session.commit()
    repository = PreferencesRepository(session)
    with pytest.raises(ValueError):
        await repository.get_row()
    await session.rollback()

    row = await repository.get_row_to_replace()
    assert row is not None and row.revision == 1
    apply_edit(row, PreferencesEdit())
    row.revision += 1
    await session.commit()

    replaced = preferences_from_row(await repository.get_row())
    assert replaced.excluded_senders == () and replaced.revision == 2
