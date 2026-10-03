"""Messages the owner leaves out of a review are remembered and never auto-selected again,
until they select them (ADR 0017)."""

from collections.abc import AsyncIterator
from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.messages import MessageImportance, NormalizedMessage, RankedMessage
from mailbrief.services.application import ApplicationService
from mailbrief.services.ranking import ShortlistReviewError
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import MessageRepository
from mailbrief.storage.tables import AccountTable
from tests.unit.services.test_application import NOW, ZONE
from tests.unit.services.test_application_outside import message, scene

# Rank order is a1 > a2 > a3 > a4 > a5: high importance, then how recent each is.
INBOX = ("a1", "a2", "a3", "a4", "a5")


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema_for_tests()
    async with database.session() as active:
        yield active
    await database.dispose()


def loud(key: str, hours_ago: float = 1, **fields: object) -> NormalizedMessage:
    item = message(key, hours_ago=hours_ago, **fields)  # type: ignore[arg-type]
    return item.model_copy(update={"importance": MessageImportance.HIGH})


def inbox(*keys: str) -> list[NormalizedMessage]:
    return [loud(key, hours_ago=index + 1) for index, key in enumerate(keys)]


class Choosing:
    """A review gate that answers from a script and records what it was offered."""

    def __init__(self, *picks: tuple[str, ...] | None) -> None:
        self.picks = list(picks)
        self.offered: list[tuple[tuple[str, ...], frozenset[str], tuple[str, ...]]] = []

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
        keys = tuple(item.message.provider_message_id for item in candidates)
        self.offered.append((selected_ids, declined_ids, keys))
        return self.picks.pop(0)


async def declined(session: AsyncSession, account: AccountTable) -> set[str]:
    await session.commit()  # Nothing pending hides behind a cached read.
    return set(await MessageRepository(session).declined_among(account.id, [*INBOX, "r0", "r1"]))


async def run(
    service: ApplicationService, **options: object
) -> tuple[tuple[str, ...], frozenset[str]]:
    result, shortlist = await service.prepare_daily_shortlist(
        tz_key=ZONE,
        now_utc=NOW,
        shortlist_limit=3,
        **options,  # type: ignore[arg-type]
    )
    return tuple(item.message.provider_message_id for item in shortlist), result.outside_ids


async def test_what_the_owner_unchecks_is_declined_and_what_they_keep_is_not(
    session: AsyncSession,
) -> None:
    service, account = await scene(session, today=inbox(*INBOX))
    gate = Choosing(("a1",))

    chosen, _ = await run(service, shortlist_gate=gate)

    assert chosen == ("a1",)
    (offered,) = gate.offered
    assert offered[0] == ("a1", "a2", "a3")  # The automatic selection it was offered.
    assert offered[1] == frozenset()  # Nothing was declined yet.
    # a2 and a3 were picked and left out; a4 and a5 were never picked, so they aren't.
    assert await declined(session, account) == {"a2", "a3"}


async def test_the_automatic_selection_and_the_next_review_skip_declined_messages(
    session: AsyncSession,
) -> None:
    service, account = await scene(session, today=inbox(*INBOX))
    await run(service, shortlist_gate=Choosing(("a1",)))
    gate = Choosing(("a1", "a4", "a5"))

    chosen, _ = await run(service, shortlist_gate=gate)

    (offered,) = gate.offered
    assert offered[0] == ("a1", "a4", "a5")  # Checked by default: a2 and a3 stay unchecked...
    assert offered[1] == frozenset({"a2", "a3"})  # ...and the review is told why.
    assert offered[2] == INBOX  # A manual review still lists every message.
    assert chosen == ("a1", "a4", "a5")
    # An automatic run, too: no review, and the declined stay out.
    auto, _ = await run(service)
    assert auto == ("a1", "a4", "a5")
    assert await declined(session, account) == {"a2", "a3"}


async def test_selecting_a_declined_message_again_forgets_the_decline(
    session: AsyncSession,
) -> None:
    service, account = await scene(session, today=inbox(*INBOX))
    await run(service, shortlist_gate=Choosing(("a1",)))  # Declines a2 and a3.

    chosen, _ = await run(service, shortlist_gate=Choosing(("a2", "a1")))

    # a2 is taken back; a3 stays declined; a4 and a5 were picked this time and left out.
    assert chosen == ("a1", "a2")
    assert await declined(session, account) == {"a3", "a4", "a5"}
    auto, _ = await run(service)
    assert auto == ("a1", "a2", "a3") or auto == ("a1", "a2")  # Never a3, a4 or a5.
    assert not {"a3", "a4", "a5"} & set(auto)


async def test_a_review_that_is_cancelled_or_refused_declines_nothing(
    session: AsyncSession,
) -> None:
    service, account = await scene(session, today=inbox(*INBOX))

    result, shortlist = await service.prepare_daily_shortlist(
        tz_key=ZONE, now_utc=NOW, shortlist_limit=3, shortlist_gate=Choosing(None)
    )
    assert shortlist == [] and result.status.value == "cancelled"
    assert await declined(session, account) == set()

    for bad in (("a1", "a1"), ("a1", "nope"), ("a1", "a2", "a3", "a4")):
        with pytest.raises(ShortlistReviewError):
            await run(service, shortlist_gate=Choosing(bad))
        assert await declined(session, account) == set()


async def test_a_run_with_no_review_and_no_choices_writes_no_declines(
    session: AsyncSession,
) -> None:
    service, account = await scene(session, today=inbox(*INBOX))

    chosen, _ = await run(service)

    assert chosen == ("a1", "a2", "a3")
    assert await declined(session, account) == set()


async def test_exclude_declines_what_the_automatic_selection_picked_and_include_takes_it_back(
    session: AsyncSession,
) -> None:
    service, account = await scene(session, today=inbox(*INBOX))

    chosen, _ = await run(service, exclude_ids=("a2", "a5"))  # a5 wouldn't have been picked.

    assert chosen == ("a1", "a3")  # A per-run exclusion is not backfilled.
    assert await declined(session, account) == {"a2"}
    # The next automatic run skips a2, and backfills.
    assert (await run(service))[0] == ("a1", "a3", "a4")
    # Including it selects it, and forgets the decline. It takes a4's place under the limit of
    # three, but being pushed out by the limit is not a decision, so a4 is not declined.
    chosen, _ = await run(service, include_ids=("a2",))
    assert chosen == ("a1", "a2", "a3")
    assert await declined(session, account) == set()
    assert (await run(service))[0] == ("a1", "a2", "a3")


async def test_include_and_exclude_together(session: AsyncSession) -> None:
    service, account = await scene(session, today=inbox(*INBOX))
    await run(service, exclude_ids=("a1",))  # Declines a1.

    chosen, _ = await run(service, include_ids=("a1", "a5"), exclude_ids=("a3",))

    assert {"a1", "a5"} <= set(chosen) and "a3" not in chosen
    # a1 is back, and a3 (picked, then excluded) is now declined; a5 was never declined.
    assert await declined(session, account) == {"a3"}


async def test_declines_follow_the_message_not_the_day(session: AsyncSession) -> None:
    service, account = await scene(session, today=inbox(*INBOX))

    # A review of a past day's window: the messages are in it, and the owner leaves one out.
    gate = Choosing(("a1", "a3"))
    result, shortlist = await service.prepare_daily_shortlist(
        tz_key=ZONE,
        now_utc=NOW,
        shortlist_limit=3,
        shortlist_gate=gate,
        local_date=date(2026, 9, 30),
    )

    assert [item.message.provider_message_id for item in shortlist] == ["a1", "a3"]
    assert await declined(session, account) == {"a2"}
    assert result.shortlisted_message_keys == ("a1", "a3")


# Replies from outside today's Inbox


async def test_a_declined_reply_is_offered_unchecked_and_never_crowds_out_a_new_one(
    session: AsyncSession,
) -> None:
    newest = message("r0", thread="deck", hours_ago=2, inbox=False)
    others = [
        message(f"r{number}", thread="deck", hours_ago=2 + number, inbox=False)
        for number in (1, 2, 3)
    ]
    service, account = await scene(session, today=[], cached=[newest, *others])
    await MessageRepository(session).set_review_declined(account.id, ["r0"], NOW)
    await session.commit()
    gate = Choosing(("r1", "r2", "r3"))

    chosen, outside = await run(service, shortlist_gate=gate)

    (offered,) = gate.offered
    # Three places, four replies: the declined newest one gives way to the three others.
    assert set(offered[2]) == {"r1", "r2", "r3"}
    assert offered[1] == frozenset()  # r0 isn't a candidate, so nothing to label.
    assert chosen == ("r1", "r2", "r3") and outside == {"r1", "r2", "r3"}
    assert await declined(session, account) == {"r0"}


async def test_a_declined_reply_takes_a_free_place_unchecked(session: AsyncSession) -> None:
    older = message("r1", thread="deck", hours_ago=3, inbox=False)
    newest = message("r0", thread="deck", hours_ago=2, inbox=False)
    service, account = await scene(session, today=[], cached=[newest, older])
    await MessageRepository(session).set_review_declined(account.id, ["r0"], NOW)
    await session.commit()
    gate = Choosing(("r1",))

    chosen, _ = await run(service, shortlist_gate=gate)

    (offered,) = gate.offered
    assert offered[0] == ("r1",)  # The declined reply is not checked by default...
    assert offered[1] == frozenset({"r0"}) and "r0" in offered[2]  # ...but it is listed.
    assert chosen == ("r1",)
    # Still declined: the owner left it out again.
    assert await declined(session, account) == {"r0"}
    # An automatic run never picks it either.
    assert (await run(service))[0] == ("r1",)
