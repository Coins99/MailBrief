"""Saved briefs by day: the latest is the newest day, summaries, and one day's brief."""

from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.digests import DigestSection, DigestStatus, SavedBriefSummary
from mailbrief.domain.messages import AccountIdentity, ProviderKind
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import (
    AccountRepository,
    AnalysisRepository,
    DigestRepository,
    MessageRepository,
)
from mailbrief.storage.tables import DigestTable
from tests.factories import fingerprint_of, make_analysis, make_message, make_suggestion

TODAY = date(2026, 9, 29)
YESTERDAY = date(2026, 9, 28)


@pytest.fixture
async def database(tmp_path: Path) -> AsyncGenerator[Database]:
    db = Database.from_path(tmp_path / "history.sqlite3")
    await db.create_schema_for_tests()
    try:
        yield db
    finally:
        await db.dispose()


async def account(session: AsyncSession, email: str, provider: ProviderKind) -> int:
    row = await AccountRepository(session).upsert(
        AccountIdentity(provider=provider, provider_account_id=email, email_address=email)
    )
    return row.id


async def brief(
    session: AsyncSession,
    account_id: int,
    day: date,
    generated: datetime,
    items: int = 0,
    *,
    suggestions: bool = False,
    follow_ups: int = 0,
) -> int:
    """Save a brief with ``items`` analyzed messages, generated at ``generated``; the last
    ``follow_ups`` of them are replies from tracked threads."""
    messages = await MessageRepository(session).upsert_messages(
        account_id,
        [make_message(provider_message_id=f"{account_id}-{day}-{index}") for index in range(items)],
    )
    rows = []
    for position, message in enumerate(messages):
        analysis = make_analysis(
            suggestions=(
                (make_suggestion(title="Approve it", fingerprint=fingerprint_of("approve")),)
                if suggestions
                else ()
            )
        )
        saved = await AnalysisRepository(session).upsert_analysis(
            message_id=message.id,
            input_hash=f"hash-{message.id}",
            provider="groq",
            model="m",
            prompt_version="p",
            schema_version="s",
            analysis=analysis,
        )
        section = (
            DigestSection.FOLLOW_UPS if position >= items - follow_ups else DigestSection.ACTIONS
        )
        rows.append((message.id, saved.id, position, section))
    digest = await DigestRepository(session).save_digest(
        account_id=account_id,
        local_date=day,
        timezone_name="America/Toronto",
        status=DigestStatus.COMPLETE if items else DigestStatus.EMPTY,
        items=rows,
    )
    await session.execute(
        update(DigestTable).where(DigestTable.id == digest.id).values(generated_at_utc=generated)
    )
    await session.commit()
    return digest.id


async def test_the_latest_brief_is_the_newest_day_not_the_newest_save(
    database: Database,
) -> None:
    async with database.session() as session:
        owner = await account(session, "owner@example.com", ProviderKind.GMAIL)
        await brief(session, owner, TODAY, datetime(2026, 9, 29, 13, tzinfo=UTC), 1)
        # Caught up later the same day, for yesterday.
        await brief(session, owner, YESTERDAY, datetime(2026, 9, 29, 16, tzinfo=UTC), 2)

        latest = await DigestRepository(session).get_latest()

    assert latest is not None and latest.local_date == TODAY


async def test_summaries_count_items_in_one_query_newest_day_first(
    database: Database,
) -> None:
    async with database.session() as session:
        owner = await account(session, "owner@example.com", ProviderKind.GMAIL)
        other = await account(session, "other@example.com", ProviderKind.GMAIL)
        outlook = await account(session, "work@example.com", ProviderKind.MICROSOFT)
        await brief(session, owner, YESTERDAY, datetime(2026, 9, 29, 16, tzinfo=UTC), 3)
        await brief(session, owner, TODAY, datetime(2026, 9, 29, 13, tzinfo=UTC), 2)
        await brief(session, other, TODAY, datetime(2026, 9, 29, 14, tzinfo=UTC))
        await brief(session, outlook, TODAY, datetime(2026, 9, 29, 15, tzinfo=UTC), 1)

        statements: list[str] = []

        def record(*args: object) -> None:
            statements.append(str(args[2]))

        sync_engine = database.engine.sync_engine
        event.listen(sync_engine, "before_cursor_execute", record)
        try:
            summaries = await DigestRepository(session).list_summaries(10)
        finally:
            event.remove(sync_engine, "before_cursor_execute", record)

    assert len(statements) == 1
    assert [(s.account_email, s.local_date, s.item_count) for s in summaries] == [
        ("other@example.com", TODAY, 0),
        ("owner@example.com", TODAY, 2),
        ("owner@example.com", YESTERDAY, 3),
    ]
    assert summaries[1] == SavedBriefSummary(
        account_email="owner@example.com",
        local_date=TODAY,
        timezone_name="America/Toronto",
        status=DigestStatus.COMPLETE,
        generated_at_utc=datetime(2026, 9, 29, 13, tzinfo=UTC),
        item_count=2,
    )
    async with database.session() as session:
        assert len(await DigestRepository(session).list_summaries(2)) == 2


async def test_summaries_count_the_replies_from_tracked_threads(database: Database) -> None:
    async with database.session() as session:
        owner = await account(session, "owner@example.com", ProviderKind.GMAIL)
        await brief(session, owner, YESTERDAY, datetime(2026, 9, 28, 13, tzinfo=UTC), 3)
        await brief(session, owner, TODAY, datetime(2026, 9, 29, 13, tzinfo=UTC), 4, follow_ups=2)
        await brief(session, owner, date(2026, 9, 27), datetime(2026, 9, 27, 13, tzinfo=UTC))

        summaries = await DigestRepository(session).list_summaries(10)

    assert [(s.local_date, s.item_count, s.follow_up_count) for s in summaries] == [
        (TODAY, 4, 2),
        (YESTERDAY, 3, 0),
        (date(2026, 9, 27), 0, 0),  # A brief without items counts none.
    ]


async def test_one_day_s_brief_with_and_without_suggestions(database: Database) -> None:
    async with database.session() as session:
        owner = await account(session, "owner@example.com", ProviderKind.GMAIL)
        await brief(session, owner, TODAY, datetime(2026, 9, 29, 13, tzinfo=UTC), 1)
        await brief(
            session, owner, YESTERDAY, datetime(2026, 9, 28, 13, tzinfo=UTC), 1, suggestions=True
        )
        repository = DigestRepository(session)

        plain = await repository.get_for_account_date("owner@example.com", TODAY)
        suggested = await repository.get_for_account_date("owner@example.com", YESTERDAY)
        missing = await repository.get_for_account_date("owner@example.com", date(2026, 9, 27))
        stranger = await repository.get_for_account_date("stranger@example.com", TODAY)

    assert plain is not None and plain.account_id == "owner@example.com"
    assert plain.items[0].suggestions == ()
    assert suggested is not None and suggested.local_date == YESTERDAY
    assert [view.suggestion.title for view in suggested.items[0].suggestions] == ["Approve it"]
    assert missing is None and stranger is None


async def test_saved_dates_and_accounts_by_date(database: Database) -> None:
    async with database.session() as session:
        owner = await account(session, "owner@example.com", ProviderKind.GMAIL)
        other = await account(session, "other@example.com", ProviderKind.GMAIL)
        outlook = await account(session, "work@example.com", ProviderKind.MICROSOFT)
        for day in (date(2026, 9, 20), YESTERDAY, TODAY):
            await brief(session, owner, day, datetime(2026, 9, 29, tzinfo=UTC))
        await brief(session, other, YESTERDAY, datetime(2026, 9, 29, tzinfo=UTC))
        await brief(session, outlook, YESTERDAY, datetime(2026, 9, 29, tzinfo=UTC))
        repository = DigestRepository(session)

        dates = await repository.saved_dates("owner@example.com", date(2026, 9, 22), YESTERDAY)
        accounts = await repository.accounts_with_brief(YESTERDAY)

    assert dates == {YESTERDAY}
    assert accounts == ("other@example.com", "owner@example.com")
