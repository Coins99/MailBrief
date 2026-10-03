"""Thread activity on actions, derived from cached messages, and the owner's "mark seen"."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.actions import ThreadActivity
from mailbrief.domain.messages import AccountIdentity, EmailContact, NormalizedMessage, ProviderKind
from mailbrief.services.actions import ActionConflictError, ActionService
from mailbrief.services.threads import ThreadService
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import AccountRepository, MessageRepository
from mailbrief.storage.tables import AccountTable, ActionSourceTable, ActionTable
from tests.factories import make_message
from tests.unit.services.test_threads import FakeReader

SOURCE_AT = datetime(2026, 9, 28, 12, tzinfo=UTC)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    database = Database.from_path(tmp_path / "activity.sqlite3")
    await database.create_schema_for_tests()
    try:
        async with database.session() as active:
            yield active
    finally:
        await database.dispose()


async def account(session: AsyncSession, provider_account_id: str = "gmail-1") -> AccountTable:
    row = await AccountRepository(session).upsert(
        AccountIdentity(
            provider=ProviderKind.GMAIL,
            provider_account_id=provider_account_id,
            email_address=f"{provider_account_id}@example.com",
        )
    )
    await session.commit()
    return row


def message(
    key: str,
    thread: str,
    minutes: int,
    *,
    sent: bool = False,
    name: str | None = "Sam",
    owner: str = "gmail-1",
    address: str = "sam@example.com",
) -> NormalizedMessage:
    return make_message(
        provider=ProviderKind.GMAIL,
        provider_account_id=owner,
        provider_message_id=key,
        conversation_id=thread,
        sender=EmailContact(name=name, address=address),
        received_at_utc=SOURCE_AT + timedelta(minutes=minutes),
        is_sent=sent,
        is_in_inbox=not sent,
        web_link=f"https://mail.google.com/mail/u/#all/{thread}",
    )


async def cache(session: AsyncSession, owner: AccountTable, *messages: NormalizedMessage) -> None:
    await MessageRepository(session).upsert_messages(owner.id, list(messages))
    await session.commit()


async def action(
    session: AsyncSession, sources: list[tuple[str, str, int]], *, snapshot: bool = True
) -> str:
    """An open action whose sources are (message ID, thread, minutes after SOURCE_AT)."""
    row = ActionTable(
        public_id=str(uuid.uuid4()),
        title="Send the deck",
        ownership="mine",
        status="open",
        deadline_precision="none",
        created_at_utc=SOURCE_AT,
        updated_at_utc=SOURCE_AT,
        revision=1,
    )
    session.add(row)
    await session.flush()
    for key, thread, minutes in sources:
        session.add(
            ActionSourceTable(
                action_id=row.id,
                provider_message_id=key,
                subject="Deck",
                sender_address="alex@example.com",
                web_link="https://mail.google.com/mail/u/#all/x",
                received_at_utc=SOURCE_AT + timedelta(minutes=minutes),
                provider="gmail" if snapshot else None,
                provider_account_id="gmail-1" if snapshot else None,
                provider_thread_id=thread if snapshot else None,
            )
        )
    await session.commit()
    return row.public_id


async def activity(session: AsyncSession, public_id: str) -> ThreadActivity | None:
    return (await ActionService(session).get(public_id)).thread


async def test_others_later_messages_are_new_and_the_owner_s_reply_is_separate(
    session: AsyncSession,
) -> None:
    owner = await account(session)
    public_id = await action(session, [("source", "deck", 0)])
    await cache(
        session,
        owner,
        message("source", "deck", 0),  # The action's own source.
        message("earlier", "deck", -30),
        message("first", "deck", 10),
        message("mine", "deck", 20, sent=True),
        message("second", "deck", 30, name=None),
    )

    found = await activity(session, public_id)

    assert found == ThreadActivity(
        new_messages=2,
        latest_at_utc=SOURCE_AT + timedelta(minutes=30),
        latest_sender="sam@example.com",  # No display name, so the address.
        owner_replied_at_utc=SOURCE_AT + timedelta(minutes=20),
    )


async def test_an_action_without_a_thread_snapshot_has_no_activity(
    session: AsyncSession,
) -> None:
    owner = await account(session)
    public_id = await action(session, [("source", "deck", 0)], snapshot=False)
    await cache(session, owner, message("reply", "deck", 10))

    assert await activity(session, public_id) is None


async def test_a_quiet_thread_has_empty_activity(session: AsyncSession) -> None:
    await account(session)
    public_id = await action(session, [("source", "deck", 0)])
    found = await activity(session, public_id)
    assert found == ThreadActivity() and not found.unseen


async def test_other_accounts_and_other_threads_are_ignored(session: AsyncSession) -> None:
    owner = await account(session)
    other = await account(session, "gmail-2")
    public_id = await action(session, [("source", "deck", 0)])
    await cache(session, other, message("theirs", "deck", 10, owner="gmail-2"))
    await cache(session, owner, message("elsewhere", "other-thread", 10))

    found = await activity(session, public_id)

    assert found is not None and found.new_messages == 0


async def test_two_sources_in_one_thread_count_from_the_latest(session: AsyncSession) -> None:
    owner = await account(session)
    public_id = await action(session, [("first", "deck", 0), ("second", "deck", 60)])
    await cache(
        session,
        owner,
        message("first", "deck", 0),
        message("between", "deck", 30),
        message("second", "deck", 60),
        message("after", "deck", 90),
    )

    found = await activity(session, public_id)

    assert found is not None and found.new_messages == 1
    assert found.latest_at_utc == SOURCE_AT + timedelta(minutes=90)


async def test_activity_spans_the_action_s_threads(session: AsyncSession) -> None:
    owner = await account(session)
    public_id = await action(session, [("a", "deck", 0), ("b", "budget", 0)])
    await cache(session, owner, message("r1", "deck", 10), message("r2", "budget", 40))

    found = await activity(session, public_id)

    assert found is not None and found.new_messages == 2
    assert found.latest_at_utc == SOURCE_AT + timedelta(minutes=40)


async def test_marking_seen_moves_the_watermark_once(session: AsyncSession) -> None:
    owner = await account(session)
    public_id = await action(session, [("source", "deck", 0)])
    await cache(
        session, owner, message("reply", "deck", 10), message("mine", "deck", 50, sent=True)
    )
    service = ActionService(session, clock=lambda: SOURCE_AT + timedelta(days=1))

    marked = await service.mark_thread_seen(public_id, 1)

    assert marked.revision == 2
    assert marked.thread_seen_until_utc == SOURCE_AT + timedelta(minutes=50)
    assert marked.thread == ThreadActivity()
    again = await service.mark_thread_seen(public_id, 2)
    assert again.revision == 2  # Nothing new: no new revision.

    await cache(session, owner, message("later", "deck", 70))
    found = await activity(session, public_id)
    assert found is not None and found.new_messages == 1


async def test_marking_seen_needs_the_current_revision(session: AsyncSession) -> None:
    owner = await account(session)
    public_id = await action(session, [("source", "deck", 0)])
    await cache(session, owner, message("reply", "deck", 10))

    with pytest.raises(ActionConflictError):
        await ActionService(session).mark_thread_seen(public_id, 7)
    assert (await ActionService(session).get(public_id)).revision == 1


async def test_a_thread_check_never_changes_the_action(session: AsyncSession) -> None:
    owner = await account(session)
    public_id = await action(session, [("source", "deck", 0)])
    before = await ActionService(session).get(public_id)
    reader = FakeReader(
        {"deck": (message("reply", "deck", 10), message("mine", "deck", 20, sent=True))}
    )

    result = await ThreadService(session, reader).check(owner)

    after = await ActionService(session).get(public_id)
    assert result.stored == 2
    assert (after.revision, after.updated_at_utc, after.status) == (
        before.revision,
        before.updated_at_utc,
        before.status,
    )
    assert after.thread is not None and after.thread.new_messages == 1


async def test_a_reply_from_an_alias_without_the_sent_label_is_the_owner_s(
    session: AsyncSession,
) -> None:
    owner = await AccountRepository(session).upsert(
        AccountIdentity(
            provider=ProviderKind.GMAIL,
            provider_account_id="gmail-1",
            email_address="gmail-1@example.com",
            account_addresses=("gmail-1@example.com", "Alias@Example.org"),
        )
    )
    await session.commit()
    public_id = await action(session, [("source", "deck", 0)])
    await cache(
        session,
        owner,
        message("source", "deck", 0),
        message("theirs", "deck", 10),
        message("alias", "deck", 20, address="alias@example.org"),  # Not labelled SENT.
        message("primary", "deck", 30, address="GMAIL-1@example.com"),
    )

    found = await activity(session, public_id)

    assert found == ThreadActivity(
        new_messages=1,
        latest_at_utc=SOURCE_AT + timedelta(minutes=10),
        latest_sender="Sam",
        owner_replied_at_utc=SOURCE_AT + timedelta(minutes=30),
    )


async def test_the_primary_address_is_the_owner_s_when_the_alias_list_lacks_it(
    session: AsyncSession,
) -> None:
    owner = await AccountRepository(session).upsert(
        AccountIdentity(
            provider=ProviderKind.GMAIL,
            provider_account_id="gmail-1",
            email_address="gmail-1@example.com",
            account_addresses=("alias@example.org",),
        )
    )
    await session.commit()
    public_id = await action(session, [("source", "deck", 0)])
    await cache(
        session,
        owner,
        message("source", "deck", 0),
        message("theirs", "deck", 10),
        message("primary", "deck", 20, address="gmail-1@example.com"),  # Not labelled SENT.
    )

    found = await activity(session, public_id)

    assert found is not None
    assert (found.new_messages, found.owner_replied_at_utc) == (
        1,
        SOURCE_AT + timedelta(minutes=20),
    )
