"""Brief history: date bounds, catch-up days, missed days, listing and coverage lines."""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import HttpUrl
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.digests import (
    DailyDigest,
    DigestItem,
    DigestSection,
    DigestStatus,
    SavedBriefSummary,
)
from mailbrief.domain.messages import AccountIdentity, EmailContact, ProviderKind
from mailbrief.services.history import (
    CATCH_UP_DAYS,
    BriefDateError,
    BriefHistory,
    catch_up_days,
    check_brief_date,
    coverage_line,
    parse_brief_date,
)
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import AccountRepository, DigestRepository

TODAY = date(2026, 9, 29)


@pytest.mark.parametrize("back", [0, 1, CATCH_UP_DAYS])
def test_today_and_the_previous_seven_days_can_be_briefed(back: int) -> None:
    check_brief_date(date.fromordinal(TODAY.toordinal() - back), TODAY)


@pytest.mark.parametrize("day", [date(2026, 9, 21), date(2026, 9, 30), date(2025, 9, 29)])
def test_other_days_can_t(day: date) -> None:
    with pytest.raises(BriefDateError) as caught:
        check_brief_date(day, TODAY)
    assert str(caught.value) == "Choose today or one of the previous 7 days."


def test_dates_are_parsed_or_refused_with_a_static_message() -> None:
    assert parse_brief_date(" 2026-09-28 ") == date(2026, 9, 28)
    for text in ("yesterday", "2026-02-30", "", "28/09/2026"):
        with pytest.raises(BriefDateError) as caught:
            parse_brief_date(text)
        assert str(caught.value) == "Write the date as YYYY-MM-DD, such as 2026-09-29."


def test_catch_up_days_are_the_seven_before_today_newest_first() -> None:
    days = catch_up_days(TODAY)
    assert days[0] == date(2026, 9, 28) and days[-1] == date(2026, 9, 22)
    assert len(days) == CATCH_UP_DAYS == 7
    assert list(days) == sorted(days, reverse=True)


def summary(local_date: date, generated: datetime) -> SavedBriefSummary:
    return SavedBriefSummary(
        account_email="owner@example.com",
        local_date=local_date,
        timezone_name="America/Toronto",
        status=DigestStatus.EMPTY,
        generated_at_utc=generated,
        item_count=0,
    )


def test_a_brief_made_on_its_day_covers_messages_up_to_then() -> None:
    made = summary(TODAY, datetime(2026, 9, 29, 13, 14, tzinfo=UTC))  # 09:14 in Toronto.
    assert coverage_line(made) == (
        "Covers messages received on 2026-09-29 up to 09:14 (America/Toronto) "
        "that were in your Inbox then."
    )


def test_a_brief_made_later_covers_only_what_was_still_in_the_inbox() -> None:
    made = summary(date(2026, 9, 28), datetime(2026, 9, 29, 14, 2, tzinfo=UTC))
    assert coverage_line(made) == (
        "Covers messages received on 2026-09-28 (America/Toronto) that were still in your "
        "Inbox on 2026-09-29 at 10:02."
    )


def test_the_brief_s_own_zone_decides_the_day_it_was_made() -> None:
    """03:30 UTC on the 29th is still the 28th in Toronto: the same day."""
    digest = DailyDigest(
        account_id="owner@example.com",
        local_date=date(2026, 9, 28),
        timezone_name="America/Toronto",
        generated_at_utc=datetime(2026, 9, 29, 3, 30, tzinfo=UTC),
        status=DigestStatus.EMPTY,
    )
    assert "up to 23:30 (America/Toronto)" in coverage_line(digest)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    database = Database.from_path(tmp_path / "history.sqlite3")
    await database.create_schema_for_tests()
    try:
        async with database.session() as active:
            yield active
    finally:
        await database.dispose()


async def save(session: AsyncSession, email: str, day: date, status: DigestStatus) -> None:
    account = await AccountRepository(session).upsert(
        AccountIdentity(provider=ProviderKind.GMAIL, provider_account_id=email, email_address=email)
    )
    await DigestRepository(session).save_digest(
        account_id=account.id, local_date=day, timezone_name="UTC", status=status
    )
    await session.commit()


async def test_missed_days_treat_any_saved_brief_as_saved(session: AsyncSession) -> None:
    await save(session, "owner@example.com", date(2026, 9, 28), DigestStatus.EMPTY)
    await save(session, "owner@example.com", date(2026, 9, 26), DigestStatus.PARTIAL)
    await save(session, "owner@example.com", TODAY, DigestStatus.EMPTY)
    await save(session, "other@example.com", date(2026, 9, 27), DigestStatus.EMPTY)

    missed = await BriefHistory(session).missed_days("owner@example.com", TODAY)

    assert missed == (
        date(2026, 9, 27),
        date(2026, 9, 25),
        date(2026, 9, 24),
        date(2026, 9, 23),
        date(2026, 9, 22),
    )


async def test_saved_briefs_list_newest_day_first_and_load_by_day(
    session: AsyncSession,
) -> None:
    await save(session, "owner@example.com", date(2026, 9, 27), DigestStatus.EMPTY)
    await save(session, "owner@example.com", TODAY, DigestStatus.EMPTY)
    await save(session, "other@example.com", date(2026, 9, 28), DigestStatus.EMPTY)
    history = BriefHistory(session)

    listed = await history.list_saved()

    assert [(item.account_email, item.local_date) for item in listed] == [
        ("owner@example.com", TODAY),
        ("other@example.com", date(2026, 9, 28)),
        ("owner@example.com", date(2026, 9, 27)),
    ]
    assert len(await history.list_saved(limit=1)) == 1
    loaded = await history.get("other@example.com", date(2026, 9, 28))
    assert loaded is not None and loaded.account_id == "other@example.com"
    assert await history.get("owner@example.com", date(2026, 9, 28)) is None
    assert await history.accounts_for(date(2026, 9, 28)) == ("other@example.com",)


def with_follow_ups(count: int, total: int = 3) -> DailyDigest:
    """A brief made on its own day with ``count`` replies from tracked threads among
    ``total`` items."""
    items = tuple(
        DigestItem(
            message_key=f"m{index}",
            section=(
                DigestSection.FOLLOW_UPS if index >= total - count else DigestSection.HIGHLIGHTS
            ),
            position=index,
            sender=EmailContact(name=None, address="sam@example.com"),
            summary="A summary.",
            source_url=HttpUrl("https://mail.google.com/mail/u/#all/x"),
        )
        for index in range(total)
    )
    return DailyDigest(
        account_id="owner@example.com",
        local_date=TODAY,
        timezone_name="America/Toronto",
        generated_at_utc=datetime(2026, 9, 29, 13, 14, tzinfo=UTC),
        status=DigestStatus.COMPLETE,
        items=items,
    )


BASE = (
    "Covers messages received on 2026-09-29 up to 09:14 (America/Toronto) "
    "that were in your Inbox then."
)


def test_replies_from_tracked_threads_are_counted_on_the_coverage_line() -> None:
    assert coverage_line(with_follow_ups(0)) == BASE
    assert coverage_line(with_follow_ups(1)) == (
        BASE + " Also includes 1 reply from a thread you track that wasn't in today's Inbox."
    )
    assert coverage_line(with_follow_ups(3)) == (
        BASE + " Also includes 3 replies from threads you track that weren't in today's Inbox."
    )


def test_a_saved_brief_summary_says_the_same() -> None:
    made = summary(TODAY, datetime(2026, 9, 29, 13, 14, tzinfo=UTC))
    assert coverage_line(made) == BASE
    two = made.model_copy(update={"follow_up_count": 2})
    assert coverage_line(two) == coverage_line(with_follow_ups(2))
    # A brief made on a later day keeps the sentence after its own.
    later = summary(date(2026, 9, 28), datetime(2026, 9, 29, 14, 2, tzinfo=UTC))
    line = coverage_line(later.model_copy(update={"follow_up_count": 1}))
    assert line.startswith("Covers messages received on 2026-09-28")
    assert line.endswith(
        "Also includes 1 reply from a thread you track that wasn't in today's Inbox."
    )
