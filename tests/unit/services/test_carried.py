"""Runs through a day are cumulative (ADR 0017): the messages of the day's saved brief are
carried forward ahead of the automatic selection, unless they are gone, archived, blocked or
declined, and an automatic run's send cap never drops one."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.analysis import ANALYSIS_SCHEMA_VERSION
from mailbrief.domain.briefs import BriefStatus
from mailbrief.domain.digests import DailyDigest, DigestSection
from mailbrief.domain.messages import NormalizedMessage
from mailbrief.services.application import ApplicationService
from mailbrief.services.brief import needs_review_sentence
from mailbrief.services.threads import ThreadCheck
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import AccountRepository, DigestRepository, MessageRepository
from mailbrief.storage.tables import AccountTable, AnalysisTable, MessageTable
from tests.unit.services.ai_fakes import FakeAIProvider, answer_all
from tests.unit.services.test_application import NOW, ZONE, RecordingThreads
from tests.unit.services.test_application_outside import message, scene
from tests.unit.services.test_automatic_run import NoAsking, analyses, permit
from tests.unit.services.test_brief_service import (
    BODY,
    TODAY,
    RecordingGate,
    build,
    seed_outside_reply,
    texts_for,
    with_reader,
)
from tests.unit.services.test_brief_service import (
    ZONE as BRIEF_ZONE,
)
from tests.unit.services.test_brief_service import (
    inbox as brief_inbox,
)
from tests.unit.services.test_declines import INBOX, Choosing, inbox, loud


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema_for_tests()
    async with database.session() as active:
        yield active
    await database.dispose()


async def run(
    service: ApplicationService, limit: int = 3, **options: object
) -> tuple[tuple[str, ...], frozenset[str]]:
    result, shortlist = await service.prepare_daily_shortlist(
        tz_key=ZONE,
        now_utc=NOW,
        shortlist_limit=limit,
        **options,  # type: ignore[arg-type]
    )
    return tuple(item.message.provider_message_id for item in shortlist), result.outside_ids


async def declined(session: AsyncSession, account: AccountTable) -> set[str]:
    await session.commit()
    return set(await MessageRepository(session).declined_among(account.id, [*INBOX, "r9"]))


def set_inbox(service: ApplicationService, messages: list[NormalizedMessage]) -> None:
    service._provider.pages = [messages]  # type: ignore[attr-defined]


async def test_carried_messages_come_first_and_the_automatic_selection_fills_the_rest(
    session: AsyncSession,
) -> None:
    service, _ = await scene(session, today=inbox(*INBOX))

    chosen, _ = await run(service, carried=("a5", "a4"))

    # a4 and a5 rank last, yet they keep their places; a1 fills the one free slot.
    assert chosen == ("a1", "a4", "a5")
    assert (await run(service))[0] == ("a1", "a2", "a3")  # Without carrying, rank decides.


async def test_the_limit_bounds_the_carried_messages_in_brief_order(
    session: AsyncSession,
) -> None:
    service, _ = await scene(session, today=inbox(*INBOX))

    assert (await run(service, limit=1, carried=("a5", "a4")))[0] == ("a5",)
    assert (await run(service, limit=2, carried=("a4", "a5", "a1")))[0] == ("a4", "a5")


async def test_an_explicit_include_is_never_cut_by_carried_messages(
    session: AsyncSession,
) -> None:
    service, _ = await scene(session, today=inbox(*INBOX))

    chosen, _ = await run(service, limit=2, include_ids=("a3",), carried=("a5", "a4"))

    assert chosen == ("a3", "a5")


async def test_carried_ids_that_are_gone_are_ignored(session: AsyncSession) -> None:
    service, _ = await scene(session, today=inbox(*INBOX))

    chosen, outside = await run(service, carried=("never-cached", "a5", "a5"))

    assert chosen == ("a1", "a2", "a5") and outside == frozenset()


async def test_a_message_chosen_by_hand_survives_a_later_run(session: AsyncSession) -> None:
    service, _ = await scene(session, today=inbox(*INBOX))
    first, _ = await run(service, include_ids=("a5",))
    assert "a5" in first

    later, _ = await run(service, carried=first)

    assert later == first


async def test_a_carried_message_archived_since_drops_out(session: AsyncSession) -> None:
    service, _ = await scene(session, today=inbox(*INBOX))
    first, _ = await run(service, include_ids=("a5",))
    set_inbox(service, [item for item in inbox(*INBOX) if item.provider_message_id != "a5"])

    later, _ = await run(service, carried=first)

    assert "a5" not in later and later == ("a1", "a2", "a3")


async def test_a_carried_message_from_a_newly_blocked_sender_drops_out(
    session: AsyncSession,
) -> None:
    messages = [
        loud(key, hours_ago=index + 1, sender="spam@blocked.example")
        if key == "a5"
        else loud(key, hours_ago=index + 1)
        for index, key in enumerate(INBOX)
    ]
    service, _ = await scene(session, today=messages)
    first, _ = await run(service, include_ids=("a5",))

    later, _ = await run(service, carried=first, excluded_senders=("@blocked.example",))

    assert "a5" not in later and len(later) == 3


async def test_a_declined_carried_message_drops_out_and_stays_declined(
    session: AsyncSession,
) -> None:
    service, account = await scene(session, today=inbox(*INBOX))
    await run(service, shortlist_gate=Choosing(("a1",)))  # Declines a2 and a3.

    later, _ = await run(service, carried=("a2", "a1"))

    assert later == ("a1", "a4", "a5")  # a2 is dropped; a1 is kept; a4 and a5 fill in.
    assert await declined(session, account) == {"a2", "a3"}


async def test_carried_messages_are_checked_by_default_and_unchecking_one_declines_it(
    session: AsyncSession,
) -> None:
    service, account = await scene(session, today=inbox(*INBOX))
    gate = Choosing(("a1",))

    chosen, _ = await run(service, shortlist_gate=gate, carried=("a5", "a4"))

    (offered,) = gate.offered
    assert offered[0] == ("a1", "a4", "a5")  # Checked by default.
    assert chosen == ("a1",)
    # a4 and a5 were carried, so they count as picked: unchecking is an ordinary decline.
    assert await declined(session, account) == {"a4", "a5"}


async def test_excluding_a_carried_message_declines_it(session: AsyncSession) -> None:
    service, account = await scene(session, today=inbox(*INBOX))

    chosen, _ = await run(service, carried=("a5",), exclude_ids=("a5",))

    assert "a5" not in chosen
    assert await declined(session, account) == {"a5"}


async def test_a_carried_message_outside_the_window_keeps_its_outside_status(
    session: AsyncSession,
) -> None:
    # In no tracked thread and out of the Inbox: it could never be offered as an outside reply.
    kept = message("r9", thread="other", hours_ago=30, inbox=False)
    service, _ = await scene(session, today=inbox(*INBOX), cached=[kept])

    chosen, outside = await run(service, carried=("r9",), carried_outside=frozenset({"r9"}))

    assert "r9" in chosen and outside == frozenset({"r9"})
    assert "r9" not in (await run(service))[0]  # Not carried, it is not a candidate.


async def test_a_carried_outside_message_from_a_blocked_sender_drops_out(
    session: AsyncSession,
) -> None:
    kept = message("r9", thread="other", hours_ago=30, inbox=False, sender="x@blocked.example")
    service, _ = await scene(session, today=inbox(*INBOX), cached=[kept])

    chosen, outside = await run(
        service,
        carried=("r9",),
        carried_outside=frozenset({"r9"}),
        excluded_senders=("@blocked.example",),
    )

    assert "r9" not in chosen and outside == frozenset()


# Through the brief


async def test_ready_counts_only_new_messages(session: AsyncSession) -> None:
    await build(
        session, FakeAIProvider([answer_all()]), RecordingGate(True), messages=brief_inbox(3)
    ).generate(tz_key=BRIEF_ZONE)  # m0, m1 and m2 are in the day's brief.
    mailbox = brief_inbox(5)
    service = build(session, FakeAIProvider(), RecordingGate(False), messages=mailbox)
    reader = with_reader(service, mailbox)

    result = await service.generate(tz_key=BRIEF_ZONE, automatic=True)

    assert result.status is BriefStatus.READY_FOR_REVIEW
    assert result.ready == 2  # m3 and m4; the carried three don't count.
    assert reader.fetched == []


async def test_with_permission_and_nothing_new_nothing_is_read_sent_or_saved_again(
    session: AsyncSession,
) -> None:
    await build(
        session, FakeAIProvider([answer_all()]), RecordingGate(True), messages=brief_inbox(3)
    ).generate(tz_key=BRIEF_ZONE)
    await permit(session, 5, provider="fake")
    mailbox = brief_inbox(3)
    provider = FakeAIProvider()
    service = build(session, provider, NoAsking(answer=False), messages=mailbox)
    reader = with_reader(service, mailbox)

    result = await service.generate(tz_key=BRIEF_ZONE, automatic=True)

    assert (result.status, result.ready) == (BriefStatus.READY_FOR_REVIEW, 0)
    assert provider.calls == 0 and reader.fetched == []


async def test_cached_carried_messages_use_none_of_the_send_cap(session: AsyncSession) -> None:
    await build(
        session, FakeAIProvider([answer_all()]), RecordingGate(True), messages=brief_inbox(3)
    ).generate(tz_key=BRIEF_ZONE)
    await permit(session, 1, provider="fake")
    provider = FakeAIProvider([answer_all()])

    result = await build(
        session, provider, NoAsking(answer=False), messages=brief_inbox(4)
    ).generate(tz_key=BRIEF_ZONE, automatic=True)

    # Three carried and cached, one new: the cap of 1 covers the new one and defers nothing.
    assert result.status is BriefStatus.SAVED and result.deferred == 0
    assert sum(len(batch) for batch in provider.batches) == 1
    assert result.coverage is not None
    assert (result.coverage.analyzed, result.coverage.reused) == (1, 3)
    assert result.digest is not None and len(result.digest.items) == 4


async def test_an_outside_reply_stays_in_its_section_once_analyzed(
    session: AsyncSession,
) -> None:
    await seed_outside_reply(session)
    mailbox = brief_inbox(2)
    texts = {**texts_for(mailbox), "archived": f"{BODY} Reference archived."}
    first = await build(
        session,
        FakeAIProvider([answer_all()]),
        RecordingGate(True),
        messages=mailbox,
        texts=texts,
        threads=RecordingThreads(session, ThreadCheck()),
    ).generate(tz_key=BRIEF_ZONE)
    assert first.sync.outside_ids == {"archived"}

    # Analyzed now, it no longer qualifies as an outside reply; the brief carries it anyway.
    again = await build(
        session,
        FakeAIProvider(),
        RecordingGate(True),
        messages=mailbox,
        texts=texts,
        threads=RecordingThreads(session, ThreadCheck()),
    ).generate(tz_key=BRIEF_ZONE)

    assert again.sync.outside_ids == {"archived"}
    assert again.digest is not None
    assert [(item.message_key, item.section) for item in again.digest.items][-1] == (
        "archived",
        DigestSection.FOLLOW_UPS,
    )
    assert again.coverage is not None and again.coverage.shortlisted == 3


async def test_a_carried_message_the_owner_unchecks_is_declined_for_later_runs(
    session: AsyncSession,
) -> None:
    mailbox = brief_inbox(3)
    await build(
        session, FakeAIProvider([answer_all()]), RecordingGate(True), messages=mailbox
    ).generate(tz_key=BRIEF_ZONE)
    gate = Choosing(("m2",))
    service = build(session, FakeAIProvider(), RecordingGate(True), messages=mailbox)

    reviewed = await service.generate(tz_key=BRIEF_ZONE, shortlist_gate=gate)

    assert reviewed.digest is not None
    assert [item.message_key for item in reviewed.digest.items] == ["m2"]
    (offered,) = gate.offered
    assert set(offered[0]) == {"m0", "m1", "m2"}  # All three were checked by default.
    later = await build(session, FakeAIProvider(), RecordingGate(True), messages=mailbox).generate(
        tz_key=BRIEF_ZONE
    )
    assert later.digest is not None
    assert [item.message_key for item in later.digest.items] == ["m2"]  # m0 and m1 stay out.


async def test_a_carried_message_only_keeps_outside_status_if_the_brief_listed_it_so(
    session: AsyncSession,
) -> None:
    # Out of the Inbox, cached, and carried, but the brief had it as an ordinary message:
    # archived since, so it drops out.
    archived = message("r9", thread="other", hours_ago=1, inbox=False)
    service, _ = await scene(session, today=inbox(*INBOX), cached=[archived])

    chosen, outside = await run(service, carried=("r9",))

    assert "r9" not in chosen and outside == frozenset()


async def test_a_carried_outside_reply_back_in_today_s_inbox_is_an_ordinary_message(
    session: AsyncSession,
) -> None:
    service, _ = await scene(session, today=inbox(*INBOX))

    chosen, outside = await run(service, carried=("a5",), carried_outside=frozenset({"a5"}))

    assert chosen == ("a1", "a2", "a5") and outside == frozenset()


# An automatic run's send cap never drops a carried message


async def brief_of(session: AsyncSession, count: int) -> None:
    """The day's brief, made by hand from m0 to m<count - 1>."""
    await build(
        session, FakeAIProvider([answer_all()]), RecordingGate(True), messages=brief_inbox(count)
    ).generate(tz_key=BRIEF_ZONE)


async def outdate(session: AsyncSession, *keys: str) -> None:
    """Cached analyses become ones from the schema version before, as after an upgrade, so
    their messages need analysis again: those of ``keys``, or all of them."""
    rows = await session.execute(
        select(AnalysisTable, MessageTable.provider_message_id).join(
            MessageTable, AnalysisTable.message_id == MessageTable.id
        )
    )
    for analysis, key in rows.tuples().all():
        if not keys or key in keys:
            analysis.schema_version = str(int(ANALYSIS_SCHEMA_VERSION) - 1)
    await session.commit()


async def saved_brief(session: AsyncSession) -> DailyDigest:
    """Today's brief as it is saved now."""
    owner = await AccountRepository(session).get_by_email("user@example.com")
    assert owner is not None
    repository = DigestRepository(session)
    row = await repository.get_by_account_and_date(owner.id, TODAY)
    assert row is not None
    return DigestRepository.to_domain(
        row, await repository.get_digest_items(row.id), "user@example.com"
    )


def keys_of(digest: DailyDigest | None) -> set[str]:
    assert digest is not None
    return {item.message_key for item in digest.items}


async def test_carried_messages_that_need_analysis_are_sent_before_new_ones(
    session: AsyncSession,
) -> None:
    await brief_of(session, 2)  # m0 and m1 are in the day's brief.
    await outdate(session)
    await permit(session, 3, provider="fake")
    provider, gate = FakeAIProvider([answer_all()]), NoAsking(answer=False)

    result = await build(session, provider, gate, messages=brief_inbox(5)).generate(
        tz_key=BRIEF_ZONE, automatic=True
    )

    # m4, m3 and m2 are new and outrank m1 and m0, which take two of the three places anyway.
    (batch,) = provider.batches
    assert [request.subject for request in batch] == ["Budget 4", "Budget 1", "Budget 0"]
    assert result.status is BriefStatus.SAVED and not result.needs_review
    assert result.deferred == 2 and gate.previews == []  # m3 and m2 wait for a review.
    assert keys_of(result.digest) == {"m4", "m1", "m0"}


async def test_only_the_carried_messages_that_need_analysis_take_places(
    session: AsyncSession,
) -> None:
    await brief_of(session, 3)
    await outdate(session, "m0")  # m1 and m2 are still cached, and cost nothing.
    await permit(session, 1, provider="fake")
    provider = FakeAIProvider([answer_all()])

    result = await build(
        session, provider, NoAsking(answer=False), messages=brief_inbox(5)
    ).generate(tz_key=BRIEF_ZONE, automatic=True)

    # The one place goes to m0, the lowest-ranked of all, not to the new m4 or m3.
    (batch,) = provider.batches
    assert [request.subject for request in batch] == ["Budget 0"]
    assert result.status is BriefStatus.SAVED and result.deferred == 2
    assert result.coverage is not None
    assert (result.coverage.analyzed, result.coverage.reused) == (1, 2)
    assert keys_of(result.digest) == {"m0", "m1", "m2"}


async def test_when_the_cap_can_t_cover_the_carried_messages_nothing_is_sent_or_saved(
    session: AsyncSession,
) -> None:
    await brief_of(session, 3)
    await outdate(session)  # m0, m1 and m2 all need analysis again.
    await permit(session, 2, provider="fake")
    before, cached = await saved_brief(session), await analyses(session)
    mailbox = brief_inbox(5)
    provider, gate = FakeAIProvider(), NoAsking(answer=False)  # Any request fails the test.
    service = build(session, provider, gate, messages=mailbox)
    reader = with_reader(service, mailbox)

    result = await service.generate(tz_key=BRIEF_ZONE, automatic=True)

    assert result.status is BriefStatus.READY_FOR_REVIEW and result.needs_review
    assert result.ready == 5  # The three carried messages and the two new ones.
    assert (result.digest, result.coverage, result.deferred, result.ai_calls) == (None, None, 0, 0)
    assert provider.calls == 0 and provider.credential_checks == 0 and gate.previews == []
    assert sorted(reader.fetched) == ["m0", "m1", "m2", "m3", "m4"]  # Read in memory, to plan.
    assert await analyses(session) == cached  # Nothing new is cached either.
    assert await saved_brief(session) == before  # The day's brief is exactly as it was.


async def test_the_review_it_asks_for_sends_what_it_counted_and_keeps_every_message(
    session: AsyncSession,
) -> None:
    await brief_of(session, 3)
    await outdate(session)
    await permit(session, 2, provider="fake")
    mailbox = brief_inbox(5)
    waiting = await build(
        session, FakeAIProvider(), NoAsking(answer=False), messages=mailbox
    ).generate(tz_key=BRIEF_ZONE, automatic=True)
    assert waiting.needs_review
    provider, gate = FakeAIProvider([answer_all()]), RecordingGate(True)

    reviewed = await build(session, provider, gate, messages=mailbox).generate(tz_key=BRIEF_ZONE)

    # A manual run is unchanged: it asks, and the permission's cap doesn't bound it.
    assert [preview.message_count for preview in gate.previews] == [waiting.ready]
    assert sum(len(batch) for batch in provider.batches) == 5 and reviewed.deferred == 0
    assert keys_of(reviewed.digest) == {"m0", "m1", "m2", "m3", "m4"}
    # Everything is cached again, so the next automatic run has nothing to ask for.
    later = await build(
        session, FakeAIProvider(), NoAsking(answer=False), messages=mailbox
    ).generate(tz_key=BRIEF_ZONE, automatic=True)
    assert (later.status, later.ready, later.needs_review) == (
        BriefStatus.READY_FOR_REVIEW,
        0,
        False,
    )


@pytest.mark.parametrize(
    ("permission", "sent"),
    [(1, 0), (2, 0), (3, 3), (4, 4), (10, 5)],
    ids=["one", "one-short", "exactly-the-carried", "one-to-spare", "everything"],
)
async def test_after_an_upgrade_an_automatic_run_keeps_every_carried_message_or_saves_nothing(
    session: AsyncSession, permission: int, sent: int
) -> None:
    await brief_of(session, 3)  # m0, m1 and m2, analyzed before the upgrade.
    await outdate(session)
    await permit(session, permission, provider="fake")
    before = await saved_brief(session)
    provider = FakeAIProvider([answer_all()] if sent else [])

    result = await build(
        session, provider, NoAsking(answer=False), messages=brief_inbox(5)
    ).generate(tz_key=BRIEF_ZONE, automatic=True)

    assert sum(len(batch) for batch in provider.batches) == sent <= permission
    if sent:
        assert result.status is BriefStatus.SAVED and result.deferred == 5 - sent
        assert keys_of(result.digest) >= {"m0", "m1", "m2"}
        assert len(keys_of(result.digest)) == sent
        assert keys_of(await saved_brief(session)) == keys_of(result.digest)
    else:
        assert result.status is BriefStatus.READY_FOR_REVIEW and result.needs_review
        assert await saved_brief(session) == before


def test_the_sentence_says_how_many_messages_need_analysis() -> None:
    assert needs_review_sentence(9) == (
        "Today's brief needs your review: 9 messages need analysis."
    )
    assert needs_review_sentence(1) == "Today's brief needs your review: 1 message needs analysis."
