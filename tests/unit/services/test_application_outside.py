"""Replies in tracked threads outside today's Inbox join today's run, and only today's
(ADR 0016): ranked, blocked, reviewed and limited like any other message."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import date, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.messages import (
    AccountIdentity,
    EmailContact,
    NormalizedMessage,
    ProviderKind,
    RankedMessage,
    RankReason,
)
from mailbrief.ports.threads import ThreadSnapshot
from mailbrief.services.application import ApplicationService
from mailbrief.services.calendar import DayWindow
from mailbrief.services.ranking import ShortlistReviewError
from mailbrief.services.threads import (
    MAX_OUTSIDE_REPLIES,
    ThreadCheck,
    ThreadService,
    TrackedThread,
)
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import (
    AccountRepository,
    MessageRepository,
    SyncRunRepository,
)
from mailbrief.storage.tables import AccountTable, ActionSourceTable, ActionTable
from tests.factories import make_message
from tests.unit.services.test_application import NOW, ZONE, RecordingThreads
from tests.unit.services.test_sync import FakeEmailProvider

SINCE = NOW - timedelta(days=2)
TODAY_INBOX = NOW - timedelta(hours=1)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema_for_tests()
    async with database.session() as active:
        yield active
    await database.dispose()


def message(
    key: str,
    *,
    thread: str | None = None,
    hours_ago: float = 1,
    inbox: bool = True,
    sender: str = "sam@example.com",
) -> NormalizedMessage:
    return make_message(
        provider_message_id=key,
        conversation_id=thread or f"own-{key}",
        sender=EmailContact(name=None, address=sender),
        received_at_utc=NOW - timedelta(hours=hours_ago),
        is_in_inbox=inbox,
        is_read=True,
        subject=f"Re: {key}",
        body_preview="Nothing to see.",
        importance="normal",
    )


async def scene(
    session: AsyncSession,
    *,
    today: list[NormalizedMessage] | None = None,
    cached: list[NormalizedMessage] = (),  # type: ignore[assignment]
    tracked: tuple[str, ...] = ("deck",),
) -> tuple[ApplicationService, AccountTable]:
    """An account with an open action in each ``tracked`` thread, ``cached`` messages already
    stored (as a thread check leaves them), and ``today`` in the provider's Inbox."""
    account = await AccountRepository(session).upsert(
        AccountIdentity(
            provider=ProviderKind.MICROSOFT,
            provider_account_id="acc-1",
            email_address="user@example.com",
        )
    )
    for thread in tracked:
        row = ActionTable(
            public_id=str(uuid.uuid4()),
            title="Send the deck",
            ownership="mine",
            status="open",
            deadline_precision="none",
            created_at_utc=SINCE,
            updated_at_utc=SINCE,
            revision=1,
        )
        session.add(row)
        await session.flush()
        session.add(
            ActionSourceTable(
                action_id=row.id,
                provider_message_id=f"source-{thread}",
                subject="Deck",
                sender_address="alex@example.com",
                web_link="https://mail.google.com/mail/u/#all/x",
                received_at_utc=SINCE,
                provider=account.provider,
                provider_account_id=account.provider_account_id,
                provider_thread_id=thread,
            )
        )
    await MessageRepository(session).upsert_messages(account.id, list(cached))
    await session.commit()
    inbox = today if today is not None else [message("quiet", hours_ago=2)]
    service = ApplicationService(
        provider=FakeEmailProvider(pages=[inbox]),
        message_repo=MessageRepository(session),
        sync_run_repo=SyncRunRepository(session),
        account_repo=AccountRepository(session),
        threads=RecordingThreads(session, ThreadCheck()),
    )
    return service, account


def keys(shortlist: list[RankedMessage]) -> list[str]:
    return [item.message.provider_message_id for item in shortlist]


class Gate:
    """Records what review was offered; answers with the suggestion unless told to pick."""

    def __init__(self, pick: tuple[str, ...] | None = None) -> None:
        self.pick = pick
        self.candidates: tuple[str, ...] = ()
        self.outside: frozenset[str] = frozenset()
        self.blocked: frozenset[str] = frozenset()
        self.limit = 0

    async def review(
        self,
        candidates: tuple[RankedMessage, ...],
        selected_ids: tuple[str, ...],
        *,
        blocked_ids: frozenset[str],
        outside_ids: frozenset[str],
        declined_ids: frozenset[str],
        limit: int,
    ) -> tuple[str, ...] | None:
        self.candidates = tuple(keys(list(candidates)))
        self.outside, self.blocked, self.limit = outside_ids, blocked_ids, limit
        return selected_ids if self.pick is None else self.pick


ARCHIVED = message("archived", thread="deck", hours_ago=3, inbox=False)
YESTERDAY = message("yesterday", thread="deck", hours_ago=30, inbox=True)


async def test_outside_replies_join_today_s_run_ranked_with_the_tracked_bonus(
    session: AsyncSession,
) -> None:
    service, account = await scene(session, cached=[ARCHIVED, YESTERDAY])

    result, shortlist = await service.prepare_daily_shortlist(tz_key=ZONE, now_utc=NOW)

    assert set(keys(shortlist)) == {"quiet", "archived", "yesterday"}
    assert result.outside_ids == {"archived", "yesterday"}
    assert set(result.shortlisted_message_keys) == {"quiet", "archived", "yesterday"}
    ranked = {item.message.provider_message_id: item for item in shortlist}
    assert RankReason.TRACKED_THREAD_REPLY in ranked["archived"].reasons
    assert RankReason.TRACKED_THREAD_REPLY not in ranked["quiet"].reasons
    # Their ranks are kept with their messages, like today's.
    stored = await MessageRepository(session).get_by_provider_message_id(account.id, "archived")
    assert stored is not None and stored.rank_score == ranked["archived"].score
    assert stored.rank_reasons_json == [reason.value for reason in ranked["archived"].reasons]


async def test_a_quiet_day_with_outside_replies_still_reviews_them(session: AsyncSession) -> None:
    service, _ = await scene(session, today=[], cached=[ARCHIVED])

    result, shortlist = await service.prepare_daily_shortlist(tz_key=ZONE, now_utc=NOW)

    assert keys(shortlist) == ["archived"]
    assert result.outside_ids == {"archived"}
    assert result.message_count == 0


async def test_nothing_at_all_still_returns_an_empty_shortlist(session: AsyncSession) -> None:
    service, _ = await scene(session, today=[])

    result, shortlist = await service.prepare_daily_shortlist(tz_key=ZONE, now_utc=NOW)

    assert shortlist == [] and result.outside_ids == frozenset()


async def test_only_today_s_run_brings_them_in(session: AsyncSession) -> None:
    old = message("old", hours_ago=52)  # Received on the 28th, a day with Inbox mail of its own.
    service, _ = await scene(
        session, today=[message("quiet", hours_ago=2), old], cached=[ARCHIVED, YESTERDAY]
    )

    # Two days back: neither reply falls in that day's window, so only today's gating keeps
    # them out.
    result, shortlist = await service.prepare_daily_shortlist(
        tz_key=ZONE, now_utc=NOW, local_date=date(2026, 9, 28)
    )

    assert keys(shortlist) == ["old"]
    assert result.outside_ids == frozenset()
    today, _ = await service.prepare_daily_shortlist(tz_key=ZONE, now_utc=NOW)
    assert today.outside_ids == {"archived", "yesterday"}


async def test_without_a_thread_service_there_are_none(session: AsyncSession) -> None:
    _, account = await scene(session, cached=[ARCHIVED])
    bare = ApplicationService(
        provider=FakeEmailProvider(pages=[[message("quiet", hours_ago=2)]]),
        message_repo=MessageRepository(session),
        sync_run_repo=SyncRunRepository(session),
        account_repo=AccountRepository(session),
    )

    result, shortlist = await bare.prepare_daily_shortlist(tz_key=ZONE, now_utc=NOW)

    assert keys(shortlist) == ["quiet"] and result.outside_ids == frozenset()


async def test_at_most_three_outside_replies_are_candidates_newest_first(
    session: AsyncSession,
) -> None:
    many = [
        message(f"r{number}", thread="deck", hours_ago=3 + number, inbox=False)
        for number in range(5)
    ]
    service, _ = await scene(session, cached=many)
    seen = Gate()

    await service.prepare_daily_shortlist(tz_key=ZONE, now_utc=NOW, shortlist_gate=seen)

    assert MAX_OUTSIDE_REPLIES == 3
    assert seen.outside == {"r0", "r1", "r2"}
    assert {"r3", "r4"}.isdisjoint(seen.candidates)


async def test_outside_replies_compete_for_the_limit_like_any_other_message(
    session: AsyncSession,
) -> None:
    service, _ = await scene(session, cached=[ARCHIVED, YESTERDAY])

    result, shortlist = await service.prepare_daily_shortlist(
        tz_key=ZONE, now_utc=NOW, shortlist_limit=1
    )

    # The tracked-thread bonus puts the newer reply ahead of the quiet Inbox message.
    assert keys(shortlist) == ["archived"]
    assert result.outside_ids == {"archived"}


async def test_blocked_senders_are_dropped_before_review_and_can_not_be_included(
    session: AsyncSession,
) -> None:
    noisy = message(
        "noisy", thread="deck", hours_ago=2, inbox=False, sender="noisy@news.example.com"
    )
    service, _ = await scene(session, cached=[noisy, ARCHIVED])
    seen = Gate()
    rules = ("@news.example.com",)

    result, shortlist = await service.prepare_daily_shortlist(
        tz_key=ZONE, now_utc=NOW, shortlist_gate=seen, excluded_senders=rules
    )

    assert "noisy" not in seen.candidates and seen.outside == {"archived"}
    assert "noisy" not in keys(shortlist) and result.outside_ids == {"archived"}
    # Naming it is refused like naming any message that is not a candidate.
    with pytest.raises(ShortlistReviewError):
        await service.prepare_daily_shortlist(
            tz_key=ZONE, now_utc=NOW, include_ids=("noisy",), excluded_senders=rules
        )


async def test_the_review_is_offered_every_outside_candidate_and_may_pick_any(
    session: AsyncSession,
) -> None:
    service, _ = await scene(session, cached=[ARCHIVED, YESTERDAY])
    quiet_only = Gate(pick=("quiet",))

    result, shortlist = await service.prepare_daily_shortlist(
        tz_key=ZONE, now_utc=NOW, shortlist_gate=quiet_only
    )

    assert quiet_only.outside == {"archived", "yesterday"}  # Offered, even unselected.
    assert set(quiet_only.candidates) == {"quiet", "archived", "yesterday"}
    assert quiet_only.limit == 10 and quiet_only.blocked == frozenset()
    assert keys(shortlist) == ["quiet"]
    assert result.outside_ids == frozenset()  # Only the selected outside replies.

    mixed = Gate(pick=("yesterday", "quiet"))
    result, shortlist = await service.prepare_daily_shortlist(
        tz_key=ZONE, now_utc=NOW, shortlist_gate=mixed
    )
    assert set(keys(shortlist)) == {"yesterday", "quiet"}
    assert result.outside_ids == {"yesterday"}
    assert result.shortlisted_message_keys == tuple(keys(shortlist))


async def test_a_review_outside_the_candidates_is_refused(session: AsyncSession) -> None:
    service, _ = await scene(session, cached=[ARCHIVED])

    with pytest.raises(ShortlistReviewError):
        await service.prepare_daily_shortlist(
            tz_key=ZONE, now_utc=NOW, shortlist_gate=Gate(pick=("not-offered",))
        )


async def test_include_and_exclude_can_name_outside_replies(session: AsyncSession) -> None:
    service, _ = await scene(session, cached=[ARCHIVED, YESTERDAY])

    # With room for one, the newer reply wins; naming the older one takes its place.
    result, shortlist = await service.prepare_daily_shortlist(
        tz_key=ZONE, now_utc=NOW, shortlist_limit=1, include_ids=("yesterday",)
    )
    assert keys(shortlist) == ["yesterday"] and result.outside_ids == {"yesterday"}

    result, shortlist = await service.prepare_daily_shortlist(
        tz_key=ZONE, now_utc=NOW, exclude_ids=("archived",)
    )
    assert set(keys(shortlist)) == {"quiet", "yesterday"}
    assert result.outside_ids == {"yesterday"}


async def test_outside_replies_from_today_s_window_never_duplicate_the_inbox(
    session: AsyncSession,
) -> None:
    inbox_reply = message("inbox-reply", thread="deck", hours_ago=2, inbox=True)
    service, _ = await scene(session, today=[inbox_reply], cached=[ARCHIVED])

    result, shortlist = await service.prepare_daily_shortlist(tz_key=ZONE, now_utc=NOW)

    assert sorted(keys(shortlist)) == ["archived", "inbox-reply"]  # Each once.
    assert result.outside_ids == {"archived"}  # The Inbox message is not an outside reply.


async def test_the_application_drops_blocked_senders_even_if_the_thread_service_does_not(
    session: AsyncSession,
) -> None:
    noisy = message("noisy", thread="deck", hours_ago=2, inbox=False, sender="noisy@example.com")
    service, account = await scene(session, cached=[noisy, ARCHIVED])

    class Leaky(ThreadService):
        """Returns every reply, ignoring the senders it was asked to leave out."""

        async def check(
            self, account: AccountTable, *, cancel: asyncio.Event | None = None
        ) -> ThreadCheck:
            return ThreadCheck()

        async def outside_replies(
            self,
            account: AccountTable,
            window: DayWindow,
            *,
            limit: int = MAX_OUTSIDE_REPLIES,
            excluded_senders: Sequence[str] = (),
            tracked: Sequence[TrackedThread] | None = None,
        ) -> list[NormalizedMessage]:
            return await super().outside_replies(account, window, limit=limit, tracked=tracked)

    leaky = ApplicationService(
        provider=FakeEmailProvider(pages=[[message("quiet", hours_ago=2)]]),
        message_repo=MessageRepository(session),
        sync_run_repo=SyncRunRepository(session),
        account_repo=AccountRepository(session),
        threads=Leaky(session, reader=None),  # type: ignore[arg-type]
    )
    seen = Gate()

    result, shortlist = await leaky.prepare_daily_shortlist(
        tz_key=ZONE, now_utc=NOW, shortlist_gate=seen, excluded_senders=("noisy@example.com",)
    )

    assert "noisy" not in seen.candidates and "noisy" not in keys(shortlist)
    assert result.outside_ids == {"archived"}


async def test_a_reply_trashed_after_it_was_cached_is_never_offered_or_ranked(
    session: AsyncSession,
) -> None:
    """The thread check runs before outside replies are looked for, so the run that learns a
    cached reply is in Trash or Spam already leaves it out."""

    class Reader:
        async def fetch_thread(self, provider_thread_id: str) -> ThreadSnapshot:
            return ThreadSnapshot(discarded_ids=frozenset({"archived"}))

    service, account = await scene(session, cached=[ARCHIVED, YESTERDAY])
    service._threads = ThreadService(session, Reader())

    result, shortlist = await service.prepare_daily_shortlist(tz_key=ZONE, now_utc=NOW)

    assert set(keys(shortlist)) == {"quiet", "yesterday"}
    assert result.outside_ids == {"yesterday"}
    assert "archived" not in result.shortlisted_message_keys
    assert (
        await MessageRepository(session).get_by_provider_message_id(account.id, "archived") is None
    )
