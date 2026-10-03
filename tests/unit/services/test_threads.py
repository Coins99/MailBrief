"""Thread tracking: which threads, what is cached, and how failures, stops and cancels end."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.bodies import MessageBody
from mailbrief.domain.messages import AccountIdentity, EmailContact, NormalizedMessage, ProviderKind
from mailbrief.ports.errors import (
    AuthenticationRequiredError,
    MessageUnavailableError,
    ProviderRateLimitError,
    ProviderResponseError,
)
from mailbrief.ports.threads import ThreadSnapshot
from mailbrief.services import threads as threads_module
from mailbrief.services.calendar import DayWindow
from mailbrief.services.threads import (
    MAX_THREAD_MESSAGES,
    MAX_TRACKED_THREADS,
    THREAD_CONCURRENCY,
    ThreadService,
)
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import AccountRepository, MessageRepository
from mailbrief.storage.tables import AccountTable, ActionSourceTable, ActionTable, MessageTable
from tests.factories import make_message

START = datetime(2026, 9, 28, 12, tzinfo=UTC)
ACCOUNT_ID = "gmail-1"


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    database = Database.from_path(tmp_path / "threads.sqlite3")
    await database.create_schema_for_tests()
    try:
        async with database.session() as active:
            yield active
    finally:
        await database.dispose()


async def gmail(session: AsyncSession, provider_account_id: str = ACCOUNT_ID) -> AccountTable:
    account = await AccountRepository(session).upsert(
        AccountIdentity(
            provider=ProviderKind.GMAIL,
            provider_account_id=provider_account_id,
            email_address=f"{provider_account_id}@example.com",
        )
    )
    await session.commit()
    return account


async def action(
    session: AsyncSession,
    threads: dict[str, datetime],
    *,
    status: str = "open",
    ownership: str = "mine",
    target: date | None = None,
    deleted: bool = False,
    provider_account_id: str | None = ACCOUNT_ID,
    created: datetime = START,
) -> ActionTable:
    """An action with one source per thread, received at the given times."""
    row = ActionTable(
        public_id=str(uuid.uuid4()),
        title="Send the deck",
        ownership=ownership,
        status=status,
        deadline_precision="none",
        target_date=target,
        created_at_utc=created,
        updated_at_utc=created,
        completed_at_utc=START if status == "completed" else None,
        deleted_at_utc=START if deleted else None,
        revision=1,
    )
    session.add(row)
    await session.flush()
    for thread_id, received in threads.items():
        session.add(
            ActionSourceTable(
                action_id=row.id,
                provider_message_id=f"source-{thread_id}-{received.timestamp()}",
                subject="Deck",
                sender_address="alex@example.com",
                web_link="https://mail.google.com/mail/u/#all/x",
                received_at_utc=received,
                provider=None if provider_account_id is None else "gmail",
                provider_account_id=provider_account_id,
                provider_thread_id=None if provider_account_id is None else thread_id,
            )
        )
    await session.commit()
    return row


def reply(
    key: str, thread: str, received: datetime, *, sent: bool = False, inbox: bool = True
) -> NormalizedMessage:
    return make_message(
        provider=ProviderKind.GMAIL,
        provider_account_id=ACCOUNT_ID,
        provider_message_id=key,
        conversation_id=thread,
        sender=EmailContact(name="Sam", address="sam@example.com"),
        received_at_utc=received,
        is_sent=sent,
        is_in_inbox=inbox,
        web_link=f"https://mail.google.com/mail/u/#all/{thread}",
    )


class FakeReader:
    """Serves threads from a dict; an exception is raised. Records reads and body fetches.

    ``discarded`` names, per thread, the messages Gmail has in Trash or Spam: they are left
    out of the thread's messages and reported as discarded.
    """

    def __init__(
        self, threads: dict[str, tuple[NormalizedMessage, ...] | Exception] | None = None
    ) -> None:
        self.threads = threads or {}
        self.discarded: dict[str, set[str]] = {}
        self.reads: list[str] = []
        self.bodies: list[str] = []
        self.in_flight = 0
        self.most_in_flight = 0
        self.gate: asyncio.Event | None = None
        self.on_read: object = None

    async def fetch_thread(self, provider_thread_id: str) -> ThreadSnapshot:
        self.reads.append(provider_thread_id)
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if callable(self.on_read):
                self.on_read(provider_thread_id)
            found = self.threads.get(provider_thread_id, ())
            if isinstance(found, Exception):
                raise found
            discarded = frozenset(self.discarded.get(provider_thread_id, ()))
            return ThreadSnapshot(
                messages=tuple(
                    message for message in found if message.provider_message_id not in discarded
                ),
                discarded_ids=discarded,
            )
        finally:
            self.in_flight -= 1

    async def fetch_message_body(self, provider_message_id: str) -> MessageBody:
        self.bodies.append(provider_message_id)
        raise AssertionError("thread tracking never reads a body")


async def cached(session: AsyncSession) -> dict[str, tuple[bool, bool]]:
    rows = await session.execute(
        select(MessageTable.provider_message_id, MessageTable.is_sent, MessageTable.is_in_inbox)
    )
    return {key: (sent, inbox) for key, sent, inbox in rows.tuples()}


async def test_only_live_open_actions_of_this_account_are_tracked(
    session: AsyncSession,
) -> None:
    account = await gmail(session)
    await gmail(session, "gmail-2")
    await action(session, {"mine": START})
    await action(session, {"waiting": START}, ownership="waiting_for")
    await action(session, {"completed": START}, status="completed")
    await action(session, {"deleted": START}, deleted=True)
    await action(session, {"other-account": START}, provider_account_id="gmail-2")
    await action(session, {"untracked": START}, provider_account_id=None)

    tracked = await ThreadService(session, FakeReader()).tracked(account)

    assert sorted(thread.thread_id for thread in tracked) == ["mine", "waiting"]


async def test_threads_are_deduplicated_most_urgent_first_and_capped(
    session: AsyncSession,
) -> None:
    account = await gmail(session)
    await action(session, {"undated": START}, created=START - timedelta(days=9))
    await action(session, {"later": START}, target=date(2026, 10, 9))
    await action(session, {"soon": START, "shared": START}, target=date(2026, 10, 1))
    # A second, less urgent action on the shared thread, with a later own source.
    await action(session, {"shared": START + timedelta(hours=5)}, target=date(2026, 10, 20))
    for index in range(MAX_TRACKED_THREADS):
        await action(session, {f"extra-{index:02}": START}, target=date(2026, 12, 1))

    tracked = await ThreadService(session, FakeReader()).tracked(account)

    ids = [thread.thread_id for thread in tracked]
    assert len(ids) == MAX_TRACKED_THREADS
    assert ids[:3] == ["shared", "soon", "later"]
    assert "undated" not in ids  # Undated actions come last and fell past the cap.
    assert tracked[0].since_utc == START  # The earliest baseline among its actions.
    # Ranking takes every tracked thread, in the same order.
    everything = await ThreadService(session, FakeReader()).tracked(account, limit=None)
    assert [thread.thread_id for thread in everything][: len(ids)] == ids
    assert [thread.thread_id for thread in everything][-1] == "undated"
    assert len(everything) == MAX_TRACKED_THREADS + 4


async def test_a_thread_s_baseline_is_the_action_s_latest_own_source(
    session: AsyncSession,
) -> None:
    account = await gmail(session)
    row = await action(session, {"deck": START})
    session.add(
        ActionSourceTable(
            action_id=row.id,
            provider_message_id="second-source",
            subject="Deck",
            sender_address="alex@example.com",
            web_link="https://mail.google.com/mail/u/#all/deck",
            received_at_utc=START + timedelta(hours=2),
            provider="gmail",
            provider_account_id=ACCOUNT_ID,
            provider_thread_id="deck",
        )
    )
    await session.commit()

    (tracked,) = await ThreadService(session, FakeReader()).tracked(account)

    assert tracked.since_utc == START + timedelta(hours=2)


async def test_only_later_messages_are_cached_newest_twenty_with_sent_flagged(
    session: AsyncSession,
) -> None:
    account = await gmail(session)
    await action(session, {"deck": START})
    thread = (
        reply("before", "deck", START - timedelta(minutes=1)),
        reply("same-time", "deck", START),
        reply("mine", "deck", START + timedelta(minutes=1), sent=True, inbox=False),
        *(
            reply(f"later-{index:02}", "deck", START + timedelta(minutes=2 + index))
            for index in range(MAX_THREAD_MESSAGES + 5)
        ),
    )
    reader = FakeReader({"deck": thread})

    result = await ThreadService(session, reader).check(account)

    stored = await cached(session)
    assert len(stored) == MAX_THREAD_MESSAGES == result.stored
    assert "before" not in stored and "same-time" not in stored
    assert "mine" not in stored and "later-00" not in stored  # Older than the newest 20.
    assert "later-24" in stored
    assert (result.tracked, result.checked, result.failed, result.gone) == (1, 1, 0, 0)
    assert reader.bodies == []


async def test_the_owner_s_reply_is_cached_as_sent(session: AsyncSession) -> None:
    account = await gmail(session)
    await action(session, {"deck": START})
    reader = FakeReader(
        {"deck": (reply("mine", "deck", START + timedelta(hours=1), sent=True, inbox=False),)}
    )

    await ThreadService(session, reader).check(account)

    assert await cached(session) == {"mine": (True, False)}


async def test_gone_and_failed_threads_are_counted_and_the_rest_continue(
    session: AsyncSession,
) -> None:
    account = await gmail(session)
    for name in ("gone", "broken", "fine"):
        await action(session, {name: START})
    reader = FakeReader(
        {
            "gone": MessageUnavailableError("gone"),
            "broken": ProviderResponseError("bad"),
            "fine": (reply("r1", "fine", START + timedelta(hours=1)),),
        }
    )

    result = await ThreadService(session, reader).check(account)

    assert (result.checked, result.failed, result.gone, result.stored) == (1, 1, 1, 1)
    assert result.stopped_code is None
    assert list(await cached(session)) == ["r1"]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ProviderRateLimitError("slow down"), "RATE_LIMITED"),
        (AuthenticationRequiredError("reconnect"), "AUTH_REQUIRED"),
    ],
)
async def test_a_stopping_error_ends_the_check_and_keeps_what_was_stored(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, error: Exception, code: str
) -> None:
    account = await gmail(session)
    await action(session, {"first": START}, target=date(2026, 10, 1))
    await action(session, {"second": START}, target=date(2026, 10, 2))
    for index in range(6):
        await action(session, {f"after-{index}": START}, target=date(2026, 10, 3))
    reader = FakeReader(
        {
            "first": (reply("kept", "first", START + timedelta(hours=1)),),
            "second": error,
        }
    )

    # One read at a time, so the order is fixed: "first", then "second" stops the check.
    monkeypatch.setattr(threads_module, "THREAD_CONCURRENCY", 1)
    result = await ThreadService(session, reader).check(account)

    assert result.stopped_code == code
    assert list(await cached(session)) == ["kept"]
    assert reader.reads == ["first", "second"]


async def test_a_cancel_stops_before_the_next_read_and_leaves_no_task_running(
    session: AsyncSession,
) -> None:
    account = await gmail(session)
    for index in range(6):
        await action(session, {f"t{index}": START}, target=date(2026, 10, 1 + index))
    cancel = asyncio.Event()
    reader = FakeReader({"t0": (reply("r0", "t0", START + timedelta(hours=1)),)})
    reader.on_read = lambda thread_id: cancel.set() if thread_id == "t0" else None

    before = len(asyncio.all_tasks())
    result = await ThreadService(session, reader).check(account, cancel=cancel)

    assert len(reader.reads) <= THREAD_CONCURRENCY  # Only reads already started ran.
    assert result.tracked == 6 and result.checked == len(reader.reads)
    assert "r0" in await cached(session)
    assert len(asyncio.all_tasks()) == before


async def test_at_most_three_threads_are_read_at_once(session: AsyncSession) -> None:
    account = await gmail(session)
    for index in range(8):
        await action(session, {f"t{index}": START})
    reader = FakeReader()
    reader.gate = asyncio.Event()
    service = ThreadService(session, reader)

    running = asyncio.create_task(service.check(account))
    for _ in range(500):  # The tracked threads are read from the database first.
        if reader.in_flight:
            break
        await asyncio.sleep(0.002)
    for _ in range(5):
        await asyncio.sleep(0)
    assert reader.in_flight == THREAD_CONCURRENCY == 3
    reader.gate.set()
    result = await running

    assert reader.most_in_flight == 3
    assert result.checked == 8


async def test_no_tracked_threads_reads_nothing(session: AsyncSession) -> None:
    account = await gmail(session)
    reader = FakeReader()
    result = await ThreadService(session, reader).check(account)
    assert result.tracked == 0 and reader.reads == []
    assert await session.scalar(select(func.count()).select_from(MessageTable)) == 0


WINDOW = DayWindow(
    local_date=date(2026, 9, 29),
    start_utc=datetime(2026, 9, 29, 4, tzinfo=UTC),
    end_utc=datetime(2026, 9, 30, 4, tzinfo=UTC),
    timezone_name="America/Toronto",
)


@pytest.mark.parametrize("label", ["TRASH", "SPAM"])
async def test_a_cached_reply_gmail_discards_is_forgotten_and_comes_back_if_restored(
    session: AsyncSession, label: str
) -> None:
    account = await gmail(session)
    other = await gmail(session, "gmail-2")
    tracking = await action(session, {"deck": START})
    thrown = reply("thrown", "deck", START + timedelta(hours=1), inbox=False)
    kept = reply("kept", "deck", START + timedelta(hours=2), inbox=False)
    reader = FakeReader({"deck": (thrown, kept)})
    service = ThreadService(session, reader)
    first = await service.check(account)
    assert (first.stored, first.removed) == (2, 0)
    # Another account's message with the same ID, and an action source that points at the
    # cached reply: neither is touched when the reply is forgotten.
    await MessageRepository(session).upsert_messages(
        other.id, [thrown.model_copy(update={"provider_account_id": "gmail-2"})]
    )
    row = await MessageRepository(session).get_by_provider_message_id(account.id, "thrown")
    assert row is not None
    source = ActionSourceTable(
        action_id=tracking.id,
        message_id=row.id,
        provider_message_id="thrown",
        subject="Deck",
        sender_address="sam@example.com",
        web_link="https://mail.google.com/mail/u/#all/deck",
        received_at_utc=START - timedelta(hours=1),
        provider="gmail",
        provider_account_id=ACCOUNT_ID,
        provider_thread_id="deck",
    )
    session.add(source)
    await session.commit()
    offered = await service.outside_replies(account, WINDOW, read_threads=first.read_ids)
    assert [message.provider_message_id for message in offered] == ["kept", "thrown"]

    reader.discarded["deck"] = {"thrown"}  # The owner trashes it, or Gmail calls it spam.
    second = await service.check(account)

    assert (second.checked, second.removed) == (1, 1)
    offered = await service.outside_replies(account, WINDOW, read_threads=second.read_ids)
    assert [message.provider_message_id for message in offered] == ["kept"]
    owners = await session.scalars(
        select(MessageTable.account_id).where(MessageTable.provider_message_id == "thrown")
    )
    assert list(owners) == [other.id]
    await session.refresh(source)
    assert source.message_id is None and source.provider_message_id == "thrown"
    assert reader.bodies == []

    reader.discarded["deck"] = set()  # Taken back out.
    third = await service.check(account)

    assert (third.stored, third.removed) == (2, 0)
    offered = await service.outside_replies(account, WINDOW, read_threads=third.read_ids)
    assert [message.provider_message_id for message in offered] == ["kept", "thrown"]


async def test_a_discarded_message_that_was_never_cached_removes_nothing(
    session: AsyncSession,
) -> None:
    account = await gmail(session)
    await action(session, {"deck": START})
    reader = FakeReader({"deck": (reply("spam", "deck", START + timedelta(hours=1)),)})
    reader.discarded["deck"] = {"spam"}

    result = await ThreadService(session, reader).check(account)

    assert (result.checked, result.stored, result.removed) == (1, 0, 0)
    assert await cached(session) == {}
