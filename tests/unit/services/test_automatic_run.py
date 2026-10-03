"""Automatic runs (ADR 0017): nothing is sent without the owner's permission on the active
consent, never more than the permission and messages per brief allow, and never a past day."""

from collections.abc import AsyncIterator
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.briefs import BriefStatus
from mailbrief.domain.digests import DigestStatus
from mailbrief.domain.messages import AccountIdentity, ProviderKind
from mailbrief.services.brief import CONSENT_DISCLOSURE_VERSION
from mailbrief.services.history import BriefDateError
from mailbrief.services.proposals import ProposalService
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import (
    AccountRepository,
    ConsentRepository,
    DigestRepository,
    MessageRepository,
)
from mailbrief.storage.tables import AccountTable, AnalysisTable
from tests.unit.services.ai_fakes import FakeAIProvider, answer_all
from tests.unit.services.test_brief_service import (
    NOW,
    TODAY,
    YESTERDAY,
    RangeProvider,
    RecordingGate,
    build,
    inbox,
    link_m0_to_an_action,
    texts_for,
    two_days,
    with_reader,
)

ZONE = "America/Toronto"
PROVIDER = "fake"  # The fake AI provider's name, which the consent is recorded under.


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema_for_tests()
    async with database.session() as active:
        yield active
    await database.dispose()


async def account(session: AsyncSession) -> AccountTable:
    """The account the fake Inbox belongs to, before its first sync."""
    row = await AccountRepository(session).upsert(
        AccountIdentity(
            provider=ProviderKind.MICROSOFT,
            provider_account_id="acc-1",
            email_address="user@example.com",
        )
    )
    await session.commit()
    return row


async def permit(
    session: AsyncSession,
    limit: int,
    *,
    version: str = CONSENT_DISCLOSURE_VERSION,
    provider: str = PROVIDER,
) -> AccountTable:
    """An active consent for ``version`` carrying a permission of ``limit`` messages."""
    owner = await account(session)
    consents = ConsentRepository(session)
    await consents.grant(owner.id, provider, version, NOW)
    if limit:
        await consents.set_auto_send(owner.id, provider, version, limit, NOW)
    await session.commit()
    return owner


async def saved_brief(session: AsyncSession, owner: AccountTable, day: date = TODAY) -> object:
    return await DigestRepository(session).get_by_account_and_date(owner.id, day)


async def analyses(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(AnalysisTable)) or 0


class NoAsking(RecordingGate):
    """A consent gate that must never be asked; any call is recorded in ``previews``."""


# Without permission: only a check


async def nothing_was_sent(
    session: AsyncSession,
    owner: AccountTable,
    provider: FakeAIProvider,
    gate: RecordingGate,
    reader: object,
) -> None:
    assert provider.calls == 0 and provider.credential_checks == 0
    assert gate.previews == []
    assert reader.fetched == []  # type: ignore[attr-defined]
    assert await saved_brief(session, owner) is None
    assert await analyses(session) == 0


@pytest.mark.parametrize("state", ["no consent", "permission 0", "revoked", "older version"])
async def test_without_permission_an_automatic_run_only_checks_and_reports(
    session: AsyncSession, state: str
) -> None:
    mailbox = inbox(3)
    if state == "permission 0":
        owner = await permit(session, 0)
    elif state == "revoked":
        owner = await permit(session, 5)
        await ConsentRepository(session).revoke_all(owner.id, PROVIDER, NOW)
        await session.commit()
    elif state == "older version":
        owner = await permit(session, 5, version="1")  # Not the current disclosure version.
    else:
        owner = await account(session)
    provider, gate = FakeAIProvider(), RecordingGate(answer=False)
    service = build(session, provider, gate, messages=mailbox)
    reader = with_reader(service, mailbox)

    result = await service.generate(tz_key=ZONE, automatic=True)

    assert result.status is BriefStatus.READY_FOR_REVIEW
    assert (result.ready, result.deferred, result.ai_calls) == (3, 0, 0)
    assert result.digest is None and result.coverage is None
    assert result.sync.message_count == 3  # It did sync.
    await nothing_was_sent(session, owner, provider, gate, reader)
    account_row = await AccountRepository(session).get_by_email("user@example.com")
    assert account_row is not None
    cached = await MessageRepository(session).get_messages_in_range(
        account_row.id, NOW.replace(hour=0), NOW.replace(day=NOW.day + 1, hour=0)
    )
    assert len(cached) == 3  # Metadata is cached and ranked, like Sync and review's first half.


async def test_turning_the_permission_off_takes_effect_on_the_next_run(
    session: AsyncSession,
) -> None:
    mailbox = inbox(2)
    owner = await permit(session, 2)
    provider, gate = FakeAIProvider([answer_all()]), RecordingGate(answer=False)
    service = build(session, provider, gate, messages=mailbox)

    sent = await service.generate(tz_key=ZONE, automatic=True)
    assert sent.status is BriefStatus.SAVED and provider.calls == 1

    await ConsentRepository(session).set_auto_send(owner.id, PROVIDER, "2", 0, NOW)
    await session.commit()
    fresh = FakeAIProvider()  # Any request would fail the test.
    checked = await build(session, fresh, gate, messages=mailbox).generate(
        tz_key=ZONE, automatic=True
    )

    assert checked.status is BriefStatus.READY_FOR_REVIEW and fresh.calls == 0
    assert gate.previews == []


async def test_nothing_selected_reports_nothing_ready_and_never_replaces_the_saved_brief(
    session: AsyncSession,
) -> None:
    owner = await permit(session, 3)
    mailbox = inbox(2)
    await build(
        session, FakeAIProvider([answer_all()]), RecordingGate(True), messages=mailbox
    ).generate(tz_key=ZONE, automatic=True)
    before = await saved_brief(session, owner)
    assert before is not None
    provider = FakeAIProvider()

    result = await build(session, provider, RecordingGate(False), messages=[]).generate(
        tz_key=ZONE, automatic=True
    )

    assert result.status is BriefStatus.READY_FOR_REVIEW and result.ready == 0
    assert provider.calls == 0
    kept = await DigestRepository(session).get_by_account_and_date(owner.id, TODAY)
    assert kept is not None and kept.status == DigestStatus.COMPLETE.value  # Not an empty brief.


# With permission: at most the cap, in rank order


async def test_with_permission_it_sends_the_top_messages_and_defers_the_rest(
    session: AsyncSession,
) -> None:
    mailbox = inbox(5)  # Equal scores, so rank order is newest first: m4, m3, m2, m1, m0.
    owner = await permit(session, 2)
    provider, gate = FakeAIProvider([answer_all()]), NoAsking(answer=False)
    service = build(session, provider, gate, messages=mailbox)
    reader = with_reader(service, mailbox)

    result = await service.generate(tz_key=ZONE, automatic=True)

    assert result.status is BriefStatus.SAVED
    (batch,) = provider.batches
    assert [request.subject for request in batch] == ["Budget 4", "Budget 3"]
    assert gate.previews == []  # Nobody was asked: the permission was the answer.
    assert result.deferred == 3 and result.ai_calls == 1
    assert result.coverage is not None
    coverage = result.coverage
    assert (coverage.shortlisted, coverage.analyzed, coverage.reused, coverage.deferred) == (
        5,
        2,
        0,
        3,
    )
    assert result.digest is not None
    assert [item.message_key for item in result.digest.items] == ["m4", "m3"]
    # Deferred messages are never cached: only the two sent have an analysis.
    assert await analyses(session) == 2
    assert await saved_brief(session, owner) is not None
    assert sorted(reader.fetched) == ["m0", "m1", "m2", "m3", "m4"]  # Read in memory, to plan.


@pytest.mark.parametrize(
    ("permission", "per_brief", "sent", "deferred"),
    [(2, 3, 2, 1), (5, 2, 2, 0), (10, 10, 5, 0), (1, 10, 1, 4), (3, 3, 3, 0)],
    ids=[
        "permission-under-messages-per-brief",
        "messages-per-brief-under-permission",
        "both-over-the-inbox",
        "one-message",
        "equal",
    ],
)
async def test_it_never_sends_more_than_the_permission_or_messages_per_brief(
    session: AsyncSession, permission: int, per_brief: int, sent: int, deferred: int
) -> None:
    await permit(session, permission)
    provider = FakeAIProvider([answer_all()])
    service = build(session, provider, NoAsking(answer=False), messages=inbox(5))

    result = await service.generate(tz_key=ZONE, shortlist_limit=per_brief, automatic=True)

    assert sum(len(batch) for batch in provider.batches) == sent
    assert result.deferred == deferred
    assert result.coverage is not None and result.coverage.analyzed == sent
    assert result.coverage.shortlisted == min(per_brief, 5)


async def test_messages_already_analyzed_cost_nothing_toward_the_cap(
    session: AsyncSession,
) -> None:
    mailbox = inbox(5)
    first = build(session, FakeAIProvider([answer_all()]), RecordingGate(True), messages=mailbox)
    await first.generate(tz_key=ZONE, shortlist_limit=2)  # Analyzes m4 and m3, and consents.
    owner = await AccountRepository(session).get_by_email("user@example.com")
    assert owner is not None
    await ConsentRepository(session).set_auto_send(owner.id, PROVIDER, "2", 1, NOW)
    await session.commit()
    provider = FakeAIProvider([answer_all()])

    result = await build(session, provider, NoAsking(answer=False), messages=mailbox).generate(
        tz_key=ZONE, shortlist_limit=4, automatic=True
    )

    (batch,) = provider.batches
    assert [request.subject for request in batch] == ["Budget 2"]  # One new message.
    assert result.coverage is not None
    assert (result.coverage.reused, result.coverage.analyzed, result.coverage.deferred) == (2, 1, 1)
    assert result.digest is not None
    assert [item.message_key for item in result.digest.items] == ["m4", "m3", "m2"]


async def test_a_missing_key_fails_the_run_without_a_request_and_keeps_the_deferrals(
    session: AsyncSession,
) -> None:
    await permit(session, 2)
    provider = FakeAIProvider(credentials=False)

    result = await build(session, provider, NoAsking(answer=False), messages=inbox(5)).generate(
        tz_key=ZONE, automatic=True
    )

    assert result.status is BriefStatus.ANALYSIS_FAILED and result.error_code == "AI_KEY_MISSING"
    assert provider.batches == []
    assert result.deferred == 3 and result.ai_calls == 0


async def test_a_cancelled_automatic_run_sends_nothing(session: AsyncSession) -> None:
    await permit(session, 2)
    provider = FakeAIProvider()
    import asyncio

    cancel = asyncio.Event()
    cancel.set()

    result = await build(session, provider, NoAsking(answer=False), messages=inbox(2)).generate(
        tz_key=ZONE, automatic=True, cancel=cancel
    )

    assert result.status is BriefStatus.CANCELLED and provider.calls == 0


# Today only, and no review


async def test_an_automatic_run_never_briefs_a_past_day_and_never_contacts_gmail_first(
    session: AsyncSession,
) -> None:
    await permit(session, 2)
    email = RangeProvider(two_days())
    provider, gate = FakeAIProvider(), NoAsking(answer=False)
    service = build(session, provider, gate, email=email)

    for day in (YESTERDAY, date(2026, 8, 28), date(2026, 9, 5)):
        with pytest.raises(BriefDateError) as caught:
            await service.generate(tz_key=ZONE, local_date=day, automatic=True)
        if day == YESTERDAY:
            assert str(caught.value) == "Automatic runs brief today only."

    assert email.connects == 0 and email.ranges == [] and provider.calls == 0
    # Today itself, named or not, is fine.
    named = await service.generate(tz_key=ZONE, local_date=TODAY, automatic=True)
    assert named.status is BriefStatus.READY_FOR_REVIEW or named.status is BriefStatus.SAVED


async def test_an_automatic_run_takes_no_review_and_no_choices(session: AsyncSession) -> None:
    await permit(session, 2)
    email = RangeProvider(two_days())
    service = build(session, FakeAIProvider(), NoAsking(answer=False), email=email)

    class Gate:
        async def review(self, *args: object, **kwargs: object) -> tuple[str, ...]:
            raise AssertionError("an automatic run is never reviewed")

    for options in (
        {"shortlist_gate": Gate()},
        {"include_ids": ("t0",)},
        {"exclude_ids": ("t0",)},
    ):
        with pytest.raises(ValueError, match="no review"):
            await service.generate(tz_key=ZONE, automatic=True, **options)

    assert email.connects == 0


# What an automatic brief does like any other


async def test_an_automatic_brief_still_derives_proposals(session: AsyncSession) -> None:
    signal = answer_all(follow_up="cancelled", follow_up_evidence="waiting on it")
    manual = build(session, FakeAIProvider([signal]), RecordingGate(True))
    await manual.generate(tz_key=ZONE, include_ids=("m1",), shortlist_limit=1)
    public_id = await link_m0_to_an_action(session)
    owner = await AccountRepository(session).get_by_email("user@example.com")
    assert owner is not None
    await ConsentRepository(session).set_auto_send(owner.id, PROVIDER, "2", 1, NOW)
    await session.commit()

    # m1, in the day's brief, follows the action's source m0 in its thread; m0 is the new one.
    result = await build(session, FakeAIProvider([answer_all()]), NoAsking(answer=False)).generate(
        tz_key=ZONE, automatic=True
    )

    assert result.status is BriefStatus.SAVED and result.proposals_created == 1
    (proposal,) = await ProposalService(session).pending()
    assert (proposal.action_public_id, proposal.provider_message_id) == (public_id, "m1")


async def test_a_manual_run_is_unchanged_by_an_automatic_permission(
    session: AsyncSession,
) -> None:
    await permit(session, 2)
    provider, gate = FakeAIProvider([answer_all()]), RecordingGate(answer=True)

    result = await build(
        session, provider, gate, messages=inbox(4), texts=texts_for(inbox(4))
    ).generate(tz_key=ZONE)

    # It still asks, and the permission's cap doesn't apply to a reviewed or manual run.
    assert len(gate.previews) == 1 and gate.previews[0].message_count == 4
    assert result.status is BriefStatus.SAVED and result.deferred == 0
    assert sum(len(batch) for batch in provider.batches) == 4


async def test_the_service_caps_at_messages_per_brief_even_if_the_selection_is_longer(
    session: AsyncSession,
) -> None:
    await permit(session, 10)
    provider = FakeAIProvider([answer_all()])
    service = build(session, provider, NoAsking(answer=False), messages=inbox(5))
    real = service._application.prepare_daily_shortlist

    async def longer(**options: object) -> object:
        # A selection over the owner's limit, which the application never makes itself.
        return await real(**{**options, "shortlist_limit": 5})  # type: ignore[arg-type]

    service._application.prepare_daily_shortlist = longer  # type: ignore[method-assign,assignment]

    result = await service.generate(tz_key=ZONE, shortlist_limit=2, automatic=True)

    assert sum(len(batch) for batch in provider.batches) == 2  # min(10, 2), not 5 or 10.
    assert result.deferred == 3 and result.coverage is not None
    assert result.coverage.shortlisted == 5
