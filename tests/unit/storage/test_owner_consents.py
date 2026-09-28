"""Owner consents: active until revoked, reactivated by a new grant, never per account."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.storage.database import Database
from mailbrief.storage.repositories import OwnerConsentRepository
from mailbrief.storage.tables import OwnerConsentTable

AT = datetime(2026, 9, 28, 13, tzinfo=UTC)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    database = Database.from_path(tmp_path / "consents.sqlite3")
    await database.create_schema_for_tests()
    try:
        async with database.session() as active:
            yield active
    finally:
        await database.dispose()


async def test_grant_revoke_and_grant_again(session: AsyncSession) -> None:
    consents = OwnerConsentRepository(session)
    assert await consents.get_active("groq", "drafting", "1") is None

    granted = await consents.grant("groq", "drafting", "1", AT)
    await session.commit()
    assert (granted.granted_at_utc, granted.revoked_at_utc) == (AT, None)
    assert await consents.get_active("groq", "drafting", "1") is granted
    assert await consents.get_active("groq", "drafting", "2") is None
    assert await consents.get_active("groq", "other", "1") is None

    assert await consents.revoke_all("groq", AT + timedelta(hours=1)) == 1
    await session.commit()
    assert await consents.get_active("groq", "drafting", "1") is None
    assert granted.revoked_at_utc == AT + timedelta(hours=1)
    assert await consents.revoke_all("groq", AT) == 0

    again = await consents.grant("groq", "drafting", "1", AT + timedelta(days=1))
    await session.commit()
    assert again is granted and again.revoked_at_utc is None
    rows = await session.scalar(select(func.count()).select_from(OwnerConsentTable))
    assert rows == 1


async def test_revoking_one_provider_leaves_another(session: AsyncSession) -> None:
    consents = OwnerConsentRepository(session)
    await consents.grant("groq", "drafting", "1", AT)
    await consents.grant("other", "drafting", "1", AT)

    assert await consents.revoke_all("groq", AT) == 1
    assert await consents.get_active("other", "drafting", "1") is not None
