"""The open actions that continue an email's thread, for the brief's continuations."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.actions import ThreadLink
from mailbrief.domain.analysis import ActionOwnership
from mailbrief.domain.messages import NormalizedMessage
from mailbrief.services.actions import MAX_THREAD_LINKS, ActionService
from mailbrief.storage.database import Database
from mailbrief.storage.tables import AccountTable, ActionSourceTable, ActionTable
from tests.unit.services.test_thread_activity import account, cache
from tests.unit.services.test_thread_activity import message as thread_message

CREATED = datetime(2026, 9, 28, 12, tzinfo=UTC)
OWNER = "gmail-1@example.com"


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    database = Database.from_path(tmp_path / "links.sqlite3")
    await database.create_schema_for_tests()
    try:
        async with database.session() as active:
            yield active
    finally:
        await database.dispose()


def message(key: str, thread: str | None, *, owner: str = "gmail-1") -> NormalizedMessage:
    return thread_message(key, thread or "unused", 10, owner=owner).model_copy(
        update={"conversation_id": thread}
    )


async def action(
    session: AsyncSession,
    sources: list[tuple[str, str]],
    *,
    title: str = "Send the deck",
    ownership: str = "mine",
    status: str = "open",
    target: date | None = None,
    deleted: bool = False,
    owner: str = "gmail-1",
    minutes: int = 0,
) -> str:
    """An action whose sources are (message ID, thread) snapshots of ``owner``'s account."""
    created = CREATED + timedelta(minutes=minutes)
    row = ActionTable(
        public_id=str(uuid.uuid4()),
        title=title,
        ownership=ownership,
        status=status,
        deadline_precision="none",
        target_date=target,
        created_at_utc=created,
        updated_at_utc=created,
        completed_at_utc=created if status == "completed" else None,
        deleted_at_utc=created if deleted else None,
        revision=4,
    )
    session.add(row)
    await session.flush()
    for key, thread in sources:
        session.add(
            ActionSourceTable(
                action_id=row.id,
                provider_message_id=key,
                subject="Deck",
                sender_address="alex@example.com",
                web_link="https://mail.google.com/mail/u/#all/x",
                received_at_utc=CREATED,
                provider="gmail",
                provider_account_id=owner,
                provider_thread_id=thread,
            )
        )
    await session.commit()
    return row.public_id


async def links(session: AsyncSession, *keys: str) -> dict[str, tuple[ThreadLink, ...]]:
    return await ActionService(session).thread_links(OWNER, keys)


async def test_only_live_open_actions_of_the_account_continue_a_thread(
    session: AsyncSession,
) -> None:
    owner = await account(session)
    other = await account(session, "gmail-2")
    await cache(session, owner, message("reply", "deck"))
    await cache(session, other, message("reply", "deck", owner="gmail-2"))
    live = await action(
        session,
        [("source", "deck"), ("later", "deck")],  # Two sources in the thread: listed once.
        title="Chase <b>Sam</b>",
        ownership="waiting_for",
    )
    await action(session, [("done", "deck")], status="completed")
    await action(session, [("gone", "deck")], deleted=True)
    await action(session, [("elsewhere", "budget")])
    await action(session, [("theirs", "deck")], owner="gmail-2")

    found = await links(session, "reply")

    assert found == {
        "reply": (
            ThreadLink(
                public_id=live,
                title="Chase <b>Sam</b>",
                revision=4,
                ownership=ActionOwnership.WAITING_FOR,
                is_source=False,
            ),
        )
    }


async def test_is_source_marks_an_action_the_email_already_belongs_to(
    session: AsyncSession,
) -> None:
    owner = await account(session)
    await cache(session, owner, message("source", "deck"))
    own = await action(session, [("source", "deck")])
    other = await action(session, [("earlier", "deck")], minutes=5)

    found = (await links(session, "source"))["source"]

    assert [(link.public_id, link.is_source) for link in found] == [(own, True), (other, False)]


async def test_at_most_three_actions_most_urgent_first(session: AsyncSession) -> None:
    owner = await account(session)
    await cache(session, owner, message("reply", "deck"))
    await action(session, [("a", "deck")], title="Undated")
    await action(session, [("b", "deck")], title="Late", target=date(2026, 10, 9))
    await action(session, [("c", "deck")], title="Soon", target=date(2026, 10, 1))
    await action(session, [("d", "deck")], title="Later", target=date(2026, 10, 20))

    found = (await links(session, "reply"))["reply"]

    assert MAX_THREAD_LINKS == 3
    assert [link.title for link in found] == ["Soon", "Late", "Later"]  # Undated come last.


async def test_uncached_threadless_and_other_account_messages_get_nothing(
    session: AsyncSession,
) -> None:
    owner = await account(session)
    other = await account(session, "gmail-2")
    await cache(session, owner, message("lonely", None))
    await cache(session, other, message("theirs", "deck", owner="gmail-2"))
    await action(session, [("source", "deck")])

    assert await links(session, "lonely", "theirs", "unknown") == {}
    service = ActionService(session)
    assert await service.thread_links("gmail-2@example.com", ["theirs"]) == {}
    assert await service.thread_links("nobody@example.com", ["lonely"]) == {}
    assert await service.thread_links(OWNER, []) == {}


async def test_the_number_of_queries_does_not_grow(session: AsyncSession) -> None:
    owner = await account(session)
    statements: list[str] = []

    def count(*args: Any) -> None:
        statements.append(str(args[2]))

    engine = session.bind
    assert engine is not None
    event.listen(engine.sync_engine, "before_cursor_execute", count)
    try:
        await cache(session, owner, message("m0", "t0"))
        await action(session, [("s0", "t0")])
        statements.clear()
        assert len((await links(session, "m0"))["m0"]) == 1
        one = len(statements)

        await cache(session, owner, *(message(f"m{index}", f"t{index % 3}") for index in range(9)))
        for index in range(9):  # Four actions track t0, and three each t1 and t2.
            await action(session, [(f"s{index + 1}", f"t{index % 3}")])
        statements.clear()
        found = await links(session, *(f"m{index}" for index in range(9)))
        many = len(statements)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count)

    assert len(found) == 9 and all(len(value) == MAX_THREAD_LINKS for value in found.values())
    assert many == one == 1


async def test_a_message_of_another_account_with_the_same_id_is_not_confused(
    session: AsyncSession,
) -> None:
    owner = await account(session)
    other: AccountTable = await account(session, "gmail-2")
    await cache(session, owner, message("same", "deck"))
    await cache(session, other, message("same", "budget", owner="gmail-2"))
    mine = await action(session, [("x", "deck")])
    await action(session, [("y", "budget")], owner="gmail-2")

    assert [link.public_id for link in (await links(session, "same"))["same"]] == [mine]
