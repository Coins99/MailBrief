"""Automatic runs in storage (ADR 0017): the refresh preferences, the permission on a consent,
declined messages and the deferred coverage count."""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.briefs import AUTO_SEND_LIMIT_MAX
from mailbrief.domain.digests import DigestCoverage, DigestStatus
from mailbrief.domain.messages import AccountIdentity, ProviderKind
from mailbrief.domain.preferences import PreferencesEdit
from mailbrief.storage import repositories
from mailbrief.storage.database import Database
from mailbrief.storage.preferences import apply_edit, preferences_from_row
from mailbrief.storage.repositories import (
    AccountRepository,
    ConsentRepository,
    DigestRepository,
    MessageRepository,
)
from mailbrief.storage.tables import AccountTable, OwnerPreferencesTable
from tests.factories import make_message

AT = datetime(2026, 9, 30, 13, tzinfo=UTC)
LATER = AT + timedelta(hours=2)


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.from_path(tmp_path / "daily.sqlite3")
    await database.create_schema_for_tests()
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    async with database.session() as active:
        yield active


async def account(
    session: AsyncSession, name: str = "me", kind: ProviderKind = ProviderKind.GMAIL
) -> AccountTable:
    row = await AccountRepository(session).upsert(
        AccountIdentity(
            provider=kind, provider_account_id=name, email_address=f"{name}@example.com"
        )
    )
    await session.commit()
    return row


# The refresh preferences


@pytest.mark.parametrize(
    ("launch", "minutes"), [(False, None), (True, None), (False, 60), (True, 240)]
)
def test_the_refresh_preferences_round_trip_through_the_row(
    launch: bool, minutes: int | None
) -> None:
    row = OwnerPreferencesTable(id=1, revision=1, updated_at_utc=AT)

    apply_edit(row, PreferencesEdit(refresh_on_launch=launch, refresh_interval_minutes=minutes))

    saved = preferences_from_row(row)
    assert (saved.refresh_on_launch, saved.refresh_interval_minutes) == (launch, minutes)


def test_a_row_with_an_unknown_interval_is_unreadable() -> None:
    row = OwnerPreferencesTable(id=1, revision=1, updated_at_utc=AT)
    apply_edit(row, PreferencesEdit())
    row.refresh_interval_minutes = 45  # Not possible through the app; the CHECK refuses it too.

    with pytest.raises(ValueError, match="Saved preferences are invalid"):
        preferences_from_row(row)


# The permission on a consent


async def test_a_permission_needs_an_active_consent_and_records_when_it_was_given(
    session: AsyncSession,
) -> None:
    owner = await account(session)
    consents = ConsentRepository(session)

    assert await consents.set_auto_send(owner.id, "groq", "2", 3, AT) is None  # No consent.

    await consents.grant(owner.id, "groq", "2", AT)
    consent = await consents.set_auto_send(owner.id, "groq", "2", 3, LATER)

    assert consent is not None
    assert (consent.auto_send_limit, consent.auto_send_granted_at_utc) == (3, LATER)
    await session.commit()
    active = await consents.get_active(owner.id, "groq", "2")
    assert active is not None and active.auto_send_limit == 3
    # Another disclosure version, or another provider, has none of it.
    assert await consents.get_active(owner.id, "groq", "3") is None
    await consents.grant(owner.id, "groq", "3", AT)
    other = await consents.get_active(owner.id, "groq", "3")
    assert other is not None and (other.auto_send_limit, other.auto_send_granted_at_utc) == (
        0,
        None,
    )


async def test_zero_clears_the_permission_and_its_time_and_a_new_limit_restamps_it(
    session: AsyncSession,
) -> None:
    owner = await account(session)
    consents = ConsentRepository(session)
    await consents.grant(owner.id, "groq", "2", AT)
    await consents.set_auto_send(owner.id, "groq", "2", 5, AT)

    changed = await consents.set_auto_send(owner.id, "groq", "2", 2, LATER)
    assert changed is not None and (changed.auto_send_limit, changed.auto_send_granted_at_utc) == (
        2,
        LATER,
    )
    off = await consents.set_auto_send(owner.id, "groq", "2", 0, LATER)
    assert off is not None and (off.auto_send_limit, off.auto_send_granted_at_utc) == (0, None)


@pytest.mark.parametrize("limit", [-1, AUTO_SEND_LIMIT_MAX + 1, 100])
async def test_a_limit_outside_zero_to_ten_is_refused_and_changes_nothing(
    session: AsyncSession, limit: int
) -> None:
    owner = await account(session)
    consents = ConsentRepository(session)
    await consents.grant(owner.id, "groq", "2", AT)
    await consents.set_auto_send(owner.id, "groq", "2", 4, AT)

    with pytest.raises(ValueError, match="0 to 10"):
        await consents.set_auto_send(owner.id, "groq", "2", limit, LATER)

    active = await consents.get_active(owner.id, "groq", "2")
    assert active is not None and active.auto_send_limit == 4


async def test_revoking_a_consent_ends_its_permission_and_a_new_grant_does_not_bring_it_back(
    session: AsyncSession,
) -> None:
    owner = await account(session)
    consents = ConsentRepository(session)
    await consents.grant(owner.id, "groq", "2", AT)
    await consents.set_auto_send(owner.id, "groq", "2", 6, AT)
    await session.commit()
    loaded = await consents.get_active(owner.id, "groq", "2")
    assert loaded is not None and loaded.auto_send_limit == 6

    assert await consents.revoke_all(owner.id, "groq", LATER) == 1

    assert await consents.get_active(owner.id, "groq", "2") is None
    # A copy loaded before the revocation shows it at once, and keeps no permission.
    assert (loaded.auto_send_limit, loaded.auto_send_granted_at_utc) == (0, None)
    assert loaded.revoked_at_utc == LATER
    regranted = await consents.grant(owner.id, "groq", "2", LATER)
    assert (regranted.auto_send_limit, regranted.auto_send_granted_at_utc) == (0, None)
    assert await consents.set_auto_send(owner.id, "groq", "2", 1, LATER) is not None


async def test_a_revoked_or_missing_consent_can_not_hold_a_permission(
    session: AsyncSession,
) -> None:
    owner = await account(session)
    consents = ConsentRepository(session)
    await consents.grant(owner.id, "groq", "2", AT)
    await consents.revoke_all(owner.id, "groq", LATER)

    assert await consents.set_auto_send(owner.id, "groq", "2", 2, LATER) is None


async def test_the_newest_active_consent_is_the_most_recently_granted_gmail_one(
    session: AsyncSession,
) -> None:
    old = await account(session, "old")
    new = await account(session, "new")
    outlook = await account(session, "work", ProviderKind.MICROSOFT)
    consents = ConsentRepository(session)
    assert await consents.newest_active("groq", "2", "gmail") is None

    await consents.grant(old.id, "groq", "2", AT)
    await consents.grant(new.id, "groq", "2", LATER)
    await consents.grant(outlook.id, "groq", "2", LATER + timedelta(days=1))  # Not Gmail.
    await consents.grant(new.id, "openai", "2", LATER + timedelta(days=1))  # Another provider.
    await consents.grant(new.id, "groq", "1", LATER + timedelta(days=1))  # Another version.

    found = await consents.newest_active("groq", "2", "gmail")
    assert found is not None
    assert found[1].email_address == "new@example.com" and found[0].account_id == new.id
    await consents.revoke_all(new.id, "groq", LATER)
    found = await consents.newest_active("groq", "2", "gmail")
    assert found is not None and found[1].email_address == "old@example.com"


# Declined messages


async def cached(session: AsyncSession, owner: AccountTable, *keys: str) -> None:
    await MessageRepository(session).upsert_messages(
        owner.id, [make_message(provider_message_id=key) for key in keys]
    )
    await session.commit()


async def test_declines_are_remembered_forgotten_and_read_back(session: AsyncSession) -> None:
    owner = await account(session)
    await cached(session, owner, "a", "b", "c")
    messages = MessageRepository(session)
    assert await messages.declined_among(owner.id, ["a", "b", "c"]) == frozenset()

    assert await messages.set_review_declined(owner.id, ["a", "b", "zzz"], AT) == 2  # zzz: none.
    await session.commit()

    assert await messages.declined_among(owner.id, ["a", "b", "c", "zzz"]) == {"a", "b"}
    row = await messages.get_by_provider_message_id(owner.id, "a")
    assert row is not None and row.review_declined_at_utc == AT
    # Declining again changes nothing; forgetting clears it.
    assert await messages.set_review_declined(owner.id, ["a"], LATER) == 0
    assert row.review_declined_at_utc == AT
    assert await messages.set_review_declined(owner.id, ["a", "c"], None) == 1
    await session.commit()
    assert await messages.declined_among(owner.id, ["a", "b", "c"]) == {"b"}
    assert row.review_declined_at_utc is None  # A loaded row shows the change at once.


async def test_a_decline_belongs_to_its_account_and_survives_a_sync(
    session: AsyncSession,
) -> None:
    mine, other = await account(session, "me"), await account(session, "other")
    await cached(session, mine, "a")
    await cached(session, other, "a")
    messages = MessageRepository(session)
    await messages.set_review_declined(mine.id, ["a"], AT)
    await session.commit()

    assert await messages.declined_among(other.id, ["a"]) == frozenset()
    # The next sync writes the message again; it is still declined.
    await messages.upsert_messages(mine.id, [make_message(provider_message_id="a", subject="New")])
    await session.commit()
    assert await messages.declined_among(mine.id, ["a"]) == {"a"}


async def test_declines_work_across_the_sqlite_batch_size(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repositories, "MAX_SQLITE_BATCH_SIZE", 2)
    owner = await account(session)
    keys = [f"m{number}" for number in range(7)]
    await cached(session, owner, *keys)
    messages = MessageRepository(session)

    assert await messages.set_review_declined(owner.id, keys, AT) == 7
    await session.commit()

    assert await messages.declined_among(owner.id, keys) == set(keys)
    assert await messages.set_review_declined(owner.id, keys[:5], None) == 5
    assert await messages.declined_among(owner.id, keys) == {"m5", "m6"}


# Deferred coverage


async def test_a_brief_keeps_its_deferred_count(session: AsyncSession) -> None:
    owner = await account(session)
    digests = DigestRepository(session)
    coverage = DigestCoverage(
        sync_complete=True, shortlisted=6, analyzed=3, reused=1, failed=0, skipped=0, deferred=2
    )

    saved = await digests.save_digest(
        account_id=owner.id,
        local_date=date(2026, 9, 30),
        timezone_name="UTC",
        status=DigestStatus.EMPTY,
        coverage=coverage,
    )
    await session.commit()

    assert saved.deferred_count == 2
    assert DigestRepository.to_domain(saved, [], owner.email_address).coverage == coverage
    # Saved again without coverage (a brief from before M4's coverage), it reads none, and the
    # deferred count, which is never NULL, is 0.
    again = await digests.save_digest(
        account_id=owner.id,
        local_date=date(2026, 9, 30),
        timezone_name="UTC",
        status=DigestStatus.EMPTY,
    )
    assert again.deferred_count == 0
    assert DigestRepository.to_domain(again, [], owner.email_address).coverage is None
