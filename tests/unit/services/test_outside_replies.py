"""Replies in tracked threads that today's Inbox sync can't see: which qualify, in what order,
how many, and that finding them takes a fixed number of queries."""

from collections.abc import AsyncIterator, Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.analysis import ANALYSIS_SCHEMA_VERSION
from mailbrief.domain.messages import EmailContact, NormalizedMessage, is_own_message
from mailbrief.services.calendar import DayWindow, day_window, resolve_timezone
from mailbrief.services.threads import (
    MAX_OUTSIDE_REPLIES,
    MAX_TRACKED_THREADS,
    ThreadService,
    own_addresses,
)
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import AnalysisRepository, MessageRepository
from mailbrief.storage.tables import AccountTable
from tests.factories import make_analysis
from tests.unit.services.test_threads import START, action, gmail, reply

TODAY = day_window(date(2026, 9, 30), resolve_timezone("America/Toronto"))
EARLIER = START + timedelta(days=1, hours=2)  # Tuesday, before today's window.
IN_TODAY = TODAY.start_utc + timedelta(hours=6)


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.from_path(tmp_path / "outside.sqlite3")
    await database.create_schema_for_tests()
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    async with database.session() as active:
        yield active


@pytest.fixture
def selects(database: Database) -> Iterator[list[str]]:
    """The SELECT statements the database runs while the test uses it."""
    statements: list[str] = []

    def record(*args: Any) -> None:
        statement = str(args[2])
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", record)
    yield statements
    event.remove(database.engine.sync_engine, "before_cursor_execute", record)


async def cache(session: AsyncSession, owner: AccountTable, *messages: NormalizedMessage) -> None:
    await MessageRepository(session).upsert_messages(owner.id, list(messages))
    await session.commit()


async def found(
    session: AsyncSession, owner: AccountTable, window: DayWindow = TODAY, **options: Any
) -> list[str]:
    """The replies offered; unless ``read_threads`` is given, as if this run's check had read
    every tracked thread."""
    service = ThreadService(session, reader=None)  # type: ignore[arg-type]
    if "read_threads" not in options:
        tracked = await service.tracked(owner, limit=None)
        options["read_threads"] = {thread.thread_id for thread in tracked}
    replies = await service.outside_replies(owner, window, **options)
    return [message.provider_message_id for message in replies]


async def track(session: AsyncSession, **threads: datetime) -> AccountTable:
    owner = await gmail(session)
    await action(session, threads or {"deck": START})
    return owner


async def test_archived_and_earlier_replies_are_found_newest_first_at_most_three(
    session: AsyncSession,
) -> None:
    owner = await track(session)
    await cache(
        session,
        owner,
        reply("archived", "deck", IN_TODAY, inbox=False),
        reply("yesterday", "deck", EARLIER),  # Still in the Inbox, but from an earlier day.
        reply("late", "deck", IN_TODAY + timedelta(hours=2), inbox=False),
        reply("tie-b", "deck", IN_TODAY + timedelta(hours=1), inbox=False),
        reply("tie-a", "deck", IN_TODAY + timedelta(hours=1), inbox=False),
        reply("earliest", "deck", START + timedelta(hours=1), inbox=False),
    )

    assert MAX_OUTSIDE_REPLIES == 3
    # Newest first; equal times by message ID; the rest are cut.
    assert await found(session, owner) == ["late", "tie-a", "tie-b"]
    assert await found(session, owner, limit=1) == ["late"]
    assert await found(session, owner, limit=10) == [
        "late",
        "tie-a",
        "tie-b",
        "archived",
        "yesterday",
        "earliest",
    ]
    with pytest.raises(ValueError):
        await found(session, owner, limit=0)


async def test_the_inbox_messages_of_today_s_window_are_left_to_today_s_sync(
    session: AsyncSession,
) -> None:
    owner = await track(session)
    await cache(
        session,
        owner,
        reply("covered", "deck", IN_TODAY, inbox=True),
        reply("archived", "deck", IN_TODAY, inbox=False),
        # The window's edges: its start is inside it, its end is the next day's.
        reply("at-start", "deck", TODAY.start_utc, inbox=True),
        reply("at-end", "deck", TODAY.end_utc, inbox=True),
    )

    assert await found(session, owner) == ["at-end", "archived"]
    # A different day's window covers a different set.
    tomorrow = day_window(date(2026, 10, 1), resolve_timezone("America/Toronto"))
    assert await found(session, owner, tomorrow) == ["archived", "covered", "at-start"]


async def test_only_replies_after_their_thread_s_baseline_count(session: AsyncSession) -> None:
    owner = await track(session)
    await cache(
        session,
        owner,
        reply("before", "deck", START - timedelta(minutes=1), inbox=False),
        reply("equal", "deck", START, inbox=False),
        reply("after", "deck", START + timedelta(minutes=1), inbox=False),
    )

    assert await found(session, owner) == ["after"]


async def test_each_thread_has_its_own_baseline(session: AsyncSession) -> None:
    owner = await gmail(session)
    await action(session, {"early": START})
    await action(session, {"late": START + timedelta(days=1)})
    noon = START + timedelta(hours=12)  # After "early"'s baseline, before "late"'s.
    await cache(
        session,
        owner,
        reply("early-after", "early", noon, inbox=False),
        reply("late-before", "late", noon, inbox=False),
        reply("late-after", "late", START + timedelta(days=1, hours=1), inbox=False),
    )

    assert await found(session, owner) == ["late-after", "early-after"]


async def test_the_earliest_baseline_of_the_actions_tracking_a_thread_applies(
    session: AsyncSession,
) -> None:
    owner = await gmail(session)
    await action(session, {"deck": START + timedelta(hours=2)})
    await action(session, {"deck": START})
    await cache(
        session,
        owner,
        reply("between", "deck", START + timedelta(hours=1), inbox=False),
        reply("before", "deck", START - timedelta(hours=1), inbox=False),
    )

    assert await found(session, owner) == ["between"]


@pytest.mark.parametrize(
    "aliases",
    [
        ["gmail-1@example.com", "alias@example.org"],
        ["Alias@Example.org"],  # An alias list that lacks the primary address.
    ],
)
async def test_the_owner_s_own_mail_is_never_found(
    session: AsyncSession, aliases: list[str]
) -> None:
    owner = await track(session)
    owner.account_addresses = aliases
    await session.commit()

    def sender(key: str, address: str) -> NormalizedMessage:
        message = reply(key, "deck", IN_TODAY, inbox=False)
        return message.model_copy(update={"sender": EmailContact(name="Me", address=address)})

    await cache(
        session,
        owner,
        reply("sent", "deck", IN_TODAY, sent=True, inbox=False),
        sender("primary", " Gmail-1@Example.com "),
        sender("alias", "ALIAS@example.org"),
        reply("theirs", "deck", IN_TODAY - timedelta(hours=1), inbox=False),
    )

    assert await found(session, owner) == ["theirs"]
    mine = own_addresses(owner)
    assert mine == {"gmail-1@example.com", "alias@example.org"}
    assert is_own_message(sender("x", "alias@example.org"), mine)
    assert not is_own_message(reply("y", "deck", IN_TODAY), mine)


async def test_replies_already_analyzed_at_the_current_schema_are_not_found(
    session: AsyncSession,
) -> None:
    owner = await track(session)
    await cache(
        session,
        owner,
        reply("analyzed", "deck", IN_TODAY + timedelta(hours=3), inbox=False),
        reply("older", "deck", IN_TODAY + timedelta(hours=2), inbox=False),
        reply("fresh", "deck", IN_TODAY + timedelta(hours=1), inbox=False),
    )
    messages = MessageRepository(session)
    analyses = AnalysisRepository(session)
    for key, version in (("analyzed", ANALYSIS_SCHEMA_VERSION), ("older", "6")):
        row = await messages.get_by_provider_message_id(owner.id, key)
        assert row is not None
        await analyses.upsert_analysis(
            row.id, "h" * 64, "groq", "m", "p1", version, make_analysis()
        )
    await session.commit()

    # An analysis from an older schema doesn't count; the message is analyzed again.
    assert await found(session, owner) == ["older", "fresh"]


async def test_only_threads_of_this_accounts_live_open_actions_count(
    session: AsyncSession,
) -> None:
    owner = await gmail(session)
    await action(session, {"open": START})
    await action(session, {"done": START}, status="completed")
    await action(session, {"deleted": START}, deleted=True)
    await action(session, {"other-account": START}, provider_account_id="gmail-2")
    await action(session, {"no-thread": START}, provider_account_id=None)
    await cache(
        session,
        owner,
        *(
            reply(thread, thread, IN_TODAY, inbox=False)
            for thread in ("open", "done", "deleted", "other-account", "no-thread", "untracked")
        ),
    )

    assert await found(session, owner) == ["open"]
    # The other account's mail is its own.
    other = await gmail(session, "gmail-2")
    assert await found(session, other) == []


async def test_senders_the_owner_excluded_never_use_up_the_three(session: AsyncSession) -> None:
    owner = await track(session)

    def from_address(key: str, address: str, minutes: int) -> NormalizedMessage:
        message = reply(key, "deck", IN_TODAY + timedelta(minutes=minutes), inbox=False)
        return message.model_copy(update={"sender": EmailContact(name=None, address=address)})

    await cache(
        session,
        owner,
        from_address("blocked-1", "noisy@news.example.com", 4),
        from_address("blocked-2", "noisy@example.com", 3),
        from_address("blocked-3", "NOISY@example.org", 2),
        from_address("kept-1", "sam@example.com", 1),
        from_address("kept-2", "kim@example.com", 0),
    )

    everyone = ("@example.com", "noisy@example.org")
    assert await found(session, owner, excluded_senders=everyone) == []  # All are example.*.
    noisy = ("@news.example.com", "noisy@example.com", "noisy@example.org")
    assert await found(session, owner, excluded_senders=noisy) == ["kept-1", "kept-2"]
    assert await found(session, owner) == ["blocked-1", "blocked-2", "blocked-3"]


async def test_every_tracked_thread_counts_not_only_the_ones_the_check_reads(
    session: AsyncSession,
) -> None:
    owner = await gmail(session)
    threads = {f"t{index:02d}": START for index in range(MAX_TRACKED_THREADS + 1)}
    # The least urgent action is last in line, beyond the check's 25.
    for index, (thread, received) in enumerate(threads.items()):
        await action(session, {thread: received}, target=date(2026, 10, 1) + timedelta(days=index))
    last = f"t{MAX_TRACKED_THREADS:02d}"
    await cache(session, owner, reply("far", last, IN_TODAY, inbox=False))

    service = ThreadService(session, reader=None)  # type: ignore[arg-type]
    assert last not in {thread.thread_id for thread in await service.tracked(owner)}
    assert await found(session, owner) == ["far"]


async def test_nothing_tracked_means_nothing_found(session: AsyncSession) -> None:
    owner = await gmail(session)
    await cache(session, owner, reply("lone", "deck", IN_TODAY, inbox=False))

    assert await found(session, owner) == []


async def test_the_number_of_queries_does_not_grow_with_the_data(
    session: AsyncSession, selects: list[str]
) -> None:
    owner = await gmail(session)
    await action(session, {"one": START})
    await cache(session, owner, reply("r0", "one", IN_TODAY, inbox=False))
    selects.clear()
    assert await found(session, owner, read_threads={"one"}) == ["r0"]
    small = len(selects)

    for index in range(30):
        thread = f"thread-{index}"
        await action(session, {thread: START})
        await cache(
            session,
            owner,
            *(
                reply(
                    f"{thread}-{number}", thread, IN_TODAY + timedelta(minutes=number), inbox=False
                )
                for number in range(5)
            ),
        )
    every_thread = {"one", *(f"thread-{index}" for index in range(30))}
    selects.clear()
    assert len(await found(session, owner, limit=10, read_threads=every_thread)) == 10
    large = len(selects)

    # The tracked threads, then the messages; a caller that has the threads saves the first.
    assert small == large == 2
    service = ThreadService(session, reader=None)  # type: ignore[arg-type]
    every = await service.tracked(owner, limit=None)
    selects.clear()
    assert (
        len(
            await service.outside_replies(
                owner, TODAY, limit=10, tracked=every, read_threads=every_thread
            )
        )
        == 10
    )
    assert len(selects) == 1


async def test_replies_the_owner_declined_come_after_the_others(session: AsyncSession) -> None:
    owner = await track(session)
    await cache(
        session,
        owner,
        reply("declined-newest", "deck", IN_TODAY + timedelta(hours=3), inbox=False),
        reply("old", "deck", IN_TODAY, inbox=False),
        reply("middle", "deck", IN_TODAY + timedelta(hours=1), inbox=False),
        reply("new", "deck", IN_TODAY + timedelta(hours=2), inbox=False),
    )
    await MessageRepository(session).set_review_declined(owner.id, ["declined-newest"], START)
    await session.commit()

    # The declined reply is the newest, but it gives way: it only fills a place nobody wants.
    assert await found(session, owner) == ["new", "middle", "old"]
    assert await found(session, owner, limit=10) == ["new", "middle", "old", "declined-newest"]
    assert await found(session, owner, limit=1) == ["new"]
    # Taken back, it is the newest again.
    await MessageRepository(session).set_review_declined(owner.id, ["declined-newest"], None)
    await session.commit()
    assert await found(session, owner) == ["declined-newest", "new", "middle"]


async def test_only_threads_this_run_read_are_drawn_on(session: AsyncSession) -> None:
    owner = await track(session, deck=START, budget=START)
    await cache(
        session,
        owner,
        reply("in-deck", "deck", IN_TODAY, inbox=False),
        reply("in-budget", "budget", IN_TODAY - timedelta(hours=1), inbox=False),
    )

    # A thread the check didn't read may hold a reply since trashed: it offers nothing.
    assert await found(session, owner, read_threads={"deck"}) == ["in-deck"]
    assert await found(session, owner, read_threads={"budget"}) == ["in-budget"]
    assert await found(session, owner, read_threads=set()) == []
    assert await found(session, owner, read_threads={"untracked"}) == []
    assert await found(session, owner, read_threads={"deck", "budget"}) == [
        "in-deck",
        "in-budget",
    ]
